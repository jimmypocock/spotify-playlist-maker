#!/usr/bin/env python3
"""
spotify_playlist.py — Build a Spotify playlist of the top N tracks for every
artist in a list.

Usage:
    python spotify_playlist.py --artists lineups/<name>.txt --name "<playlist>" \
        [--top 10] [--market US] [--description "..."] [--public] [--dry-run]

Artists file format: one artist per line. Blank lines and lines starting with
'#' are ignored. Append '|<override>' to disambiguate a name. The override can
be either:
  - a refined search query  (e.g. 'Geese|Geese band Brooklyn')
  - a Spotify artist URL/URI/ID  (e.g. '...|https://open.spotify.com/artist/3hxr...')

URL/URI/ID overrides bypass search entirely and look the artist up directly —
the most reliable fix when search keeps picking the wrong artist. See
lineups/example.txt for a starter file.

Auth: OAuth Authorization Code flow via spotipy. First run opens a browser to
authorize; subsequent runs use the cached refresh token in .spotify_cache.

Resolution cache: per-artist resolutions (artist ID + track URIs) are saved
to .resolution_cache.json after each successful lookup. Re-runs skip cached
entries entirely (zero API calls). Edit an artist's line in the lineup file
to invalidate just that entry, or delete the cache file to re-resolve all.
This matters because Spotify's Dev Mode daily quota is small enough that a
couple of full runs against a 100+ artist lineup can lock you out for ~20h.

Required env vars (or .env file):
    SPOTIPY_CLIENT_ID
    SPOTIPY_CLIENT_SECRET
    SPOTIPY_REDIRECT_URI  (e.g. http://127.0.0.1:8888/callback)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

# ---------- config ----------

SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"
CACHE_PATH = ".spotify_cache"                 # spotipy OAuth token cache
RESOLUTION_CACHE_PATH = ".resolution_cache.json"  # our per-artist resolution cache
SPOTIFY_ADD_BATCH = 100  # max tracks per add-to-playlist call
SEARCH_LIMIT = 10        # Spotify search max page size as of Feb 2026
RATE_LIMIT_SLEEP = 0.2   # seconds between Spotify API calls — keeps us well under 6/s burst cap

# Off-Spotify discovery sources. Used to side-step Spotify's tight daily quota.
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LISTENBRAINZ_LABS = "https://labs.api.listenbrainz.org"
LASTFM_SLEEP = 0.2   # polite delay between Last.fm calls
USER_AGENT = "spotify-playlist-maker/0.2 (+https://github.com/jimmypocock/spotify-playlist-maker)"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("playlist")


# ---------- data ----------

# Spotify artist IDs are 22-char base62. We accept any of these override forms.
SPOTIFY_ARTIST_URI_RE = re.compile(r"^spotify:artist:([A-Za-z0-9]{22})$")
SPOTIFY_ARTIST_URL_RE = re.compile(r"https?://open\.spotify\.com/artist/([A-Za-z0-9]{22})")
SPOTIFY_BARE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_artist_override(s: str) -> tuple[Optional[str], str]:
    """If s is a Spotify URI/URL/bare ID, return (artist_id, original_string).
    Otherwise return (None, s) — caller treats s as a search query."""
    s = s.strip()
    for regex in (SPOTIFY_ARTIST_URI_RE, SPOTIFY_ARTIST_URL_RE):
        m = regex.search(s)
        if m:
            return m.group(1), s
    if SPOTIFY_BARE_ID_RE.match(s):
        return s, s
    return None, s


@dataclass
class ArtistEntry:
    """One line from the artists file."""
    display_name: str               # what was on the poster, used for logs/output
    search_query: str               # used when searching by name (also for track search)
    artist_id: Optional[str] = None # set when override is a Spotify URI/URL/ID

    @classmethod
    def parse(cls, line: str) -> Optional["ArtistEntry"]:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        if "|" in line:
            display, override = (part.strip() for part in line.split("|", 1))
            artist_id, _ = parse_artist_override(override)
            # Track-search query falls back to display name when override is an ID.
            search_query = display if artist_id else override
            return cls(display_name=display, search_query=search_query, artist_id=artist_id)
        return cls(display_name=line, search_query=line)


@dataclass
class ArtistCandidate:
    """An alternative search hit kept around so the user can review mismatches."""
    artist_id: str
    name: str


@dataclass
class ResolveResult:
    entry: ArtistEntry
    artist_id: Optional[str] = None
    matched_name: Optional[str] = None
    track_uris: list[str] = field(default_factory=list)
    sample_track: Optional[str] = None  # name of top track, shown for sanity-check
    alternatives: list[ArtistCandidate] = field(default_factory=list)
    confidence: str = "high"  # "high" | "medium" | "low"
    confidence_reasons: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------- helpers ----------

def normalize_name(s: str) -> str:
    """Lowercase, strip diacritics + parentheticals + extra whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    stripped = re.sub(r"\([^)]*\)", "", no_diacritics.lower())
    return re.sub(r"\s+", " ", stripped).strip()


