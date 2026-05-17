# Spotify Playlist Maker

Generate a Spotify playlist from a list of artists OR from an artist's recent live setlists. Built for festival lineups originally — works for any list of artists, or now for "I'm going to a concert, what are they playing?"

**Two modes:**

- **Top-tracks mode** — `--artists <file>`: build a playlist of the top N tracks per artist from a text file. Use this for festival lineups, themed lists, etc.
- **Concert mode** — `--setlist "<artist>"`: build a playlist from the artist's most recent live setlists. Use this when you're going to see someone and want to know their current tour set.

**Two ways to invoke, both run from inside the cloned repo:**

- **CLI**: `python spotify_playlist.py [--artists <file> | --setlist "<artist>"] ...`. Scriptable.
- **`/playlist` in Claude Code**: launch Claude Code from inside this directory and type `/playlist`. Attach a poster image, paste an artist list, point at a `lineups/<name>.txt` path, or ask for a concert playlist. Claude does vision extraction / orchestration, walks you through review, and creates the playlist.

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

### 2. Get a Last.fm API key (recommended, ~1 min)

Spotify's Dev Mode daily quota is brutally tight — about 150-200 calls per 24h, enough to lock you out for ~24h after a couple of full playlist builds. The script offloads artist/track discovery to Last.fm + ListenBrainz (both free, no quota worth worrying about) when a Last.fm key is configured.

1. Go to https://www.last.fm/api/account/create (you'll need to be logged into a regular Last.fm account first — sign up at https://www.last.fm/join if you don't have one).
2. Fill in any application name and description. Leave callback URL blank.
3. Copy the **API key** (and the shared secret — we don't use it yet but you can't see it again later).

You can skip this step — the script will work without it — but a 100-artist build will then take 3-4 days to complete due to Spotify's quota.

### 2b. Get a Setlist.fm API key (only if you want concert mode)

1. Go to https://www.setlist.fm/settings/api (you'll need to be logged into a regular Setlist.fm account first — sign up at https://www.setlist.fm/signup if you don't have one).
2. Click "request an API key" — fill in app name and short description.
3. **Key is instant** for the default (free) tier. Add it to `.env` as `SETLISTFM_API_KEY`.

Default rate limit is 2 req/sec, 1440/day — plenty for occasional use.

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install spotipy python-dotenv requests
```

### 4. Create `.env`

Copy `.env.example` to `.env` and fill in:

```
SPOTIPY_CLIENT_ID=your_client_id_here
SPOTIPY_CLIENT_SECRET=your_client_secret_here
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback

# Optional but strongly recommended (see step 2):
LASTFM_API_KEY=your_lastfm_api_key_here

# Optional, only for concert mode (see step 2b):
SETLISTFM_API_KEY=your_setlistfm_api_key_here
```

## Usage

### Top-tracks mode (artist list)

Put your artist list in `lineups/<your_name>.txt` (see `lineups/example.txt`). Then:

```bash
# Dry run first — resolves artists, shows mismatches, doesn't write anything
python spotify_playlist.py --artists lineups/<your_name>.txt --name "<playlist>" --dry-run

# Real run (creates/reuses a private playlist by default)
python spotify_playlist.py --artists lineups/<your_name>.txt --name "<playlist>" \
    --top 10 --description "Top 10 tracks per artist"
```

### Concert mode (recent setlists)

When you're going to see an artist and want a playlist of what they're playing live:

```bash
# Dry run — fetches recent setlists, dedupes, maps to Spotify
python spotify_playlist.py --setlist "Phoebe Bridgers" --shows 10 --dry-run

