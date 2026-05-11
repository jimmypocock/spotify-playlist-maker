---
description: Build a Spotify playlist of top tracks per artist. Accepts a file path, pasted artist list, or an attached image (festival poster, lineup screenshot). Uses vision to extract artists, walks the user through the review loop, and creates a playlist in their Spotify account.
disable-model-invocation: true
allowed-tools: Bash(.venv/bin/python *), Bash(python3 *), Bash(test *), Bash(ls *), Bash(mkdir *), Read, Write, Edit, Glob
argument-hint: "[lineup-path] [--name \"<playlist>\"]  |  use with an attached image or pasted artist list"
---

# /playlist — Build a Spotify playlist

Build a Spotify playlist of the top N tracks per artist. The user can provide artists via a file path, an attached image (festival poster, lineup screenshot), pasted text, or a combination.

This skill wraps the project's Python CLI (`spotify_playlist.py`) and guides the user through extraction → review → confirmation. Be conversational and concise in your updates — surface only the things the user needs to act on.

---

## Step 0 — Environment check

Before doing anything else, verify the working directory has what's needed:

1. `test -f .env && test -f spotify_playlist.py && test -d lineups`
2. Pick the Python interpreter: prefer `.venv/bin/python` if it exists, fall back to `python3`. Store the choice — use it for every CLI invocation below.

If `.env` is missing, point the user at README §1 (register Spotify app + create `.env`). If `.venv` is missing, suggest: `python3 -m venv .venv && .venv/bin/pip install spotipy python-dotenv`. Don't proceed until these are in place.

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

Run:
```bash
<python> spotify_playlist.py --artists lineups/<slug>.txt --name "<playlist>" --dry-run
```
Capture stdout+stderr to `/tmp/playlist_dryrun.log` for parsing. Don't show the user the raw log unless they ask — just the summary.

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

Then run for real:
```bash
<python> spotify_playlist.py --artists lineups/<slug>.txt --name "<playlist>" \
    [--public] [--replace] [--description "..."]
```

When it finishes, report the playlist URL from the script's final line.

---

## Notes for Claude

- **Be terse during execution.** Don't echo the entire dry-run log; summarize and surface only flagged artists.
- **Be honest about misses.** If the script can't find tracks for an artist (legitimate Spotify gap), tell the user; don't pretend it succeeded.
- **Rate limit handling.** If a run hits Spotify's daily quota (Retry-After ~72000s), stop immediately, explain that the user is locked out for ~20h, and exit. Don't sleep through it.
- **Cache awareness.** Mention how many cache hits vs new resolutions there were (`X/Y from cache`) — gives the user a sense of cost.
- **No autonomous invocation.** This skill has `disable-model-invocation: true` — the user must explicitly type `/playlist`. Don't try to invoke it on their behalf in other contexts.
