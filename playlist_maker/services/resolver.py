"""Artist-and-tracks resolution service.

Combines the three discovery sources into one cohesive flow:

1. Last.fm + ListenBrainz (free, no Spotify quota cost) when LASTFM_API_KEY
   is set. Always tried first.
2. Spotify search-by-track (filtered by artist_id, popularity-sorted).
3. Album walk via artist_albums + album_tracks for thin search results.

Each step only runs if the prior didn't return enough tracks. The Last.fm
path doesn't need a Spotify artist_id, so it works even when Spotify is
rate-limited and `resolve_artist` couldn't get one.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import spotipy

from ..clients import lastfm, listenbrainz
from ..clients.spotify import (
    list_album_tracks,
    list_artist_albums,
    search_artist,
    search_tracks,
)
from ..models import ArtistCandidate, ArtistEntry, ResolveResult

log = logging.getLogger("playlist")


def resolve_artist(sp: spotipy.Spotify, entry: ArtistEntry) -> ResolveResult:
    """Resolve an entry to a Spotify artist.

    - If the entry has a direct ID override, trust it (skip API call — the
      user already verified the URL).
    - Otherwise search by name, take the top hit, and keep next 4 as
      alternatives.

    Note: Feb 2026 stripped popularity/followers/genres from artist objects,
    so name + ID are all we have to work with for confidence scoring.
    """
    result = ResolveResult(entry=entry)
    if entry.artist_id:
        result.artist_id = entry.artist_id
        result.matched_name = entry.display_name
        return result
    try:
        items = search_artist(sp, entry.search_query)
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


def fetch_top_tracks(
    sp: spotipy.Spotify,
    result: ResolveResult,
    n: int,
    market: str,
    fast: bool = False,
) -> None:
    """Populate result.track_uris with up to n popular tracks for the artist.

    Resolution order:
    1. Last.fm + ListenBrainz (if LASTFM_API_KEY env var is set). Free.
    2. Spotify search type=track filtered by artist_id, sorted by popularity.
    3. Album walk for whatever slots remain. Expensive; --fast limits it to
       ID-overridden entries (where the user knows the right artist).

    Each step only runs if the previous one didn't return n tracks.
    """
    lastfm_key = os.getenv("LASTFM_API_KEY")
    if not result.artist_id and not lastfm_key:
        return

    track_uris: list[str] = []
    sample: Optional[str] = None

    # Path 1: Last.fm + ListenBrainz (no Spotify cost, no artist_id needed)
    if lastfm_key:
        lastfm_uris, lastfm_sample = _via_lastfm(result.entry.display_name, n, lastfm_key)
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
            items = search_tracks(sp, result.entry.search_query, market)
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

    # Path 3: album walk fills remaining slots. Expensive — under --fast only
    # runs for ID-overridden entries.
    should_walk = (
        len(track_uris) < n
        and result.artist_id
        and (result.entry.artist_id or not fast)
    )
    if should_walk:
        seen_uris = set(track_uris)
        walked_uris, walked_sample = _walk_albums(
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


# ---------- discovery helpers ----------

def _via_lastfm(
    artist_name: str, n: int, api_key: str
) -> tuple[list[str], Optional[str]]:
    """End-to-end: artist display name → up to n Spotify track URIs.
    Over-requests from Last.fm (2x n) so unmapped tracks don't shrink the
    result. Two HTTP calls total per artist: Last.fm + ListenBrainz batch."""
    track_names = lastfm.get_top_tracks(artist_name, n=n * 2, api_key=api_key)
    if not track_names:
        return [], None
    mapping = listenbrainz.map_tracks_to_spotify(artist_name, track_names)
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


def _walk_albums(
    sp: spotipy.Spotify,
    artist_id: str,
    n: int,
    market: str,
    skip_uris: Optional[set[str]] = None,
) -> tuple[list[str], Optional[str]]:
    """Walk the artist's albums to collect their own tracks. Used to fill out
    thin search results or as a full fallback when search-by-name finds nothing.

    `skip_uris` lets the caller exclude tracks already collected via another
    path so the walk only adds new tracks.
    """
    skip_uris = skip_uris or set()
    track_uris: list[str] = []
    first_name: Optional[str] = None
    seen_names: set[str] = set()

    albums = list_artist_albums(sp, artist_id, market)
    for album in albums:
        tracks_resp = list_album_tracks(sp, album["id"], market)
        for t in tracks_resp:
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
