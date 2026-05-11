# Spotify Playlist Maker

Generate a Spotify playlist of the top N tracks for every artist in a list. Built for festival lineups originally — works for any list of artists you want to compile into a playlist.

Two ways to use it, both run from inside the cloned repo:

- **CLI**: `python spotify_playlist.py --artists <file> --name "<playlist>"`. Self-contained, scriptable.
- **`/playlist` in Claude Code**: launch Claude Code from inside this directory and type `/playlist`. Attach a poster image, paste an artist list, or pass a `lineups/<name>.txt` path. Claude does vision extraction, walks you through review, and creates the playlist.

The `/playlist` skill is project-scoped (lives in `.claude/skills/`), so it's available automatically once you've cloned the repo. No marketplace install yet — that's planned for a later version. For now: clone, set up, run.

## One-time setup (~5 min)

### 1. Register a Spotify app

1. Go to https://developer.spotify.com/dashboard and log in with your Spotify account.
2. Click **Create app**.
   - **App name**: anything (e.g. `spotify-playlist-maker`)
   - **App description**: anything
   - **Redirect URI**: `http://127.0.0.1:8888/callback` (must match exactly)
   - **Which API/SDKs are you planning to use?**: Web API
3. Save. On the app page, copy the **Client ID** and **Client Secret** (click "View client secret").

The app stays in Development Mode forever — that's fine. As the app owner with a Premium account, you don't hit user-allowlist limits for your own use.

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install spotipy python-dotenv
```

### 3. Create `.env`

Copy `.env.example` to `.env` and fill in:

```
SPOTIPY_CLIENT_ID=your_client_id_here
SPOTIPY_CLIENT_SECRET=your_client_secret_here
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

## Usage

Put your artist list in `lineups/<your_name>.txt` (see `lineups/example.txt`). Then:

```bash
# Dry run first — resolves artists, shows mismatches, doesn't write anything
python spotify_playlist.py --artists lineups/<your_name>.txt --name "<playlist>" --dry-run

# Real run (creates/reuses a private playlist by default)
python spotify_playlist.py --artists lineups/<your_name>.txt --name "<playlist>" \
    --top 10 --description "Top 10 tracks per artist"
```

**Important**: as of Feb 2026, every Dev Mode app must add its owner under **User Management** in the dashboard (Settings → User Management → Add User) — even though you own the app. Without this, every API call returns 403.

First run pops a browser to authorize the app against your Spotify account. After that it caches the refresh token in `.spotify_cache` and runs silently.

### Flags

| Flag            | Default | Notes                                          |
|-----------------|---------|------------------------------------------------|
| `--artists`     | —       | Path to your lineup file (required)            |
| `--name`        | —       | Playlist name (required)                       |
| `--top`         | 10      | Top N tracks per artist                        |
| `--market`      | US      | ISO country code; affects track availability   |
| `--description` | ""      | Playlist description                           |
| `--public`      | off     | Make playlist public (default: private)        |
| `--dry-run`     | off     | Resolve & report only, don't write             |

## Artists file format

One per line. Lines starting with `#` are comments. See `lineups/example.txt` for a complete starter file.

For names that confuse Spotify's search (stylized text, common words, ambiguous names), use a pipe override. The override can be either a refined search query OR a direct Spotify URL/URI/ID:

```
Beyoncé
BUNT.|BUNT
Geese|Geese band Brooklyn
Palace|https://open.spotify.com/artist/48vDIufGC8ujPuBiTxY8dm
```

The display name (left of `|`) is used in logs. The override (right) is either fed to Spotify search OR — when it looks like a Spotify artist URL/URI/ID — used directly to bypass search. URL/ID overrides are the most reliable fix when search keeps picking the wrong artist.

## Workflow tip

Always do `--dry-run` first. Each match logs inline with the artist's top track for sanity-check, and uncertain matches are summarized in a **Review needed** block at the end:

```
[12/124] Geese
  → 10 tracks (top: '3D Country') ⚠ medium confidence

...

Review needed (3):

  ⚠ MED  Geese
      matched: 'Geese'  (top track: '3D Country')
      reasons: 3 artists named 'Geese'
      alternatives:
        • Geese  (id: 4kE2...)
        • Geese  (id: 7tQp...)
      fix: in lineups/<your_name>.txt, change line to 'Geese|<more specific search query>'
```

Confidence scoring is intentionally minimal: Spotify stripped artist `popularity`, `followers`, and `genres` from Dev Mode responses in Feb 2026, so only **name distance** and **same-name ambiguity** can be detected automatically. The top-track display is the main eyeball-verification signal — if the top track comes back as something you recognize, it's the right artist. Edit the lineup file with `Display|<refined query>` or `Display|<Spotify URL>` overrides for the flagged ones, re-run, then do the real run. Re-running with the same `--name` reuses the existing playlist; to fully refresh, uncomment the `playlist_replace_items` line in the script.

## Notes on Spotify API quirks

- **`/artists/{id}/top-tracks` was removed** for new Dev Mode apps in Feb 2026. The script works around this with `search(type=track)` filtered by artist ID.
- **Artist metadata stripped**: `popularity`, `followers`, and `genres` no longer come back on artist objects in Dev Mode — every artist looks identical (all zero/empty). This is why confidence scoring is reduced to name + same-name ambiguity only.
- **Search limit** is 10 results per page (Feb 2026 Dev Mode change). The artist resolver pulls a full page so it can keep alternatives for the review block.
- **Rate limits**: two-layer. A per-30s burst limit (~180 req — handled by a 0.2s sleep between calls) AND a much tighter **daily quota** that can lock you out for ~20 hours after only a couple of full runs. The resolution cache (below) is the real defense.
- **Other removed endpoints** (audio-features, recommendations, related-artists, batch `/artists`, etc.) aren't in our path.

## Resolution cache (`.resolution_cache.json`)

After each successful artist resolution, the script appends to `.resolution_cache.json`. Re-runs hit the cache and skip the API for those entries — zero calls if nothing changed. Cache key includes the entry's override, so changing an artist's line in the file invalidates just that entry. To re-resolve everything, delete the file.

The cache is gitignored — it's user-specific and may contain proprietary lineup data.

## License

MIT — see [LICENSE](LICENSE).
