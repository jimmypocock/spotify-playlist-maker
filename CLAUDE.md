# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project intent

A **general-purpose Spotify playlist maker** (`spotify_playlist.py`). Started as a one-off ACL 2026 script (originally `festival_playlist.py`) and has been generalized for any list of artists. Public repo at `https://github.com/jimmypocock/spotify-playlist-maker`.

Strict separation between generic and specific content:

- **Generic / agnostic code** (script, auth, search, batching, CLI plumbing, the `lineups/example.txt`) — committed to GitHub.
- **Specific inputs and caches** (`lineups/*.txt` other than `example.txt`, `.resolution_cache.json`, `.spotify_cache`, `.env`) — gitignored, local-only.

When adding features, default to making them reusable across input lists. When adding files, decide explicitly whether each one is generic (committed) or specific (gitignored, in `lineups/` or hidden at root).

A Claude Code skill wraps the CLI: `.claude/skills/playlist/SKILL.md` defines the `/playlist` slash command. It's project-scoped — auto-loaded when Claude Code runs from inside a clone of this repo. Marketplace distribution (proper plugin format with `.claude-plugin/plugin.json`) is intentionally deferred until v0.2; current scope is "clone and use." See "Skill layer" section below.

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
spotify_playlist.py             # generic CLI — committed
.claude/
  skills/
    playlist/
      SKILL.md                  # /playlist slash command — committed
  settings.local.json           # per-user Claude Code prefs — gitignored
lineups/
  example.txt                   # generic example — committed
  <name>.txt                    # user's actual lineups — gitignored
.resolution_cache.json          # per-artist cache — gitignored
.spotify_cache                  # spotipy OAuth token — gitignored
.env                            # credentials — gitignored
```

The `.gitignore` enforces: any file in `lineups/` except `example.txt` is local-only; all caches, credentials, and per-user Claude state are local-only.

## Skill layer

The `/playlist` skill is the user-facing entry point. It's a thin orchestrator that:

1. Decides whether the input is an attached image (vision-extract), pasted text, or a file path.
2. Slugifies a filename, writes the extracted artists to `lineups/<slug>.txt`.
3. Shells out to `spotify_playlist.py` for the dry-run, parses the `Failed` and `Review needed` sections, and walks the user through each flagged artist (keep / override / drop).
4. Checks for an existing same-named playlist and asks append-vs-replace before the real run.
5. Re-invokes the CLI without `--dry-run` for the final write.

The skill has `disable-model-invocation: true` — only the user can trigger it via `/playlist`, never Claude autonomously. `allowed-tools` pre-approves the CLI invocations (`.venv/bin/python *` and `python3 *`) so the user isn't permission-prompted mid-flow.

When extending: keep all Spotify API logic in the Python CLI (so it stays scriptable without Claude). The skill should only do UX, input extraction, and orchestration — not duplicate the resolution/caching/playlist logic.

### Path to marketplace distribution (v0.2+)

To go from project-scoped skill to installable plugin:
1. Move `.claude/skills/playlist/SKILL.md` → `skills/playlist/SKILL.md`
2. Add `.claude-plugin/plugin.json` with manifest (name, version, description, repository).
3. Skill name becomes namespaced as `<plugin-name>:playlist` for users who install via `/plugin marketplace add ... && /plugin install ...`.

This intentionally isn't done in v0.1 — single-source for the skill keeps the repo simple while we shake out real usage.
