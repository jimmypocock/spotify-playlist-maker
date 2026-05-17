"""Command-line entry point and main orchestration loop."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .cache import (
    RESOLUTION_CACHE_PATH,
    cache_key,
    cache_record,
    hydrate_from_cache,
    load_resolution_cache,
    save_resolution_cache,
)
from .clients.spotify import RATE_LIMIT_SLEEP, get_client, get_playlist_url
from .confidence import print_review_block, score_confidence
from .models import ArtistEntry, ResolveResult
from .services.playlist import get_or_create_playlist, write_playlist
from .services.resolver import fetch_setlist_tracks, fetch_top_tracks, resolve_artist

DOCSTRING = """
spotify_playlist.py — Build a Spotify playlist of the top N tracks for every
artist in a list.

Usage:
    python spotify_playlist.py --artists lineups/<name>.txt --name "<playlist>" \\
        [--top 10] [--market US] [--description "..."] [--public] [--dry-run]

Artists file format: one artist per line. Append '|<override>' to disambiguate
a name. The override can be either:
  - a refined search query  (e.g. 'Geese|Geese band Brooklyn')
  - a Spotify artist URL/URI/ID  (e.g. '...|https://open.spotify.com/artist/3hxr...')

URL/URI/ID overrides bypass search entirely and look the artist up directly —
the most reliable fix when search keeps picking the wrong artist. See
lineups/example.txt for a starter file.

Auth: OAuth Authorization Code flow via spotipy. First run opens a browser to
authorize; subsequent runs use the cached refresh token in .spotify_cache.

Resolution cache: per-artist resolutions are saved to .resolution_cache.json
after each successful lookup. Re-runs skip cached entries entirely (zero API
calls). Edit an artist's line in the lineup file to invalidate just that
entry, or delete the cache file to re-resolve all.

