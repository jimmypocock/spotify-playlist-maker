"""Last.fm artist.getTopTracks client — primary discovery source when
LASTFM_API_KEY is configured.

Note: Last.fm returns MBIDs for tracks, but they're frequently stale/invalid
against the live MusicBrainz database (404 on lookup). We ignore them and
use track names instead — ListenBrainz's spotify-id-from-metadata endpoint
takes (artist_name, track_name) directly.
"""

from __future__ import annotations

import logging
import time

import requests

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LASTFM_SLEEP = 0.2  # polite delay between calls
USER_AGENT = "spotify-playlist-maker/0.3 (+https://github.com/jimmypocock/spotify-playlist-maker)"

log = logging.getLogger("playlist")


def get_top_tracks(artist_name: str, n: int, api_key: str) -> list[str]:
    """Call Last.fm artist.getTopTracks with autocorrect. Returns track names
    in popularity order. Returns [] on any failure — caller falls back."""
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
