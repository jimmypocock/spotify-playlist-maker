---
description: Build a Spotify playlist either of top tracks per artist (from a file/image/paste) OR of an artist's recent live setlists (concert mode). Uses vision to extract artists from posters, walks the user through review, and creates the playlist in their Spotify account.
disable-model-invocation: true
allowed-tools: Bash(.venv/bin/python *), Bash(python3 *), Bash(test *), Bash(ls *), Bash(mkdir *), Read, Write, Edit, Glob
argument-hint: "[lineup-path] [--name \"<playlist>\"]  |  [concert request like \"seeing Phoebe Bridgers Friday\"]  |  use with an attached image or pasted artist list"
---

# /playlist — Build a Spotify playlist

Two modes, both wrapping the project's Python CLI (`spotify_playlist.py`):

- **Top-tracks mode** (`--artists <file>`) — playlist of top N tracks per artist from a list. Use this for festival lineups, themed lists, anything multi-artist.
- **Concert mode** (`--setlist "<artist>"`) — playlist from the artist's most recent live setlists. Use this when the user is going to see ONE artist and wants their current tour set.

Be conversational and concise in updates — surface only what the user needs to act on.

## Mode detection (first thing to do)

Read the user's message + arguments and pick a mode:
- **Concert intent** ("I'm seeing X", "going to see X", "for X concert/tour", "setlist for X", or just an artist name with no list): → concert mode, `--setlist "<artist>"`.
- **List intent** (attached image of a lineup, pasted multi-artist text, file path, festival mention): → top-tracks mode, `--artists lineups/<slug>.txt`.
- **Ambiguous**: ask. e.g., "Phoebe Bridgers" alone → "Are you going to see her live (concert mode), or want a playlist of her top tracks (you can just use Spotify for that)?"

For concert mode, you skip Steps 1-3 below (no file extraction needed) and jump straight to running the CLI with `--setlist`. The rest of the flow (verify env, dry-run, review, confirm, write) is the same.

---

## Step 0 — Environment check

Before doing anything else, verify the working directory has what's needed:

1. `test -f .env && test -f spotify_playlist.py && test -d lineups`
2. Pick the Python interpreter: prefer `.venv/bin/python` if it exists, fall back to `python3`. Store the choice — use it for every CLI invocation below.
3. Check for `LASTFM_API_KEY` in `.env` (just grep). If absent, mention to the user: "You don't have a Last.fm API key configured — without it, the script will use Spotify's API for discovery, which has a tight daily quota (~150 calls/day) and may not complete a large lineup in one session. Get a free key at https://www.last.fm/api/account/create (takes ~1 min) for much faster builds. Want to set that up first, or proceed with Spotify-only?"

If `.env` is missing entirely, point the user at README §1 (register Spotify app + create `.env`). If `.venv` is missing, suggest: `python3 -m venv .venv && .venv/bin/pip install spotipy python-dotenv requests`. Don't proceed until these are in place.

## Step 1 — Identify the input source

Look at the arguments (`$ARGUMENTS`) and the conversation context:

- **Attached image** (festival poster, lineup graphic, screenshot) → use vision to extract every artist name visible. Don't skip smaller print — sub-headliners, late-night sets, side stages all matter. List the artists back to the user for a quick visual check before saving.
- **Pasted text** in the user's message → parse line by line, one artist per line.
- **File path** that exists (typically `lineups/*.txt`) → use directly, skip extraction.
- **Nothing provided** → ask: "Paste your artists, attach a poster image, or point me at an existing file in `lineups/`."

Combine sources if the user provides more than one (e.g., poster + a few hand-typed additions).

## Step 2 — Pick a playlist name and lineup filename

Derive the playlist name from the user's message if they gave one (e.g., "ACL Fest 26", "summer 2026"). Otherwise ask.

The lineup filename is a slugified version of the name: lowercase, alphanumeric, underscores for spaces. `ACL Fest 26` → `lineups/acl_fest_26.txt`. If the file already exists, ask before overwriting — they may have manual overrides already.

## Step 3 — Save the lineup file

Write the extracted artists to `lineups/<slug>.txt` with a brief header comment naming the source (poster, paste, etc.) and the date. One artist per line. No pipe-overrides at this stage — those get added during review.

## Step 4 — Dry run

