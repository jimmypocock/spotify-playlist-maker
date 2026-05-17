---
description: Build a Spotify playlist from any description of what you want — a festival lineup image, a pasted artist list, a concert you're going to, a tour name, a single artist, a vibe. Interprets messy natural language, researches missing info from the web when needed, and creates the playlist in the user's Spotify account.
disable-model-invocation: true
allowed-tools: Bash(.venv/bin/python *), Bash(python3 *), Bash(test *), Bash(ls *), Bash(mkdir *), Bash(grep *), Read, Write, Edit, Glob, WebFetch, WebSearch
argument-hint: "describe what you want — \"seeing Phoebe Bridgers Friday\" / \"Coachella 2027 lineup\" / [attached poster] / lineups/foo.txt"
---

# /playlist — Build a Spotify playlist from any description

You are an **orchestrator**. The user describes what playlist they want in messy English (or an image, a file, a URL, whatever). Your job is to figure out what they actually want, gather any missing info via web research if needed, and call the right CLI commands. **Don't make them adapt to your tool — adapt to them.**

## The primitives you have

The Python CLI (`spotify_playlist.py`) gives you two execution modes:

| Mode | Flag | What it does |
|---|---|---|
| Top-tracks | `--artists <file>` | Playlist of top N tracks for each artist in a text file. The list-of-artists case. |
| Concert | `--setlist "<artist>"` | Playlist from an artist's most recent live setlists. Optional `--tour "<name>"` or `--year <yyyy>` filters. The going-to-a-show case. |

Common flags either mode accepts: `--name`, `--description`, `--public`, `--replace` (wipe before adding), `--dry-run`, `--top N` (top-tracks), `--shows N` (concert, default 10), `--fast` (skip album-walk fallback for Spotify quota).

Everything else is YOU figuring out which primitive to call with what arguments — and using web research, vision, file I/O to bridge the gap between the user's words and those arguments.

---

## Step 0 — Environment check (always first, just once per session)

```bash
test -f .env && test -f spotify_playlist.py && test -d lineups
```

Pick the Python interpreter: prefer `.venv/bin/python` if it exists, fall back to `python3`. Use that for every CLI call below.

Grep `.env` for what's configured:
- `SPOTIPY_*` — required (playlist write). If missing, point to README §1.
- `LASTFM_API_KEY` — strongly recommended for top-tracks mode. Without it, Spotify's ~150 calls/day quota becomes the bottleneck. If absent and the user wants a large list, mention this and offer to wait while they set one up (https://www.last.fm/api/account/create, instant).
- `SETLISTFM_API_KEY` — required only for concert mode. If absent and they want concert mode, point to https://www.setlist.fm/settings/api (instant).