# Real run — auto-derives playlist name as "<Artist> — Recent Live"
python spotify_playlist.py --setlist "Phoebe Bridgers" --public
```

By default, pulls the last 10 setlists (use `--shows N` to change). Covers are included: each cover is looked up first under the performing artist (in case Spotify has their recording of it), then falls back to the original artist. Walk-on music and intro tapes are skipped automatically.

**Important**: as of Feb 2026, every Dev Mode app must add its owner under **User Management** in the dashboard (Settings → User Management → Add User) — even though you own the app. Without this, every API call returns 403.

First run pops a browser to authorize the app against your Spotify account. After that it caches the refresh token in `.spotify_cache` and runs silently.

### Flags

| Flag            | Default | Notes                                                                       |
|-----------------|---------|-----------------------------------------------------------------------------|
| `--artists`     | —       | Path to your lineup file (top-tracks mode — mutually exclusive with --setlist) |
| `--setlist`     | —       | Artist name for concert mode (mutually exclusive with --artists)            |
| `--name`        | —       | Playlist name (required for --artists; auto-derived for --setlist)          |
| `--top`         | 10      | Top N tracks per artist (top-tracks mode)                                   |
| `--shows`       | 10      | Number of recent setlists to pull (setlist mode)                            |
| `--market`      | US      | ISO country code; affects track availability                                |
| `--description` | ""      | Playlist description                                                        |
| `--public`      | off     | Make playlist public (default: private)                                     |
| `--replace`     | off     | Wipe an existing same-named playlist before adding (default: append)        |
| `--fast`        | off     | Skip album-walk fill-in for search-resolved artists (saves Spotify quota)   |
| `--dry-run`     | off     | Resolve & report only, don't write                                          |

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

Confidence scoring is intentionally minimal: Spotify stripped artist `popularity`, `followers`, and `genres` from Dev Mode responses in Feb 2026, so only **name distance** and **same-name ambiguity** can be detected automatically. The top-track display is the main eyeball-verification signal — if the top track comes back as something you recognize, it's the right artist. Edit the lineup file with `Display|<refined query>` or `Display|<Spotify URL>` overrides for the flagged ones, re-run, then do the real run.

Re-running with the same `--name` reuses the existing playlist and **appends** new tracks by default. To fully refresh (wipe then re-add), pass `--replace`.

## How discovery works (Last.fm + ListenBrainz)

When `LASTFM_API_KEY` is in your `.env`, the script does artist/track discovery off-Spotify:

1. **Last.fm `artist.getTopTracks`** — one call per artist, returns the top tracks ordered by global popularity. Has an autocorrect feature that handles most name disambiguation automatically.
2. **ListenBrainz `spotify-id-from-metadata`** — one batch call per artist, maps every track from step 1 to a Spotify track URI in a single request.

That's **2 HTTP calls per artist**, both to free services with no meaningful quota, instead of 5-8 Spotify calls per artist. The Spotify API only gets touched for the final playlist write (~15 calls regardless of size).

If a Last.fm key isn't configured, the script falls back to Spotify-only discovery: `search(type=track)` for tracks credited to the artist, then walking the artist's albums via `artist_albums` + `album_tracks` to fill out anything search missed. Works, but slow and quota-bound.

## Notes on Spotify API quirks (Feb 2026 lockdown)

A bunch of useful endpoints were removed or reshaped in Feb 2026; the relevant ones are worked around in code:

- **`/artists/{id}/top-tracks` is gone** for new Dev Mode apps. Replaced by `search(type=track)` filtered by artist ID, then album walking.
- **`POST /users/{id}/playlists` returns 403** for Dev Mode apps. Use `POST /me/playlists` instead. spotipy still calls the old path; we bypass via `sp._post`.
- **Playlist response schema changed**: `tracks` renamed to `items`, and inside each item `track` renamed to `item`. spotipy's `playlist_items()` returns the old shape but with empty values.
- **Artist metadata stripped**: `popularity`, `followers`, and `genres` no longer come back on artist objects.
- **Search limit** is 10 results per page; **`artist_albums` limit** is 10 (was 50).
- **Rate limits**: per-30s burst (~180 req) AND a much tighter **daily quota** — about 150-200 calls per 24h rolling window. Once exceeded, you're locked out for ~24h. The Last.fm path above is the real fix.

## Resolution cache (`.resolution_cache.json`)

After each successful artist resolution, the script appends to `.resolution_cache.json`. Re-runs hit the cache and skip discovery for those entries — zero API calls if nothing changed. Cache key includes the entry's override, so changing an artist's line in the file invalidates just that entry. To re-resolve everything, delete the file.

The cache is gitignored — it's user-specific and may contain proprietary lineup data.

## License

MIT — see [LICENSE](LICENSE).
