"""Setlist.fm REST API client — search artists, fetch recent setlists.

Used by the "concert mode" — `python spotify_playlist.py --setlist <artist>`
fetches the most recent live setlists for an artist and builds a playlist of
what they're actually playing on tour right now.

Auth: API key in `x-api-key` header. Key is free + instant at
https://www.setlist.fm/settings/api. Default rate limit is 2 req/sec,
1440/day — plenty for occasional use.

Date format quirk: Setlist.fm uses `dd-MM-yyyy` (European), not ISO.

Response shape quirks:
- Cover songs: `song.cover.name` is the *original* artist; the performer
  is still the setlist's top-level `artist`. Empty `{}` when not a cover.
- `song.tape = true` means walk-on/intro music (skip).
- Some recent setlists return empty `set: []` (artist played but tracklist
  not entered yet) — common, just skip them.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

SETLISTFM_API = "https://api.setlist.fm/rest/1.0"
SETLISTFM_SLEEP = 1.1  # 2 req/sec is enforced strictly; 1.1s gives a safe margin
USER_AGENT = "spotify-playlist-maker/0.3 (+https://github.com/jimmypocock/spotify-playlist-maker)"

log = logging.getLogger("playlist")


def _headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Accept": "application/json",
        "Accept-Language": "en",
        "User-Agent": USER_AGENT,
    }


def search_artist_mbid(artist_name: str, api_key: str) -> Optional[tuple[str, str]]:
    """Search for an artist by name. Returns (mbid, canonical_name) of the top
    relevance-sorted match, or None if no results."""
    try:
        r = requests.get(
            f"{SETLISTFM_API}/search/artists",
            params={"artistName": artist_name, "sort": "relevance"},
            headers=_headers(api_key),
            timeout=10,
        )
        time.sleep(SETLISTFM_SLEEP)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        items = r.json().get("artist", [])
        if not items:
            return None
        top = items[0]
        return top["mbid"], top["name"]
    except (requests.RequestException, ValueError) as e:
        log.warning("Setlist.fm artist search failed for %r: %s", artist_name, e)
        return None


def fetch_recent_setlists(
    artist_mbid: str, max_setlists: int, api_key: str
) -> list[dict]:
    """Fetch up to max_setlists most-recent setlists for the artist (newest
    first, past dates only — skips future-scheduled shows). 20 per page;
    paginates as needed. Empty/no-songs-yet setlists are filtered out."""
    setlists: list[dict] = []
    page = 1
    while len(setlists) < max_setlists:
        try:
            r = requests.get(
                f"{SETLISTFM_API}/artist/{artist_mbid}/setlists",
                params={"p": page},
                headers=_headers(api_key),
                timeout=10,
            )
            time.sleep(SETLISTFM_SLEEP)
            if r.status_code == 404:
                break
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Setlist.fm setlists fetch failed for %s page %d: %s",
                        artist_mbid, page, e)
            break

        page_items = data.get("setlist", [])
        if not page_items:
            break

        for sl in page_items:
            # Skip setlists with no songs entered yet (recent shows often have
            # an empty tracklist for a day or two)
            sets = sl.get("sets", {}).get("set", [])
            song_count = sum(len(s.get("song", [])) for s in sets)
            if song_count == 0:
                continue
            setlists.append(sl)
            if len(setlists) >= max_setlists:
                break

        total_pages = (data.get("total", 0) + data.get("itemsPerPage", 20) - 1) // max(data.get("itemsPerPage", 20), 1)
        if page >= total_pages:
            break
        page += 1

    return setlists


def extract_songs(setlist: dict) -> list[tuple[str, str, Optional[str]]]:
    """Pull every performed song from a setlist as
    (song_title, performer, original_artist_or_None).

    `original_artist` is set only for covers (Setlist.fm's `cover.name`).
    Caller's lookup strategy: try `performer` first (in case Spotify has
    their recording of the cover), fall back to `original_artist` for the
    studio original.

    Skips `tape: true` entries (walk-on/intro music, not actually performed).
    """
    out: list[tuple[str, str, Optional[str]]] = []
    performer = setlist.get("artist", {}).get("name", "")
    for s in setlist.get("sets", {}).get("set", []):
        for song in s.get("song", []):
            if song.get("tape"):
                continue
            name = song.get("name")
            if not name:
                continue
            cover_artist = song.get("cover", {}).get("name") if song.get("cover") else None
            out.append((name, performer, cover_artist))
    return out