If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install spotipy python-dotenv requests`.

---

## Read the intent

The user's input might be any of these. Pick the matching pattern:

| What they say / show | What they probably want | Which primitive |
|---|---|---|
| Attached image (poster, lineup graphic, screenshot) | Playlist of those artists | Top-tracks |
| Pasted multi-line list of names | Same | Top-tracks |
| Path to an existing `lineups/*.txt` | Same, skip extraction | Top-tracks |
| "I'm seeing X" / "going to X" / "for the X show" | Concert mode | `--setlist "X"` |
| "X's [tour-name] tour" | Concert mode with tour filter | `--setlist + --tour` |
| "X 2019" / "X at Lollapalooza last year" | Concert mode with year filter | `--setlist + --year` |
| Festival/event name + year ("Coachella 2027", "ACL 26", "Outside Lands") | Lineup-based playlist | Web-research lineup → top-tracks |
| Just an artist name, no other context | Genuinely ambiguous | Ask once |

When in doubt, take your best guess and confirm in one sentence: "Sounds like you want a playlist of what Phoebe Bridgers is playing on her current tour — that right, or did you mean something else?"

---

## Web research patterns

You have `WebSearch` and `WebFetch`. Use them when the user's description references info that isn't in their message.

### Researching a festival lineup

When the user names a festival/event but doesn't paste the artist list:

1. `WebSearch` for `"<festival> <year> lineup"` or `"<festival> <year> all artists"`. Look for the official site or a comprehensive list (Wikipedia is often good).
2. `WebFetch` the most-promising URL and extract the FULL artist list — headliners AND smaller acts. Side stages count.
3. Tell the user the count and a few sample names: "I found 124 artists for ACL 2026 from their site (headliners include Charli XCX, RÜFÜS DU SOL, Twenty One Pilots …). Saving as `lineups/acl_2026.txt`."
4. Confirm before writing if the count is dramatically off from expectation.

### Researching a tour name

When the user says "his current tour" or "the Sable tour" without specifics:

1. `WebSearch` for `"<artist> current tour 2026"` or `"<artist> tour name"`.
2. If you find a clear tour name, use it as `--tour "<name>"`. If you don't, just skip the filter — `--shows 10` will pull recent setlists anyway, which is probably what they want.

### Don't over-research

Three searches max for one request, unless the user explicitly asks for thoroughness. Cost the user's patience, not just API quota. If you can't find what you need quickly, just ask them.

---

## Execution patterns

### Pattern A — Festival from a name only

```
1. WebSearch + WebFetch to extract full lineup
2. Slugify name → write to lineups/<slug>.txt
3. <python> spotify_playlist.py --artists lineups/<slug>.txt --name "<derived>" --dry-run
4. Review loop (see below)
5. Confirm → real run
```

### Pattern B — Concert / single artist live

```
1. (Optional) WebSearch for tour name if user said "current tour" etc.
2. <python> spotify_playlist.py --setlist "<artist>" [--tour ...] [--year ...] [--shows N] --dry-run
3. Surface track count + cover-artist sample
4. Confirm → real run (name auto-derives if omitted)
```

### Pattern C — Image attachment

```
1. Vision-extract all artists. Don't skip smaller print — sub-headliners,
   late-night sets, side stages all matter.
2. List artists back to user for visual sanity-check.
3. Continue as Pattern A from step 2.
```

### Pattern D — Existing file path

```
1. Skip extraction.
2. <python> spotify_playlist.py --artists <path> --name "<derived>" --dry-run
3. Continue as Pattern A from step 4.
```

### Compound asks (most realistic case)

"Playlist for Lollapalooza but only the hip-hop artists" → Pattern A + filter the extracted lineup yourself before writing the file. "Bon Iver Sable tour" → Pattern B with `--tour "Sable"` (researched first if you don't already know). "Coachella 2027 weekend 1 only" → Pattern A + filter to weekend-1 acts.

---

## Review loop (top-tracks mode only)

After the dry-run, the script's output has two sections worth surfacing:

- **`Failed (N)`** — no tracks found at all
- **`Review needed (N)`** — same-name ambiguity or name mismatch

For each, present **display name + matched artist + top track + alternatives** to the user. Offer three actions:
1. **Keep** (it's right) — leave the line alone
2. **Override** — ask for the right Spotify URL, append `|<url>` to the line in the lineup file
3. **Drop** — comment the line out (`#`)

Apply changes, re-run the dry-run. Cache makes re-runs nearly free. Loop until clean or user is satisfied.

Concert mode usually doesn't need this — Setlist.fm's artist match is explicit (one match, you've already shown them the name).

---

## Existing-playlist check (before any real write)

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
    total = existing.get("tracks", {}).get("total", "?")
    print(f"EXISTS:{existing['id']}:{total}")
else:
    print("NEW")
```

If `EXISTS:...:N`, ask: "A playlist named '<name>' already exists with N tracks. **Append** new tracks, or **wipe and rebuild**?" Pass `--replace` for wipe.

---

## Final run

Confirm:
- Public or private (default private)
- Description (suggest something based on source: "Top tracks per artist · ACL 2026 lineup", "Phoebe Bridgers — last 10 shows" — short and self-explanatory)
- Append vs. replace

Run for real. Report the playlist URL from the script's final line.

---

## Operating notes

- **Be terse during execution.** Don't echo full logs — summarize. The user wants to know what's flagged for review and what worked, not every line.
- **Be honest about misses.** If the script can't find tracks for an artist (legit Spotify gap), say so. Don't pretend.
- **Web research is a budget.** Three searches per request max unless the user signals they want thoroughness. They're describing playlists, not asking for dissertations.
- **Rate limit handling.** Spotify daily quota is ~150 calls per rolling 24h. If hit, kill the Python process immediately (don't let spotipy sleep through a 24h Retry-After) and explain. With `LASTFM_API_KEY` set, discovery doesn't touch Spotify's quota.
- **Cache awareness.** Top-tracks mode caches per-artist; mention cache hits vs new ("100/120 from cache, 20 new resolutions"). Concert mode doesn't cache (setlists change as artists play new shows — and the API is cheap).
- **No autonomous invocation.** `disable-model-invocation: true` — the user must type `/playlist`. Don't try to launch this on their behalf in other conversations.
