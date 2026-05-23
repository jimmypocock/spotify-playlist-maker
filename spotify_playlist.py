#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "spotipy>=2.23",
#     "python-dotenv>=1.0",
#     "requests>=2.31",
# ]
# ///
"""Entry-point shim. All logic lives in the playlist_maker/ package.

Kept as a file at repo root so the `spotify_playlist.py` invocation that
the /playlist skill and README document keeps working.

Plugin users invoke via `uv run` — uv reads the PEP 723 metadata above,
creates a cached isolated env with the listed deps, and runs the script.
First invocation pays a one-time install; subsequent runs hit cache.

Dev mode (running from inside this repo) can still use the local .venv
directly — the PEP 723 metadata is just a comment to non-uv interpreters.
"""

import sys

from playlist_maker.cli import main

if __name__ == "__main__":
    sys.exit(main())