# ---------- resolution cache ----------
#
# Spotify's Dev Mode daily quota is small enough that you'll hit it after a
# couple of full runs against a 100+ artist lineup. Caching lets re-runs skip
# already-resolved artists entirely. The cache key encodes both display name
# AND override (search query or ID), so changing an entry's line invalidates
# its cache slot and forces a fresh resolve. To re-resolve everything, just
# delete .resolution_cache.json.

def cache_key(entry: "ArtistEntry") -> str:
    suffix = f"id:{entry.artist_id}" if entry.artist_id else f"q:{entry.search_query}"
    return f"{entry.display_name}||{suffix}"


def load_resolution_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Resolution cache at %s is unreadable; starting fresh", path)
        return {}


def save_resolution_cache(path: Path, cache: dict[str, dict]) -> None:
    """Atomic write — a crash mid-write won't corrupt the file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def cache_record(result: "ResolveResult") -> dict:
    return {
        "artist_id": result.artist_id,
        "matched_name": result.matched_name,
        "track_uris": result.track_uris,
        "sample_track": result.sample_track,
        "confidence": result.confidence,
        "confidence_reasons": result.confidence_reasons,
        "alternatives": [
            {"artist_id": a.artist_id, "name": a.name} for a in result.alternatives
        ],
    }


def hydrate_from_cache(entry: "ArtistEntry", cached: dict) -> "ResolveResult":
    return ResolveResult(
        entry=entry,
        artist_id=cached.get("artist_id"),
        matched_name=cached.get("matched_name"),
        track_uris=cached.get("track_uris", []),
        sample_track=cached.get("sample_track"),
        alternatives=[
            ArtistCandidate(artist_id=a["artist_id"], name=a["name"])
            for a in cached.get("alternatives", [])
        ],
        confidence=cached.get("confidence", "high"),
        confidence_reasons=cached.get("confidence_reasons", []),
    )


# ---------- spotify helpers ----------

def get_client() -> spotipy.Spotify:
    """Return an authenticated spotipy client (Authorization Code flow)."""
    load_dotenv()
    missing = [
        v for v in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI")
        if not os.getenv(v)
    ]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}. See README.")

    auth = SpotifyOAuth(
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, retries=3)


def resolve_artist(sp: spotipy.Spotify, entry: ArtistEntry) -> ResolveResult:
    """Resolve an entry to a Spotify artist. If the entry has a direct ID
    override, trust it and skip the artist lookup entirely (the user already
    verified the URL — we'd only be paying an API call to fetch the canonical
    name for display purposes). Otherwise search by name, take the top hit,
    and keep next 4 as alternatives.

    Note: as of Feb 2026, Dev Mode apps no longer receive `popularity`,
    `followers`, or `genres` on artist objects — so we only have name and ID
    to work with for confidence scoring.
    """
    result = ResolveResult(entry=entry)
    if entry.artist_id:
        # Skip sp.artist() — trust user-provided ID, use display name as matched.
        result.artist_id = entry.artist_id
        result.matched_name = entry.display_name
        return result
    try:
        resp = sp.search(q=entry.search_query, type="artist", limit=SEARCH_LIMIT)
        time.sleep(RATE_LIMIT_SLEEP)
        items = resp.get("artists", {}).get("items", [])
        if not items:
            result.error = "no results"
            return result
        top = items[0]
        result.artist_id = top["id"]
        result.matched_name = top["name"]
        result.alternatives = [
            ArtistCandidate(artist_id=a["id"], name=a["name"])
            for a in items[1:5]
        ]
    except spotipy.SpotifyException as e:
        result.error = f"search failed: {e}"
    return result


# ---------- off-Spotify discovery (Last.fm + ListenBrainz) ----------
#
# Spotify Dev Mode's daily quota is brutally tight (~150-200 calls per 24h),
# so artist/track discovery is done via free external sources when possible:
#   Last.fm artist.getTopTracks         → top track names for an artist (1 call)
#   ListenBrainz spotify-id-from-metadata → batch map (artist, track) → Spotify
#                                           track IDs in a single call
# Only the final playlist write touches Spotify's API in this path.
#
# Note: Last.fm DOES return MBIDs but they're frequently stale/invalid against
# the live MusicBrainz database, so we ignore them and use track names instead.

def _lastfm_get_top_tracks(
    artist_name: str, n: int, api_key: str
) -> list[str]:
    """Call Last.fm artist.getTopTracks. Returns ordered list of track names.
    Returns [] on any failure — caller falls back to Spotify path."""
    params = {
        "method": "artist.getTopTracks",
        "artist": artist_name,
        "api_key": api_key,
        "format": "json",
        "autocorrect": 1,
        "limit": n,
    }
    try:
        r = requests.get(
            LASTFM_API,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        time.sleep(LASTFM_SLEEP)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            log.warning("Last.fm error for %r: %s", artist_name, data.get("message"))
            return []
        raw_tracks = data.get("toptracks", {}).get("track", [])
        return [t["name"] for t in raw_tracks if t.get("name")]
    except (requests.RequestException, ValueError) as e:
        log.warning("Last.fm request failed for %r: %s", artist_name, e)
        return []


def _listenbrainz_map_tracks(
    artist_name: str, track_names: list[str]
) -> dict[str, Optional[str]]:
    """Batch-map (artist_name, track_name) pairs to Spotify track URIs via
    ListenBrainz labs `spotify-id-from-metadata`. One POST request handles
    every track for an artist. Returns {track_name: spotify_uri_or_None}."""
    if not track_names:
        return {}
    url = f"{LISTENBRAINZ_LABS}/spotify-id-from-metadata/json"
    payload = [
        {"artist_name": artist_name, "release_name": "", "track_name": t}
        for t in track_names
    ]
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        time.sleep(LASTFM_SLEEP)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("ListenBrainz mapping failed for %r: %s", artist_name, e)
        return {t: None for t in track_names}

    out: dict[str, Optional[str]] = {t: None for t in track_names}
    for entry in data:
        name = entry.get("track_name")
        ids = entry.get("spotify_track_ids") or []
        if name in out and ids:
            out[name] = f"spotify:track:{ids[0]}"
    return out


def fetch_top_tracks_via_lastfm(
    artist_name: str, n: int, api_key: str
) -> tuple[list[str], Optional[str]]:
    """End-to-end: artist display name → up to n Spotify track URIs.
    Over-requests from Last.fm (2x n) so unmapped tracks don't shrink the
    result. Two HTTP calls total per artist: Last.fm + ListenBrainz batch.
    Returns (track_uris, first_track_name)."""
    track_names = _lastfm_get_top_tracks(artist_name, n=n * 2, api_key=api_key)
    if not track_names:
        return [], None
    mapping = _listenbrainz_map_tracks(artist_name, track_names)
    uris: list[str] = []
    first_name: Optional[str] = None
    seen: set[str] = set()
    for name in track_names:  # preserve Last.fm's popularity order
        uri = mapping.get(name)
        if not uri or uri in seen:
            continue
        uris.append(uri)
        seen.add(uri)
        if first_name is None:
            first_name = name
        if len(uris) >= n:
            break
    return uris, first_name


def fetch_top_tracks(
    sp: spotipy.Spotify,
    result: ResolveResult,
    n: int,
    market: str,
    fast: bool = False,
) -> None:
    """Populate result.track_uris with up to n popular tracks for the artist.

    Resolution order:
    1. Last.fm artist.getTopTracks + ListenBrainz MBID→Spotify mapping (if
       LASTFM_API_KEY env var is set). Free, no Spotify quota cost.
    2. Spotify search type=track filtered by artist_id, sorted by popularity.
    3. Album walk via /artists/{id}/albums + /albums/{id}/tracks for whatever
       slots remain. Expensive (~5 Spotify calls per artist), so --fast limits
       this to ID-overridden entries.

    Each step only runs if the previous one didn't return n tracks.
    The Last.fm path doesn't require a Spotify artist_id — useful when
    Spotify is rate-limited and resolve_artist couldn't get one.
    """
    lastfm_key = os.getenv("LASTFM_API_KEY")
    if not result.artist_id and not lastfm_key:
        return

    track_uris: list[str] = []
    sample: Optional[str] = None

    # Path 1: Last.fm + ListenBrainz (no Spotify cost, no artist_id needed)
    if lastfm_key:
        lastfm_uris, lastfm_sample = fetch_top_tracks_via_lastfm(
            result.entry.display_name, n, api_key=lastfm_key,
        )
        if lastfm_uris:
            track_uris = lastfm_uris
            sample = lastfm_sample
            # Last.fm searched by display_name. If Spotify's resolve_artist
            # had picked a different same-name artist, the matched_name would
            # be misleading. Override to match what Last.fm actually used.
            result.matched_name = result.entry.display_name

    # Path 2: Spotify search-by-track (requires artist_id)
    if result.artist_id and len(track_uris) < n:
        try:
            resp = sp.search(
                q=result.entry.search_query,
                type="track",
                limit=SEARCH_LIMIT,
                market=market,
            )
            time.sleep(RATE_LIMIT_SLEEP)
            items = resp.get("tracks", {}).get("items", [])
            own = [
                t for t in items
                if any(a.get("id") == result.artist_id for a in t.get("artists", []))
            ]
            own.sort(key=lambda t: t.get("popularity", 0), reverse=True)
            seen = set(track_uris)
            for t in own:
                if t["uri"] in seen:
                    continue
                track_uris.append(t["uri"])
                seen.add(t["uri"])
                if sample is None:
                    sample = t.get("name")
                if len(track_uris) >= n:
                    break
        except spotipy.SpotifyException as e:
            result.error = f"track search failed: {e}"

    # Fill out the rest via album walk if search underdelivered. The walk is
    # expensive (~5 API calls per artist), so under --fast we limit it to
    # ID-overridden entries — where the user explicitly told us the right
    # artist, typically tiny acts whose search returns ~nothing without a
    # name boost. Search-resolved artists in --fast just take what search
    # gave them; that's usually 7-10 tracks anyway for any halfway-popular act.
    should_walk = (
        len(track_uris) < n
        and result.artist_id
        and (result.entry.artist_id or not fast)
    )
    if should_walk:
        seen_uris = set(track_uris)
        walked_uris, walked_sample = fetch_tracks_via_albums(
            sp, result.artist_id, n, market, skip_uris=seen_uris,
        )
        for uri in walked_uris:
            if uri in seen_uris:
                continue
            track_uris.append(uri)
            seen_uris.add(uri)
            if len(track_uris) >= n:
                break
        if sample is None:
            sample = walked_sample

    if track_uris:
        result.track_uris = track_uris
        result.sample_track = sample
        result.error = None
    else:
        result.error = result.error or "no playable tracks credited to this artist in market"


def fetch_tracks_via_albums(
    sp: spotipy.Spotify,
    artist_id: str,
    n: int,
    market: str,
    skip_uris: Optional[set[str]] = None,
) -> tuple[list[str], Optional[str]]:
    """Walk the artist's albums to collect their own tracks. Used to fill out
    thin search results or as a full fallback when search-by-name finds nothing.

    `skip_uris` lets the caller exclude tracks already collected via another
    path (typically search), so the walk only adds NEW tracks beyond those.
    """
    skip_uris = skip_uris or set()
    track_uris: list[str] = []
    first_name: Optional[str] = None
    seen_names: set[str] = set()

    albums: list[dict] = []
    offset = 0
    try:
        while True:
            # Including 'compilation' catches artists whose only catalog
            # entries are EPs Spotify classifies oddly. Pass country=market
            # explicitly — spotipy sends `country=None` literally otherwise,
            # and Spotify rejects it as a malformed param.
            # Feb 2026: artist_albums max limit reduced from 50 to 10.
            page = sp.artist_albums(
                artist_id,
                album_type="album,single,compilation",
                country=market,
                limit=10,
                offset=offset,
            )
            time.sleep(RATE_LIMIT_SLEEP)
            albums.extend(page.get("items", []))
            if not page.get("next"):
                break
            offset += 10
    except spotipy.SpotifyException as e:
        log.warning("artist_albums failed for %s: %s", artist_id, e)
        return track_uris, first_name

    for album in albums:
        try:
            tracks_resp = sp.album_tracks(album["id"], market=market, limit=50)
            time.sleep(RATE_LIMIT_SLEEP)
        except spotipy.SpotifyException as e:
            log.warning("album_tracks failed for album %s: %s", album.get("id"), e)
            continue
        for t in tracks_resp.get("items", []):
            if not any(a.get("id") == artist_id for a in t.get("artists", [])):
                continue
            if t["uri"] in skip_uris:
                continue
            key = t["name"].lower().strip()
            if key in seen_names:
                continue
            seen_names.add(key)
            track_uris.append(t["uri"])
            if first_name is None:
                first_name = t.get("name")
            if len(track_uris) >= n:
                return track_uris, first_name
    return track_uris, first_name


def score_confidence(result: ResolveResult) -> None:
    """Tag result.confidence as high/medium/low.

    Dev Mode apps don't get popularity/followers/genres anymore (Feb 2026),
    so only two signals remain: literal name distance, and same-name
    ambiguity (multiple Spotify artists sharing the matched name)."""
    if not result.artist_id:
        return
    reasons: list[str] = []
    severity = 0  # 0=high, 1=medium, 2=low

    norm_input = normalize_name(result.entry.display_name)
    norm_match = normalize_name(result.matched_name or "")

    # Signal 1: name match (after diacritic/case/parenthetical normalization).
    # Substring containment counts as a match — handles "Geese" ↔ "Geese (band)".
    name_matches = (
        norm_input == norm_match
        or norm_input in norm_match
        or norm_match in norm_input
    )
    if not name_matches:
        reasons.append(f"name mismatch ('{result.matched_name}')")
        severity = max(severity, 2)

    # Signal 2: same-name ambiguity — alternative hits share the matched name.
    same_name_alts = [
        a for a in result.alternatives if normalize_name(a.name) == norm_match
    ]
    if same_name_alts:
        reasons.append(
            f"{len(same_name_alts) + 1} artists named '{result.matched_name}'"
        )
        severity = max(severity, 1)

    result.confidence = ["high", "medium", "low"][severity]
    result.confidence_reasons = reasons


def get_or_create_playlist(
    sp: spotipy.Spotify, name: str, description: str, public: bool
) -> str:
    """Return playlist ID. Creates a new one or reuses an existing one with same name."""
    user_id = sp.current_user()["id"]
    time.sleep(RATE_LIMIT_SLEEP)
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        time.sleep(RATE_LIMIT_SLEEP)
        for pl in page["items"]:
            if pl["name"] == name and pl["owner"]["id"] == user_id:
                log.info("Reusing existing playlist '%s' (%s)", name, pl["id"])
                return pl["id"]
        if not page["next"]:
            break
        offset += 50

    # Spotify deprecated POST /users/{id}/playlists for Dev Mode apps in Feb
    # 2026. Use POST /me/playlists instead. spotipy doesn't expose this
    # endpoint as a public method, so we drop down to its internal _post.
    pl = sp._post(
        "me/playlists",
        payload={
            "name": name,
            "public": public,
            "collaborative": False,
            "description": description,
        },
    )
    time.sleep(RATE_LIMIT_SLEEP)
    log.info("Created playlist '%s' (%s)", name, pl["id"])
    return pl["id"]


def add_tracks_in_batches(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Add tracks to playlist, respecting Spotify's 100-per-call limit."""
    for i in range(0, len(uris), SPOTIFY_ADD_BATCH):
        batch = uris[i : i + SPOTIFY_ADD_BATCH]
        sp.playlist_add_items(playlist_id, batch)
        log.info("  added %d/%d", min(i + SPOTIFY_ADD_BATCH, len(uris)), len(uris))
        time.sleep(RATE_LIMIT_SLEEP)