For top-tracks mode:
```bash
<python> spotify_playlist.py --artists lineups/<slug>.txt --name "<playlist>" --dry-run
```

For concert mode (requires `SETLISTFM_API_KEY` in `.env`):
```bash
<python> spotify_playlist.py --setlist "<artist>" --shows 10 --dry-run
```

Capture stdout+stderr to `/tmp/playlist_dryrun.log` for parsing. Don't show the user the raw log unless they ask — just the summary.

For concert mode specifically: surface the number of tracks resolved (e.g., "Pulled 10 setlists, got 27 unique tracks on Spotify"). Worth noting if any cover songs are included and which artists they're from — could surprise the user.

## Step 5 — Review loop

Parse the dry-run output. Two interesting sections:

- **`Failed (N)`** — artists where no playable tracks were found
- **`Review needed (N)`** — same-name ambiguity or name mismatches

For each failed or low-confidence artist, present the **display name + matched artist name + top track + alternatives** to the user. Offer three options per artist:
1. **Looks right, keep it** — leave the entry alone
2. **Wrong artist** — ask for a Spotify URL, then update the lineup file's line with the override (`Display Name|<url>`)
3. **Skip / drop** — comment out the line (`# Display Name`)

After applying any overrides, re-run the dry-run. Loop until the user is satisfied or until no more issues remain.

The cache makes re-runs cheap — only changed entries hit the API.

## Step 6 — Check for existing playlist with same name

Before the real run, see if a playlist with the same name already exists in the user's account. Use a quick Python snippet via Bash:

```python
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
load_dotenv(".env")
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    scope="playlist-modify-public playlist-modify-private playlist-read-private",
    cache_path=".spotify_cache", open_browser=False,
))
uid = sp.current_user()["id"]
name = "<playlist>"
existing = None
offset = 0
while True:
    page = sp.current_user_playlists(limit=50, offset=offset)
    for pl in page["items"]:
        if pl["name"] == name and pl["owner"]["id"] == uid:
            existing = pl
            break
    if existing or not page["next"]:
        break
    offset += 50
if existing:
    # Spotify renamed `tracks` → `items` in Feb 2026; spotipy still exposes it
    # but the count is on the original object.
    total = existing.get("tracks", {}).get("total", "?")
    print(f"EXISTS:{existing['id']}:{total}")
else:
    print("NEW")
```

If `EXISTS:...`, tell the user: "A playlist called '<name>' already exists with <N> tracks. **Append** new tracks, or **wipe and rebuild**?" Pass `--replace` to the CLI for wipe; omit it for append (default).

## Step 7 — Confirm and create

Confirm with the user:
- Public or private (default: private)
- Description (optional — suggest something based on the source, e.g. "Top tracks per artist · ACL 2026 lineup")
- Append vs. replace (from step 6)

Then run for real.

For top-tracks mode:
```bash
<python> spotify_playlist.py --artists lineups/<slug>.txt --name "<playlist>" \
    [--public] [--replace] [--description "..."]
```

For concert mode (name auto-derives to "<Artist> — Recent Live" if you don't pass `--name`):
```bash
<python> spotify_playlist.py --setlist "<artist>" --shows 10 \
    [--public] [--replace] [--name "<custom>"] [--description "..."]
```

When it finishes, report the playlist URL from the script's final line.

---

## Notes for Claude

- **Be terse during execution.** Don't echo the entire dry-run log; summarize and surface only flagged artists.
- **Be honest about misses.** If the script can't find tracks for an artist (legitimate Spotify gap), tell the user; don't pretend it succeeded.
- **Rate limit handling.** If a run hits Spotify's daily quota (Retry-After ~72000s), stop immediately (don't let spotipy sleep through it — kill the python process), explain the user is locked out for ~24h, and suggest setting `LASTFM_API_KEY` if they haven't already. With Last.fm configured, discovery doesn't touch Spotify's quota and a fresh build of 100+ artists takes under 2 minutes.
- **Cache awareness.** Mention how many cache hits vs new resolutions there were (`X/Y from cache`) — gives the user a sense of cost. With Last.fm, even fresh resolutions are nearly free.
- **No autonomous invocation.** This skill has `disable-model-invocation: true` — the user must explicitly type `/playlist`. Don't try to invoke it on their behalf in other contexts.
