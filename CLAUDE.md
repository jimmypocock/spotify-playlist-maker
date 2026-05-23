# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project intent

A **general-purpose Spotify playlist maker** (`spotify_playlist.py`). Started as a one-off ACL 2026 script (originally `festival_playlist.py`) and has been generalized for any list of artists. Public repo at `https://github.com/jimmypocock/spotify-playlist-maker`.

Strict separation between generic and specific content:

- **Generic / agnostic code** (script, auth, search, batching, CLI plumbing, the `lineups/example.txt`) — committed to GitHub.
- **Specific inputs and caches** (`lineups/*.txt` other than `example.txt`, `.resolution_cache.json`, `.spotify_cache`, `.env`) — gitignored, local-only.

When adding features, default to making them reusable across input lists. When adding files, decide explicitly whether each one is generic (committed) or specific (gitignored, in `lineups/` or hidden at root).

A Claude Code plugin wraps the CLI: `skills/playlist/SKILL.md` defines the `/playlist` slash command, packaged with `.claude-plugin/plugin.json`. Distribution is via Claude Code's plugin marketplace — users `/plugin install spotify-playlist-maker`, no clone needed. See "Skill layer" section below.

## Commands

Setup (one-time):
```bash
python -m venv .venv && source .venv/bin/activate
pip install spotipy python-dotenv
cp .env.example .env  # then fill in client id/secret
```

Run:
```bash
# Always dry-run first to triage name mismatches
python spotify_playlist.py --artists lineups/<name>.txt --name "<playlist>" --dry-run

# Real run
python spotify_playlist.py --artists lineups/<name>.txt --name "<playlist>" \
    --top 10 --description "..."
```

There is no test suite, linter, or build step configured. The script exits non-zero on missing env vars or empty artist file.

## Architecture

Single-file script with a deliberate two-phase flow in `main()`:

1. **Resolve phase** — for each `ArtistEntry`, call `resolve_artist()` (search → top match) then `fetch_top_tracks()` (artist_top_tracks endpoint, ranked by Spotify popularity, no client-side sort needed). Results accumulate in `ResolveResult` dataclasses *before* any write happens. This is what makes `--dry-run` cheap and safe.
2. **Write phase** — `get_or_create_playlist()` reuses a playlist of the same name if it exists (does NOT clear it; the `playlist_replace_items` call to wipe is intentionally commented out in `main()`), then `add_tracks_in_batches()` chunks at 100 URIs/call.

Auth uses spotipy's `SpotifyOAuth` Authorization Code flow with token cache at `.spotify_cache`. First run opens a browser; subsequent runs are silent.

### Artists file format — load-bearing detail

`ArtistEntry.parse()` supports a `Display Name|search query` pipe override. The display name is used for logs and mismatch detection; the search query is what hits Spotify. This is the **primary tool for fixing wrong matches** — when dry-run shows `⚠ matched as '<wrong>'`, the fix is to add or refine an override in the artists file, not to change code. Stylized names (`¥ØU$UK€ ¥UK1MAT$U`), single common words (`Geese`, `Levity`), and band names that collide with other artists are the typical override cases.

### Spotify API constraints already accounted for

Don't re-introduce code that fights these:
- App stays in **Development Mode** (correct for personal use; the owner must be added to the app's User Management list — even as the owner — for API calls to succeed).
- **Search** is capped at 10 results per page (`SEARCH_LIMIT` constant). The artist resolver pulls the full page so it can keep alternatives for review.
- **Rate limit** is two-layer: a per-30s burst limit (~180 req) AND a hidden **daily quota** that is much tighter than you'd think — a few full ~120-artist runs can lock you out for ~20 hours. Hit empirically on 2026-05-05; spotipy's `Retry-After` came back as 72,330s.
- `RATE_LIMIT_SLEEP = 0.2s` between every API call keeps us well under the burst cap. The real defense against the daily quota is the resolution cache (below).

### Resolution cache — load-bearing for staying within daily quota

`.resolution_cache.json` holds per-artist resolutions (artist_id, matched_name, track_uris, sample_track, confidence, alternatives). Cache key encodes display name + override (`q:<query>` or `id:<id>`), so changing an entry's line invalidates just that slot. The cache is **persisted after every successful resolution**, not at end of run — a mid-run crash never loses progress.

Cache hits skip both `resolve_artist` and `fetch_top_tracks` entirely (zero API calls). Re-runs after a clean first run typically make 0 API calls in the resolve phase. To force a full re-resolve, delete the file. Don't commit it (per-user, may contain proprietary lineup data).

### Discovery via Last.fm + ListenBrainz (the primary path when configured)

When `LASTFM_API_KEY` is in `.env`, `fetch_top_tracks()` tries this path first and only falls back to Spotify if it comes up short:

1. `_lastfm_get_top_tracks()` — single `artist.getTopTracks` call with `autocorrect=1`, returns ordered track names. Ignore the MBIDs Last.fm returns; they're frequently stale/invalid against the live MusicBrainz database (404 on lookup).
2. `_listenbrainz_map_tracks()` — single batch POST to `https://labs.api.listenbrainz.org/spotify-id-from-metadata/json` with all track names + the artist name. Returns Spotify track IDs (`{track_name: spotify_uri}`).

Two HTTP calls per artist total — both to free, no-key, no-quota services. The Last.fm key is free, instant signup, no approval. This bypasses Spotify's daily quota entirely for the discovery phase; only the final playlist write (~15 calls for a 1000-track playlist) touches the Spotify API.

Critical: when the Last.fm path succeeds, `result.matched_name` is forced to `entry.display_name` to keep the cache consistent with what Last.fm actually searched for. Otherwise Spotify's `resolve_artist` might have picked a different same-named artist, and the cached `matched_name` would mislead the review block.

If `LASTFM_API_KEY` is absent, the script falls back to pure-Spotify resolution — slower but still works. The Last.fm path requires no other configuration.

### Concert mode (Setlist.fm)

When invoked with `--setlist "<artist>"` instead of `--artists <file>`, the script switches to a completely different discovery path:

1. `clients/setlistfm.py:search_artist_mbid()` — resolves the artist name to a MusicBrainz ID via Setlist.fm's `/search/artists?sort=relevance`.
2. `clients/setlistfm.py:fetch_recent_setlists()` — pulls up to `--shows N` setlists (newest first, past dates only). Empty-tracklist setlists (artist played but tracks not entered yet) are filtered.
3. `services/resolver.py:fetch_setlist_tracks()` — orchestrates the above + ListenBrainz mapping in two passes:
   - **Pass 1**: batch-look up every song under the performing artist (handles non-covers, plus the case where the performer released a studio recording of a cover they play live).
   - **Pass 2**: any cover that didn't resolve under the performer falls back to the original artist (Setlist.fm's `song.cover.name`).
   Result is one URI per song, deduped, in setlist order.