# ---------- output ----------

def load_artists(path: Path) -> list[ArtistEntry]:
    entries: list[ArtistEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = ArtistEntry.parse(raw)
        if entry:
            entries.append(entry)
    return entries


def print_review_block(results: list[ResolveResult], artists_file: Path) -> None:
    """Print a per-artist review section for medium/low confidence matches."""
    needs_review = [r for r in results if r.artist_id and r.confidence != "high"]
    if not needs_review:
        return
    print(f"\nReview needed ({len(needs_review)}):\n")
    for r in needs_review:
        tag = "LOW " if r.confidence == "low" else "MED "
        sample = f"top track: {r.sample_track!r}" if r.sample_track else "no top track"
        print(f"  ⚠ {tag} {r.entry.display_name}")
        print(f"      matched: '{r.matched_name}'  ({sample})")
        print(f"      reasons: {'; '.join(r.confidence_reasons)}")
        if r.alternatives:
            print(f"      alternatives:")
            for alt in r.alternatives[:3]:
                print(f"        • {alt.name}  (id: {alt.artist_id})")
        print(f"      fix: in {artists_file}, change line to "
              f"'{r.entry.display_name}|<more specific search query>'")
        print()


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artists", required=True, type=Path, help="path to artists.txt")
    parser.add_argument("--name", required=True, help="playlist name")
    parser.add_argument("--top", type=int, default=10, help="top N tracks per artist (default 10)")
    parser.add_argument("--market", default="US", help="ISO market code (default US)")
    parser.add_argument("--description", default="", help="playlist description")
    parser.add_argument("--public", action="store_true", help="make playlist public (default private)")
    parser.add_argument("--replace", action="store_true", help="clear an existing same-named playlist before adding (default: append)")
    parser.add_argument("--fast", action="store_true", help="skip album-walk fill-in for search-resolved artists (still walks ID-overridden ones) — saves ~5 API calls/artist, accepts thin search results")
    parser.add_argument("--dry-run", action="store_true", help="resolve artists & print plan, don't write")
    args = parser.parse_args()

    if not args.artists.exists():
        sys.exit(f"Artists file not found: {args.artists}")

    entries = load_artists(args.artists)
    log.info("Loaded %d artists from %s", len(entries), args.artists)

    cache_path = Path(RESOLUTION_CACHE_PATH)
    cache = load_resolution_cache(cache_path)
    if cache:
        log.info("Loaded resolution cache: %d entries from %s", len(cache), cache_path)

    sp = get_client()
    log.info("Authenticated as %s", sp.current_user()["display_name"])
    time.sleep(RATE_LIMIT_SLEEP)

    # Phase 1: resolve every artist and collect top tracks
    results: list[ResolveResult] = []
    cache_hits = 0
    for i, entry in enumerate(entries, 1):
        key = cache_key(entry)
        if key in cache:
            r = hydrate_from_cache(entry, cache[key])
            cache_hits += 1
            log.info(
                "[%d/%d] %s  ← cached (%d tracks)",
                i, len(entries), entry.display_name, len(r.track_uris),
            )
            results.append(r)
            continue

        log.info("[%d/%d] %s", i, len(entries), entry.display_name)
        r = resolve_artist(sp, entry)
        # Always try track-fetch. The Last.fm path works even when resolve_artist
        # failed (e.g., Spotify rate-limited) since it doesn't need a Spotify ID.
        fetch_top_tracks(sp, r, args.top, args.market, fast=args.fast)
        score_confidence(r)
        if r.track_uris:
            sample = f" (top: {r.sample_track!r})" if r.sample_track else ""
            tag = ""
            if r.confidence == "low":
                tag = " ⚠ LOW confidence"
            elif r.confidence == "medium":
                tag = " ⚠ medium confidence"
            log.info("  → %d tracks%s%s", len(r.track_uris), sample, tag)
            # Persist immediately so a mid-run crash doesn't lose progress.
            cache[key] = cache_record(r)
            save_resolution_cache(cache_path, cache)
        else:
            log.warning("  → %s", r.error or "no tracks found")
        results.append(r)

    if cache_hits:
        log.info("Cache: %d/%d hits, %d new resolutions", cache_hits, len(entries), len(entries) - cache_hits)

    # Summary
    found = [r for r in results if r.track_uris]
    failed = [r for r in results if not r.track_uris]
    total_tracks = sum(len(r.track_uris) for r in found)

    print(f"\n{'='*60}")
    print(f"Resolved: {len(found)}/{len(entries)} artists, {total_tracks} tracks")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for r in failed:
            print(f"  - {r.entry.display_name}: {r.error or 'no tracks'}")
        print("\nFix: edit artists.txt, add '|<better search query>' overrides for these.")
    print_review_block(results, args.artists)
    print(f"{'='*60}\n")

    if args.dry_run:
        log.info("Dry run complete. No playlist written.")
        return 0

    if not found:
        sys.exit("No artists resolved; nothing to write.")

    # Phase 2: write playlist
    playlist_id = get_or_create_playlist(sp, args.name, args.description, args.public)

    if args.replace:
        sp.playlist_replace_items(playlist_id, [])
        time.sleep(RATE_LIMIT_SLEEP)
        log.info("Cleared existing tracks (replace mode)")

    # Dedupe across artists by URI — when track X features both Artist A and
    # Artist B and both are in the lineup, our per-artist resolution returns X
    # twice. Spotify allows playlist duplicates, but the user doesn't want them.
    # Preserves artist order (first occurrence wins). Different versions of a
    # song have different URIs and are kept.
    all_uris: list[str] = []
    seen: set[str] = set()
    for r in found:
        for uri in r.track_uris:
            if uri in seen:
                continue
            seen.add(uri)
            all_uris.append(uri)
    add_tracks_in_batches(sp, playlist_id, all_uris)

    pl = sp.playlist(playlist_id, fields="external_urls,name")
    print(f"\n✓ Done. {pl['name']}: {pl['external_urls']['spotify']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
