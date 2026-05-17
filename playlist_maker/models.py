"""Data shapes used across the package, plus a couple of related helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# Spotify artist IDs are 22-char base62. We accept any of these override forms.
SPOTIFY_ARTIST_URI_RE = re.compile(r"^spotify:artist:([A-Za-z0-9]{22})$")
SPOTIFY_ARTIST_URL_RE = re.compile(r"https?://open\.spotify\.com/artist/([A-Za-z0-9]{22})")
SPOTIFY_BARE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_artist_override(s: str) -> tuple[Optional[str], str]:
    """If s is a Spotify URI/URL/bare ID, return (artist_id, original_string).
    Otherwise return (None, s) — caller treats s as a search query."""
    s = s.strip()
    for regex in (SPOTIFY_ARTIST_URI_RE, SPOTIFY_ARTIST_URL_RE):
        m = regex.search(s)
        if m:
            return m.group(1), s
    if SPOTIFY_BARE_ID_RE.match(s):
        return s, s
    return None, s


def normalize_name(s: str) -> str:
    """Lowercase, strip diacritics + parentheticals + extra whitespace.
    Used for confidence-scoring's name-match heuristic."""
    nfkd = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    stripped = re.sub(r"\([^)]*\)", "", no_diacritics.lower())
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass
class ArtistEntry:
    """One line from the artists file."""
    display_name: str                # what was on the poster, used for logs/output
    search_query: str                # used when searching by name (also for track search)
    artist_id: Optional[str] = None  # set when override is a Spotify URI/URL/ID

    @classmethod
    def parse(cls, line: str) -> Optional["ArtistEntry"]:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        if "|" in line:
            display, override = (part.strip() for part in line.split("|", 1))
            artist_id, _ = parse_artist_override(override)
            # Track-search query falls back to display name when override is an ID.
            search_query = display if artist_id else override
            return cls(display_name=display, search_query=search_query, artist_id=artist_id)
        return cls(display_name=line, search_query=line)


@dataclass
class ArtistCandidate:
    """An alternative search hit kept around so the user can review mismatches."""
    artist_id: str
    name: str


@dataclass
class ResolveResult:
    entry: ArtistEntry
    artist_id: Optional[str] = None
    matched_name: Optional[str] = None
    track_uris: list[str] = field(default_factory=list)
    sample_track: Optional[str] = None  # name of top track, shown for sanity-check
    alternatives: list[ArtistCandidate] = field(default_factory=list)
    confidence: str = "high"  # "high" | "medium" | "low"
    confidence_reasons: list[str] = field(default_factory=list)
    error: Optional[str] = None