Important Setlist.fm quirks baked into the client:
- `song.tape == true` means walk-on/intro music — skipped (not actually performed).
- `song.cover.name` is the ORIGINAL artist (not the performer). Setlist's top-level `artist` is the performer.
- Rate limit is **2 req/sec** enforced strictly — `SETLISTFM_SLEEP = 1.1s` keeps us safe.
- Date format is `dd-MM-yyyy` (European), not ISO.
- Default content type is XML — always send `Accept: application/json`.

Concert mode does NOT use the resolution cache (setlists change as artists play new shows; cheap to re-fetch). Only the top-tracks mode uses `.resolution_cache.json`.

### Other Feb 2026 endpoint surprises actually hit

Found empirically while writing the playlist phase:
- `POST /users/{id}/playlists` returns 403 for Dev Mode apps. Use `POST /me/playlists` instead. spotipy's `user_playlist_create()` calls the old path — bypassed via `sp._post("me/playlists", payload=...)`.
- `GET /playlists/{id}` no longer returns a top-level `tracks` key — it's renamed to `items`, and inside each item the `track` field is renamed to `item` (for tracks-or-episodes generalization). spotipy's `playlist_items()` returns the old shape but the values are missing — must call the endpoint directly via `sp._get("playlists/{id}/items")` and parse `item.item.uri` instead of `item.track.uri`.
- Adding tracks via `playlist_add_items()` still works fine since we only send URIs, not parse responses.
- OAuth scope `playlist-read-private` is required to call `current_user_playlists` (used in the get-or-create check) even for the user's own private playlists. The original scope set lacked this.

### The `top-tracks` workaround

`GET /artists/{id}/top-tracks` was **removed for new Dev Mode apps in the Feb 2026 API changes**, with no official replacement. `fetch_top_tracks()` works around this by calling `search(type="track")`, filtering results to tracks where the resolved artist ID is credited (drops covers and stray features), then sorting client-side by `popularity`. This keeps the call count the same as before. Don't try to "fix" it back to `artist_top_tracks` — it returns 403.

