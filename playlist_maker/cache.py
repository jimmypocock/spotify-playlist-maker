"""Per-artist resolution cache.

Spotify Dev Mode's daily quota is small enough that re-resolving every
artist on each run is impractical. This cache stores (artist_id,
matched_name, track_uris, sample_track, confidence, alternatives) keyed by
display name + override. The cache is persisted after every successful
resolution — not at end of run — so a mid-run crash never loses progress.

To force a full re-resolve, delete .resolution_cache.json. To re-resolve a
single entry, change its line in the artists file (the cache key encodes
the override, so any change invalidates that slot).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import ArtistCandidate, ArtistEntry, ResolveResult

log = logging.getLogger("playlist")


def cache_key(entry: ArtistEntry) -> str:
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


def cache_record(result: ResolveResult) -> dict:
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


def hydrate_from_cache(entry: ArtistEntry, cached: dict) -> ResolveResult:
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
