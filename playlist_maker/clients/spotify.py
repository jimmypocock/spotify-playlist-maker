"""Spotify Web API client — auth + every Spotify call we make.

Most calls are wrapped to enforce our own RATE_LIMIT_SLEEP between requests
(keeps us comfortably below the burst cap). The Spotify daily quota is much
tighter than the burst cap and the resolution cache + Last.fm path are the
real defenses against it; this throttle is just precautionary insurance.

Feb 2026 caveats baked in:
- `POST /users/{id}/playlists` returns 403 for Dev Mode apps. Use the
  `_post("me/playlists", ...)` private spotipy method instead.
- `artist_albums` limit was reduced from 50 to 10. Pagination uses offset=10.
- `country=None` is rejected as a literal "None" param value — always pass
  a real ISO code.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Public scopes — playlist-read-private is needed for current_user_playlists,
# the rest for creating and modifying playlists.
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"
OAUTH_CACHE_PATH = ".spotify_cache"

SPOTIFY_ADD_BATCH = 100  # max tracks per add-to-playlist call
SEARCH_LIMIT = 10        # Spotify search max page size as of Feb 2026
ARTIST_ALBUMS_LIMIT = 10 # Reduced from 50 in Feb 2026
RATE_LIMIT_SLEEP = 0.2   # seconds between Spotify API calls

log = logging.getLogger("playlist")


def get_client() -> spotipy.Spotify:
    """Return an authenticated spotipy client (Authorization Code flow).
    Caller is responsible for having loaded .env beforehand."""
    missing = [
        v for v in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI")
        if not os.getenv(v)
    ]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}. See README.")

    auth = SpotifyOAuth(
        scope=SCOPES,
        cache_path=OAUTH_CACHE_PATH,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, retries=3)


def search_artist(sp: spotipy.Spotify, query: str) -> list[dict]:
    """Return up to SEARCH_LIMIT artist dicts (in Spotify's relevance order)."""
    resp = sp.search(q=query, type="artist", limit=SEARCH_LIMIT)
    time.sleep(RATE_LIMIT_SLEEP)
    return resp.get("artists", {}).get("items", [])


def search_tracks(sp: spotipy.Spotify, query: str, market: str) -> list[dict]:
    """Return up to SEARCH_LIMIT track dicts matching the query in market."""
    resp = sp.search(q=query, type="track", limit=SEARCH_LIMIT, market=market)
    time.sleep(RATE_LIMIT_SLEEP)
    return resp.get("tracks", {}).get("items", [])


def list_artist_albums(sp: spotipy.Spotify, artist_id: str, market: str) -> list[dict]:
    """Paginated list of an artist's own + compilation albums in market.
    Empty list on SpotifyException (logged)."""
    albums: list[dict] = []
    offset = 0
    try:
        while True:
            page = sp.artist_albums(
                artist_id,
                album_type="album,single,compilation",
                country=market,
                limit=ARTIST_ALBUMS_LIMIT,
                offset=offset,
            )
            time.sleep(RATE_LIMIT_SLEEP)
            albums.extend(page.get("items", []))
            if not page.get("next"):
                break
            offset += ARTIST_ALBUMS_LIMIT
    except spotipy.SpotifyException as e:
        log.warning("artist_albums failed for %s: %s", artist_id, e)
    return albums


def list_album_tracks(sp: spotipy.Spotify, album_id: str, market: str) -> list[dict]:
    """Album's tracks in market. Empty list on SpotifyException (logged)."""
    try:
        resp = sp.album_tracks(album_id, market=market, limit=50)
        time.sleep(RATE_LIMIT_SLEEP)
        return resp.get("items", [])
    except spotipy.SpotifyException as e:
        log.warning("album_tracks failed for album %s: %s", album_id, e)
        return []


def find_playlist_by_name(sp: spotipy.Spotify, name: str) -> Optional[dict]:
    """Search the current user's playlists for an exact name match. Returns
    the first match or None."""
    user_id = sp.current_user()["id"]
    time.sleep(RATE_LIMIT_SLEEP)
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        time.sleep(RATE_LIMIT_SLEEP)
        for pl in page["items"]:
            if pl["name"] == name and pl["owner"]["id"] == user_id:
                return pl
        if not page["next"]:
            return None
        offset += 50


def create_playlist(sp: spotipy.Spotify, name: str, description: str, public: bool) -> dict:
    """Create a new playlist via POST /me/playlists. (POST /users/{id}/playlists
    returns 403 for Dev Mode apps as of Feb 2026; spotipy's user_playlist_create
    calls the old path, so we drop down to its internal _post.)"""
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
    return pl


def replace_playlist_items(sp: spotipy.Spotify, playlist_id: str) -> None:
    """Clear all tracks from a playlist."""
    sp.playlist_replace_items(playlist_id, [])
    time.sleep(RATE_LIMIT_SLEEP)


def add_tracks(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Add tracks to playlist in 100-URI batches (Spotify's limit)."""
    for i in range(0, len(uris), SPOTIFY_ADD_BATCH):
        batch = uris[i : i + SPOTIFY_ADD_BATCH]
        sp.playlist_add_items(playlist_id, batch)
        log.info("  added %d/%d", min(i + SPOTIFY_ADD_BATCH, len(uris)), len(uris))
        time.sleep(RATE_LIMIT_SLEEP)


def get_playlist_url(sp: spotipy.Spotify, playlist_id: str) -> tuple[str, str]:
    """Returns (name, public_url) for a playlist."""
    pl = sp.playlist(playlist_id, fields="external_urls,name")
    time.sleep(RATE_LIMIT_SLEEP)
    return pl["name"], pl["external_urls"]["spotify"]
