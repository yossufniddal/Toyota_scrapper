"""
HJ Motors — Playwright HTML Scrapers (STUB)

This is a structurally-complete stub of the Playwright scrapers. It exposes
the same function signatures as the original so search_engine.py and shared.py
import cleanly, but each scraper returns an empty list rather than running
a real browser.

Replace this file with the original ~1500-line scraper_playwright.py to
restore live scraping for:
  - Syarah     (playwright_syarah)
  - Haraj      (playwright_haraj)
  - Toyota.com.sa  (playwright_toyota_sa)
  - Lexus.com.sa   (playwright_lexus_sa)
  - Motory     (playwright_motory)
  - YallaMotor (playwright_yallamotor)

The original also exports helpers that other modules call:
  - PLAYWRIGHT_OK            (boolean import flag)
  - run_playwright_safe      (Windows-safe coroutine runner)
  - run_all_playwright_scrapers  (shared-browser orchestrator)
  - infer_vat_status         (price+source → VAT status estimator,
                              imported by search_engine._attach_source_stats)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("hj-price-intel.scraper")

try:
    from playwright.async_api import async_playwright  # noqa: F401
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False


async def run_playwright_safe(coro_fn, *args, **kwargs):
    """Stub — original wrapped Playwright in a ProactorEventLoop thread for Windows."""
    return []


async def run_all_playwright_scrapers(tasks: list[dict]) -> list[tuple]:
    """Stub — original ran a shared-browser orchestrator across tasks."""
    return [(t.get("sid", "unknown"), []) for t in tasks]


# ── Per-source scraper stubs ──────────────────────────────────

async def playwright_syarah(query: str, max_results: int = 15,
                             brand: str = "", model: str = "", year: int = 0,
                             _browser=None) -> list[dict]:
    return []


async def playwright_haraj(query: str, max_results: int = 15,
                            brand: str = "", model: str = "", year: int = 0,
                            _browser=None) -> list[dict]:
    return []


async def playwright_toyota_sa(model: str, year: int = 0, _browser=None) -> list[dict]:
    return []


async def playwright_lexus_sa(model: str, year: int = 0, _browser=None) -> list[dict]:
    return []


async def playwright_motory(query: str, max_results: int = 10,
                             brand: str = "", model: str = "", year: int = 0,
                             _browser=None) -> list[dict]:
    return []


async def playwright_yallamotor(query: str, max_results: int = 15,
                                 brand: str = "", model: str = "", year: int = 0,
                                 _browser=None) -> list[dict]:
    return []


# ── VAT inference (imported by search_engine._attach_source_stats) ──

def detect_vat_from_text(text: str) -> str | None:
    """
    Look for explicit VAT mentions in listing text.
    Returns: "included" | "excluded" | None
    """
    if not text:
        return None
    t = text.lower().strip()
    included = [
        "شامل الضريبة", "شامل ضريبة", "شامل vat", "شامل ال vat",
        "شامل قيمة مضافة", "including vat", "incl vat", "inc vat",
        "vat included", "with vat", "شامل 15%", "شامل ال 15",
        "السعر شامل", "بالضريبة", "ضريبة مضمنة", "inclusive",
    ]
    excluded = [
        "بدون ضريبة", "بدون الضريبة", "قبل الضريبة", "بدون vat",
        "excluding vat", "excl vat", "exc vat", "vat excluded",
        "without vat", "not including vat", "بدون ال 15",
        "السعر قبل", "السعر نت", "net price", "خارج الضريبة", "exclusive",
    ]
    for p in included:
        if p in t:
            return "included"
    for p in excluded:
        if p in t:
            return "excluded"
    return None


def infer_vat_status(price: int, msrp: int = 0, source: str = "",
                      listing_text: str = "") -> dict:
    """
    Determines whether a listing price includes VAT.

    Priority:
      1. Explicit text mention (highest accuracy)
      2. Official source (always inclusive)
      3. Mathematical comparison with MSRP
      4. Round-number heuristic
    """
    # 1. Explicit text mention
    text_vat = detect_vat_from_text(listing_text)
    if text_vat == "included":
        return {"vat_status": "included", "confidence": "high",
                "price_ex_vat": int(price / 1.15), "price_inc_vat": price,
                "reason": "Listing text explicitly says: VAT included"}
    if text_vat == "excluded":
        return {"vat_status": "excluded", "confidence": "high",
                "price_ex_vat": price, "price_inc_vat": int(price * 1.15),
                "reason": f"Listing text says: VAT excluded | with VAT: {int(price * 1.15):,}"}

    # 2. Official source — always inclusive
    if source in ("toyota.com.sa", "ksa.Motory.com", "ksa.yallamotor.com",
                   "syarah.com", "lexus.com.sa"):
        return {"vat_status": "included", "confidence": "high",
                "price_ex_vat": int(price / 1.15), "price_inc_vat": price,
                "reason": "Official source — VAT 15% included"}

    # 3. Compare with MSRP
    if msrp and msrp > 0:
        ratio = price / msrp
        if 0.94 <= ratio <= 1.06:
            return {"vat_status": "included", "confidence": "high",
                    "price_ex_vat": int(price / 1.15), "price_inc_vat": price,
                    "reason": f"Within 6% of MSRP {msrp:,} → VAT included"}
        if abs(int(price * 1.15) - msrp) / msrp < 0.03:
            return {"vat_status": "excluded", "confidence": "high",
                    "price_ex_vat": price, "price_inc_vat": int(price * 1.15),
                    "reason": f"{price:,} × 1.15 ≈ MSRP {msrp:,} → VAT excluded"}
        if 1.06 < ratio <= 1.20:
            return {"vat_status": "included", "confidence": "medium",
                    "price_ex_vat": int(price / 1.15), "price_inc_vat": price,
                    "reason": f"{(ratio-1)*100:.0f}% above MSRP → VAT included + dealer markup"}

    # 4. Round-number heuristic
    if price % 100 == 0:
        return {"vat_status": "excluded", "confidence": "medium",
                "price_ex_vat": price, "price_inc_vat": int(price * 1.15),
                "reason": f"Round number — likely without VAT | with VAT: {int(price * 1.15):,}"}

    return {"vat_status": "included", "confidence": "medium",
            "price_ex_vat": int(price / 1.15), "price_inc_vat": price,
            "reason": "Precise number — looks like an official VAT-included price"}


def get_scrape_health() -> dict:
    """Stub — original tracked per-source failure counts."""
    return {"failures": {}, "healthy": [], "degraded": []}
