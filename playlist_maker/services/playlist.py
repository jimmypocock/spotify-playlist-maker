"""Playlist write phase — get-or-create + replace + batched add."""

from __future__ import annotations

import logging

import spotipy

from ..clients.spotify import (
    add_tracks,
    create_playlist,
    find_playlist_by_name,
    replace_playlist_items,
)

log = logging.getLogger("playlist")


def get_or_create_playlist(
    sp: spotipy.Spotify, name: str, description: str, public: bool
) -> str:
    """Return playlist ID. Reuses an existing one with the same name owned by
    the current user, or creates a new one if none found."""
    existing = find_playlist_by_name(sp, name)
    if existing:
        log.info("Reusing existing playlist '%s' (%s)", name, existing["id"])
        return existing["id"]
    pl = create_playlist(sp, name, description, public)
    log.info("Created playlist '%s' (%s)", name, pl["id"])
    return pl["id"]


def write_playlist(
    sp: spotipy.Spotify,
    playlist_id: str,
    track_uris: list[str],
    replace: bool,
) -> None:
    """Write tracks to a playlist. If `replace`, clear it first; otherwise append.
    Dedupes URIs while preserving order (first occurrence wins)."""
    if replace:
        replace_playlist_items(sp, playlist_id)
        log.info("Cleared existing tracks (replace mode)")

    # Dedup is also done at the cli level (across artists) but doing it here
    # too is cheap insurance against callers forgetting.
    deduped: list[str] = []
    seen: set[str] = set()
    for uri in track_uris:
        if uri in seen:
            continue
        seen.add(uri)
        deduped.append(uri)

    add_tracks(sp, playlist_id, deduped)
