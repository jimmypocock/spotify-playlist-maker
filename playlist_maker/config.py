"""Config directory resolution.

The CLI runs in two contexts:

1. **Dev mode** — invoked from inside this repo (e.g. `python spotify_playlist.py …`).
   .env, .spotify_cache, .resolution_cache.json all live in the repo root (CWD).

2. **Plugin mode** — installed via Claude Code's plugin marketplace. The script
   lives in the plugin install dir (read-only-ish), but the user's credentials
   and per-user caches need a persistent home that isn't tied to whatever
   directory they happened to be in when they ran /playlist.

Resolution order for the config dir (where .env / caches live):

1. $SPOTIFY_PLAYLIST_CONFIG_DIR if set (explicit override)
2. ~/.spotify-playlist-maker/ if it exists (plugin-mode default)
3. The current working directory (dev-mode default — keeps existing repo
   workflow unchanged)

Lineup files default to <config_dir>/lineups/ — they're user-generated
artifacts that should persist across sessions and not litter random CWDs.
The CLI's --artists flag still accepts any explicit path.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "SPOTIFY_PLAYLIST_CONFIG_DIR"
DEFAULT_PLUGIN_DIR = Path.home() / ".spotify-playlist-maker"


def config_dir() -> Path:
    """Return the directory where .env and cache files live. See module docstring."""
    override = os.getenv(ENV_VAR)
    if override:
        return Path(override).expanduser()
    if DEFAULT_PLUGIN_DIR.exists():
        return DEFAULT_PLUGIN_DIR
    return Path.cwd()


def env_path() -> Path:
    return config_dir() / ".env"


def oauth_cache_path() -> Path:
    return config_dir() / ".spotify_cache"


def resolution_cache_path() -> Path:
    return config_dir() / ".resolution_cache.json"


def lineups_dir() -> Path:
    """Default lineups directory. CLI users can still pass any --artists path."""
    return config_dir() / "lineups"
