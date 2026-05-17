#!/usr/bin/env python3
"""Entry-point shim. All logic lives in the playlist_maker/ package.

Kept as a file at repo root so `python spotify_playlist.py ...` (the
invocation the /playlist skill and the README document) keeps working.
"""

import sys

from playlist_maker.cli import main

if __name__ == "__main__":
    sys.exit(main())
