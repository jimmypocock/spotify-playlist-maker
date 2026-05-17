"""Confidence scoring + the dry-run review block.

Feb 2026 stripped popularity/followers/genres from Dev Mode artist responses,
so we have only two signals left: literal name distance, and same-name
ambiguity (multiple Spotify artists sharing the matched name).
"""

from __future__ import annotations

from pathlib import Path

from .models import ResolveResult, normalize_name


def score_confidence(result: ResolveResult) -> None:
    """Tag result.confidence as high/medium/low.

    Two signals only:
    1. Name distance — input display vs. matched name after normalization.
    2. Same-name ambiguity — alternative hits share the matched name.
    """
    if not result.artist_id:
        return
    reasons: list[str] = []
    severity = 0  # 0=high, 1=medium, 2=low

    norm_input = normalize_name(result.entry.display_name)
    norm_match = normalize_name(result.matched_name or "")

    # Signal 1: name match (after diacritic/case/parenthetical normalization).
    # Substring containment counts as a match — handles "Geese" ↔ "Geese (band)".
    name_matches = (
        norm_input == norm_match
        or norm_input in norm_match
        or norm_match in norm_input
    )
    if not name_matches:
        reasons.append(f"name mismatch ('{result.matched_name}')")
        severity = max(severity, 2)

    # Signal 2: same-name ambiguity — alternative hits share the matched name.
    same_name_alts = [
        a for a in result.alternatives if normalize_name(a.name) == norm_match
    ]
    if same_name_alts:
        reasons.append(
            f"{len(same_name_alts) + 1} artists named '{result.matched_name}'"
        )
        severity = max(severity, 1)

    result.confidence = ["high", "medium", "low"][severity]
    result.confidence_reasons = reasons


def print_review_block(results: list[ResolveResult], artists_file: Path) -> None:
    """Print a per-artist review section for medium/low confidence matches."""
    needs_review = [r for r in results if r.artist_id and r.confidence != "high"]
    if not needs_review:
        return
    print(f"\nReview needed ({len(needs_review)}):\n")
    for r in needs_review:
        tag = "LOW " if r.confidence == "low" else "MED "
        sample = f"top track: {r.sample_track!r}" if r.sample_track else "no top track"
        print(f"  ⚠ {tag} {r.entry.display_name}")
        print(f"      matched: '{r.matched_name}'  ({sample})")
        print(f"      reasons: {'; '.join(r.confidence_reasons)}")
        if r.alternatives:
            print(f"      alternatives:")
            for alt in r.alternatives[:3]:
                print(f"        • {alt.name}  (id: {alt.artist_id})")
        print(f"      fix: in {artists_file}, change line to "
              f"'{r.entry.display_name}|<more specific search query>'")
        print()