Required env vars (or .env file):
    SPOTIPY_CLIENT_ID
    SPOTIPY_CLIENT_SECRET
    SPOTIPY_REDIRECT_URI  (e.g. http://127.0.0.1:8888/callback)

Optional but strongly recommended (sidesteps Spotify's daily quota):
    LASTFM_API_KEY  (free, get one at https://www.last.fm/api/account/create)
"""

log = logging.getLogger("playlist")


def _configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )


def load_artists(path: Path) -> list[ArtistEntry]:
    entries: list[ArtistEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = ArtistEntry.parse(raw)
        if entry:
            entries.append(entry)
    return entries


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=DOCSTRING,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Mode selection — exactly one of these two must be provided.
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--artists", type=Path,
                      help="path to a lineup file (top-tracks mode)")
    mode.add_argument("--setlist", metavar="ARTIST",
                      help="artist name to build a playlist of their recent live setlists "
                           "(concert mode — requires SETLISTFM_API_KEY in .env)")

    parser.add_argument("--name", help="playlist name (auto-derived in setlist mode if omitted)")
    parser.add_argument("--top", type=int, default=10, help="top N tracks per artist (default 10, top-tracks mode only)")
    parser.add_argument("--shows", type=int, default=10,
                        help="number of recent setlists to pull (default 10, setlist mode only)")
    parser.add_argument("--market", default="US", help="ISO market code (default US)")
    parser.add_argument("--description", default="", help="playlist description")
    parser.add_argument("--public", action="store_true", help="make playlist public (default private)")
    parser.add_argument("--replace", action="store_true",
                        help="clear an existing same-named playlist before adding (default: append)")
    parser.add_argument("--fast", action="store_true",
                        help="skip album-walk fill-in for search-resolved artists "
                             "(still walks ID-overridden ones) — saves ~5 API calls/artist, "
                             "accepts thin search results")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve artists & print plan, don't write")
    args = parser.parse_args(argv)

    # Top-tracks mode requires --name (no good default); setlist mode auto-derives it.
    if args.artists and not args.name:
        parser.error("--name is required when using --artists")
    return args


def _resolve_all(
    sp, entries: list[ArtistEntry], cache_path: Path, top: int, market: str, fast: bool
) -> list[ResolveResult]:
    """Run the resolution loop over every entry, using the cache where possible
    and persisting each new resolution as it succeeds."""
    cache = load_resolution_cache(cache_path)
    if cache:
        log.info("Loaded resolution cache: %d entries from %s", len(cache), cache_path)

    results: list[ResolveResult] = []
    cache_hits = 0
    for i, entry in enumerate(entries, 1):
        key = cache_key(entry)
        if key in cache:
            r = hydrate_from_cache(entry, cache[key])
            cache_hits += 1
            log.info("[%d/%d] %s  ← cached (%d tracks)",
                     i, len(entries), entry.display_name, len(r.track_uris))
            results.append(r)
            continue

        log.info("[%d/%d] %s", i, len(entries), entry.display_name)
        r = resolve_artist(sp, entry)
        # Always try track-fetch. Last.fm path works without a Spotify ID,
        # so it succeeds even when resolve_artist failed (e.g. rate-limited).
        fetch_top_tracks(sp, r, top, market, fast=fast)
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
        log.info("Cache: %d/%d hits, %d new resolutions",
                 cache_hits, len(entries), len(entries) - cache_hits)
    return results


def _print_summary(results: list[ResolveResult], artists_file: Path) -> None:
    found = [r for r in results if r.track_uris]
    failed = [r for r in results if not r.track_uris]
    total_tracks = sum(len(r.track_uris) for r in found)

    print(f"\n{'=' * 60}")
    print(f"Resolved: {len(found)}/{len(results)} artists, {total_tracks} tracks")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for r in failed:
            print(f"  - {r.entry.display_name}: {r.error or 'no tracks'}")
        print("\nFix: edit artists.txt, add '|<better search query>' overrides for these.")
    print_review_block(results, artists_file)
    print(f"{'=' * 60}\n")


def main() -> int:
    _configure_logging()
    load_dotenv(".env")

    args = _parse_args()

    # ---------- mode dispatch ----------
    sp = None  # Spotify client — only authed when we actually need it
    if args.setlist:
        setlist_key = os.getenv("SETLISTFM_API_KEY")
        if not setlist_key:
            sys.exit("SETLISTFM_API_KEY not set in .env — required for setlist mode.")
        log.info("Concert mode: fetching last %d setlists for %r", args.shows, args.setlist)
        results = [fetch_setlist_tracks(args.setlist, args.shows, setlist_key)]
        # Auto-derive name if omitted
        if not args.name:
            canonical = results[0].matched_name or args.setlist
            args.name = f"{canonical} — Recent Live"
            log.info("Playlist name: %r (auto-derived)", args.name)
        _print_summary(results, Path(f"<setlist:{args.setlist}>"))
    else:
        if not args.artists.exists():
            sys.exit(f"Artists file not found: {args.artists}")
        entries = load_artists(args.artists)
        log.info("Loaded %d artists from %s", len(entries), args.artists)

        sp = get_client()
        log.info("Authenticated as %s", sp.current_user()["display_name"])
        time.sleep(RATE_LIMIT_SLEEP)
        # Phase 1: resolve every artist and collect top tracks
        results = _resolve_all(
            sp, entries, Path(RESOLUTION_CACHE_PATH),
            args.top, args.market, fast=args.fast,
        )
        _print_summary(results, args.artists)

    if args.dry_run:
        log.info("Dry run complete. No playlist written.")
        return 0

    found = [r for r in results if r.track_uris]
    if not found:
        sys.exit("Nothing resolved; nothing to write.")

    # Setlist mode skipped Spotify auth above (discovery doesn't need it).
    # Now we need it for the write phase.
    if sp is None:
        sp = get_client()
        log.info("Authenticated to Spotify as %s", sp.current_user()["display_name"])
        time.sleep(RATE_LIMIT_SLEEP)

    # Phase 2: write playlist
    playlist_id = get_or_create_playlist(sp, args.name, args.description, args.public)

    # Dedupe across artists: when track X features both Artist A and Artist B
    # and both are in the lineup, our per-artist resolution returns X twice.
    # Different versions of a song have different URIs and are kept.
    all_uris: list[str] = []
    seen: set[str] = set()
    for r in found:
        for uri in r.track_uris:
            if uri in seen:
                continue
            seen.add(uri)
            all_uris.append(uri)

    write_playlist(sp, playlist_id, all_uris, replace=args.replace)

    name, url = get_playlist_url(sp, playlist_id)
    print(f"\n✓ Done. {name}: {url}")
    return 0