Other already-deprecated endpoints (audio-features, recommendations, related-artists, batch `/artists`, `/users/{id}`, etc.) must not be added — see [Feb 2026 changelog](https://developer.spotify.com/documentation/web-api/references/changes/february-2026).

### Confidence scoring on dry-run — what's actually possible

The Feb 2026 changes also stripped `popularity`, `followers`, and `genres` from artist objects in search responses for Dev Mode apps. (Confirmed empirically: every artist, even Charli XCX, came back with `popularity=0, followers=0, genres=[]`.) Three of the four signals originally planned for confidence scoring are dead on arrival.

`score_confidence()` is reduced to two signals that still work:
1. **Name distance** — input display name vs. matched name after diacritic/case/parenthetical normalization (substring containment counts as a match). Mismatch ⇒ LOW.
2. **Same-name ambiguity** — alternative hits whose normalized name equals the matched name (e.g. multiple Spotify artists literally named "Geese" or "Palace"). ⇒ MEDIUM.

Each per-artist log line now includes the top-track name so the user can eyeball-verify, since automated heuristics are limited:
```
[12/124] Geese
  → 10 tracks (top: '3D Country') ⚠ medium confidence
```

If popularity/followers/genres are ever restored (or the user moves the app to Extended Quota Mode), restoring those signals is straightforward — the dataclasses just need their fields back. Don't add them speculatively while Dev Mode strips them; they'll always be zero/empty and produce false positives.

The intended workflow stays the same: dry-run → review block surfaces uncertain matches → user edits the artist file with `Display|search query` overrides → dry-run again → real run when satisfied. The artist file is the source of truth so overrides persist across runs.

## File layout

```
spotify_playlist.py                  # thin entry-point shim — `from playlist_maker.cli import main`
playlist_maker/
  __init__.py
  cli.py                             # argparse + main orchestration loop + summary
  models.py                          # ArtistEntry, ArtistCandidate, ResolveResult,
                                     #   parse_artist_override, normalize_name
  cache.py                           # load/save/hydrate .resolution_cache.json
  config.py                          # config-dir resolution (env var → ~/.spotify-playlist-maker/ → CWD)
  confidence.py                      # score_confidence + print_review_block
  clients/                           # thin wrappers around external APIs — one file per service
    spotify.py                       # auth + every Spotify call we make
    lastfm.py                        # artist.getTopTracks
    listenbrainz.py                  # spotify-id-from-metadata batch mapping
    setlistfm.py                     # artist search + recent setlists + cover extraction
  services/                          # business logic composing clients
    resolver.py                      # resolve_artist + fetch_top_tracks (Last.fm-first chain)
                                     #   + fetch_setlist_tracks (concert mode)
    playlist.py                      # get_or_create + replace + dedupe + add batches

.claude-plugin/
  plugin.json                        # plugin manifest (name, version, repo) — committed
skills/
  playlist/
    SKILL.md                         # /playlist slash command — committed
.claude/
  settings.local.json                # per-user Claude Code prefs — gitignored
lineups/
  example.txt                        # generic example — committed
  <name>.txt                         # dev-mode lineups when working in this repo — gitignored

# Plugin users get a separate config dir (not in this tree):
~/.spotify-playlist-maker/
  .env                               # credentials
  .spotify_cache                     # spotipy OAuth token
  .resolution_cache.json             # per-artist cache
  lineups/                           # generated + override lineup files
```

The `.gitignore` enforces: any file in `lineups/` except `example.txt` is local-only; all caches, credentials, and per-user Claude state are local-only. The `config.py` module resolves which directory to use at runtime — `$SPOTIFY_PLAYLIST_CONFIG_DIR` if set, else `~/.spotify-playlist-maker/` if it exists, else CWD (the dev case inside this repo).

**Reading the tree**: `clients/` is one file per external API (Spotify, Last.fm, ListenBrainz). `services/` composes those clients to do real work (resolution, playlist writing). `models`/`cache`/`confidence` are standalone concerns at the package root. `cli.py` wires it into the user-facing command. The root `spotify_playlist.py` is a 12-line shim that exists only so `python spotify_playlist.py …` (what the README and `/playlist` skill invoke) still works without needing a pip install.

Most files are well under 100 lines; the two orchestration files (`cli.py`, `services/resolver.py`) are ~220 lines each. When adding a new discovery source (e.g. Setlist.fm), add `clients/<name>.py` and either extend `services/resolver.py` or create a sibling `services/<purpose>_resolver.py` — that's the seam the structure was designed around.

## Skill layer

The `/playlist` skill is the user-facing entry point. It's a thin orchestrator that:

1. Detects first-run (no `~/.spotify-playlist-maker/.env`) and walks the user through Spotify dev app setup + key collection.
2. Decides whether the input is an attached image (vision-extract), pasted text, or a file path.
3. Slugifies a filename, writes the extracted artists to `~/.spotify-playlist-maker/lineups/<slug>.txt`.
4. Shells out to `python3 ${CLAUDE_PLUGIN_ROOT}/spotify_playlist.py` for the dry-run, parses the `Failed` and `Review needed` sections, and walks the user through each flagged artist (keep / override / drop).
5. Checks for an existing same-named playlist and asks append-vs-replace before the real run.
6. Re-invokes the CLI without `--dry-run` for the final write.

The skill has `disable-model-invocation: true` — only the user can trigger it via `/playlist`, never Claude autonomously. `allowed-tools` pre-approves `Bash(python3 *)` so the user isn't permission-prompted mid-flow.

When extending: keep all Spotify API logic in the Python CLI (so it stays scriptable without Claude). The skill should only do UX, input extraction, and orchestration — not duplicate the resolution/caching/playlist logic.

### Dev workflow vs. plugin-install workflow

The CLI is **dual-mode** on purpose. Inside this repo, `python spotify_playlist.py --artists lineups/foo.txt …` still works — `config.py` falls back to CWD when `~/.spotify-playlist-maker/` doesn't exist. That keeps iteration cheap.

For end users, the plugin lives at `${CLAUDE_PLUGIN_ROOT}` after `/plugin install` and reads/writes from `~/.spotify-playlist-maker/`. Same code, different config dir.

### MCP wrapper (v0.3+)

A Claude Desktop MCP server is planned as a thin wrapper around `playlist_maker/` — exposes `create_playlist_from_artists` and `create_concert_playlist` tools. Same package powers both surfaces. Deferred until the plugin sees real usage.
