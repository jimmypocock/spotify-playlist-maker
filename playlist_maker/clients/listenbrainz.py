"""ListenBrainz labs client — batch maps (artist_name, track_name) pairs to
Spotify track URIs.

We pair this with the Last.fm client: Last.fm gives us top track names, and
this maps them all to Spotify URIs in a single POST. Two HTTP calls per
artist total, both free, no quota worth worrying about.

Endpoint docs: https://labs.api.listenbrainz.org/spotify-id-from-metadata
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

LISTENBRAINZ_LABS = "https://labs.api.listenbrainz.org"
LB_SLEEP = 0.2  # polite delay
USER_AGENT = "spotify-playlist-maker/0.3 (+https://github.com/jimmypocock/spotify-playlist-maker)"

log = logging.getLogger("playlist")


def map_tracks_to_spotify(
    artist_name: str, track_names: list[str]
) -> dict[str, Optional[str]]:
    """Batch-map track names to Spotify URIs via ListenBrainz labs
    `spotify-id-from-metadata`. One POST handles every track for an artist.
    Returns {track_name: spotify_uri_or_None}."""
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
        time.sleep(LB_SLEEP)
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
