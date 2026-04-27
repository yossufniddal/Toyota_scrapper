"""
HJ Motors — Trim Configuration
================================
Manual trim configuration. Used as a FALLBACK when the HJ catalog API is
unavailable (see search_engine.fetch_official_trims priority order).

How to add a trim:
    ("brand", "model", "year"): [
        TrimDefinition(
            id          = "TRIM_CODE",
            name        = "Official Name",
            name_ar     = "الاسم بالعربي",
            msrp        = 120000,           # SAR including VAT 15%
            engine      = "2.5L HEV 226hp",
            keywords    = ["keyword1", "كلمة2"],
            match_score = 0.90,
        ),
    ],

NOTE: This stub file was created with an empty TRIM_CONFIG so the rest of the
codebase imports cleanly. The original project's TRIM_CONFIG had hundreds of
entries for Toyota and Lexus models. To restore them, paste your original
TRIM_CONFIG dict back in below.

Auto-trim discovery (services/auto_trims.py) fetches trims live from the HJ
catalog API on every search and is the PRIMARY source — this manual config
only kicks in if that API call fails.
"""

from dataclasses import dataclass


@dataclass
class TrimDefinition:
    id:          str
    name:        str
    name_ar:     str
    msrp:        int          # SAR including VAT — 0 = unknown
    engine:      str
    keywords:    list[str]
    match_score: float = 0.85


# ──────────────────────────────────────────────────────────────
#  TRIM_CONFIG — populate from your project's original
# ──────────────────────────────────────────────────────────────
#
# The original project shipped with hundreds of TrimDefinition entries
# keyed by (brand, model, year). Paste them back into this dict.
#
# Example shape:
#   ("toyota", "camry", "2025"): [
#       TrimDefinition(
#           id="camry25-le_automatic",
#           name="le automatic",
#           name_ar="تويوتا كامري ال اي",
#           msrp=0,
#           engine="petrol",
#           keywords=["le", "ال اي", "le automatic"],
#           match_score=0.88,
#       ),
#       # ...
#   ],

TRIM_CONFIG: dict[tuple, list[TrimDefinition]] = {}


# ──────────────────────────────────────────────────────────────
#  Helper functions — DO NOT modify
# ──────────────────────────────────────────────────────────────

def get_trims(brand: str, model: str, year: int) -> list[TrimDefinition]:
    """Returns trims for brand/model/year or empty list."""
    return TRIM_CONFIG.get((brand.lower(), model.lower(), str(year)), [])


def get_all_configured() -> list[dict]:
    """Summary of all configured brand/models."""
    return [
        {
            "brand": k[0], "model": k[1],
            "trim_count": len(v),
            "trims": [{"id": t.id, "name": t.name, "msrp": t.msrp} for t in v],
        }
        for k, v in TRIM_CONFIG.items()
    ]


def normalize_from_config(title: str, brand: str, model: str, year: int) -> tuple[str, str, float]:
    """
    Match a listing title against configured trims.
    Supports:
    - Bilingual keyword matching (Arabic + English)
    - Common abbreviations (YX, Y+, ...)
    - Partial matching as fallback
    """
    trims = get_trims(brand, model, year)
    if not trims:
        return f"{model} Standard", "no trims configured", 0.50

    t = title.lower().strip()

    # Round 1: precise keyword match
    for trim in trims:
        for kw in trim.keywords:
            if kw and len(kw) >= 2 and kw in t:
                return trim.name, f"keyword: «{kw}»", trim.match_score

    # Round 2: direct trim name match
    for trim in trims:
        trim_name_l = trim.name.lower()
        trim_ar_l   = trim.name_ar.lower()
        if trim_name_l in t or trim_ar_l in t:
            return trim.name, f"trim name: «{trim.name}»", trim.match_score

    # Round 3 / 4: word overlap fallback
    best, best_score = trims[-1], 0.0
    for trim in trims:
        words = [w for w in trim.name.lower().split() if len(w) > 2]
        hits = sum(1 for w in words if w in t)
        score = (hits / len(words) * 0.4) if words else 0
        if score > best_score:
            best_score, best = score, trim

    if best_score < 0.2:
        return trims[-1].name, "no match — fallback", 0.40

    return best.name, "partial match", max(0.45, best_score)
