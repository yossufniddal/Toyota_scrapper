"""
Quick test: show all trims returned for a make/model/year.

Exercises both code paths that fetch_official_trims uses:
  1. services/auto_trims.fetch_trims_from_catalog  (PRIMARY)
  2. search_engine.get_model_trims_from_catalog    (the [:5] cap fix lives here)

Usage:
    python test_trims.py                    # defaults to toyota camry 2025
    python test_trims.py toyota fortuner 2026
    python test_trims.py lexus lx 2025
"""
import asyncio
import sys

import httpx

from services.auto_trims import fetch_trims_from_catalog
from services.cache import cache_set
from search_engine import transform_catalog, get_model_trims_from_catalog


async def fetch_raw_catalog():
    url = "https://appw.hassanjameelapp.com/en/api/finance/index"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def main(brand: str, model: str, year: int):
    print()
    print(f"=== {year} {brand} {model} ===")
    print()

    # ── Path 1: services/auto_trims.fetch_trims_from_catalog ──
    # This is the primary trim source per fetch_official_trims priority order.
    # Hits the HJ catalog API, then enriches each trim with bilingual keywords.
    print("[1] fetch_trims_from_catalog  (live HJ API, full trim records)")
    trims = await fetch_trims_from_catalog(brand, model, year, anthropic_key="")
    print(f"    → {len(trims)} trims")
    for t in trims:
        print(f"      • {t['name']:<48} engine: {t['engine']}")

    # ── Path 2: search_engine.get_model_trims_from_catalog ────
    # Reads from the cached catalog. This is the function the [:5] cap
    # was inside of. We seed the cache with the raw API response, then call.
    print()
    print("[2] get_model_trims_from_catalog  (the function with the [:5] fix)")
    raw = await fetch_raw_catalog()
    transformed = transform_catalog(raw)
    await cache_set("car_catalog", transformed)
    names = await get_model_trims_from_catalog(brand, model, year)
    print(f"    → {len(names)} trim names")
    for n in names:
        print(f"      • {n}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    brand = args[0] if len(args) > 0 else "toyota"
    model = args[1] if len(args) > 1 else "camry"
    year = int(args[2]) if len(args) > 2 else 2025
    asyncio.run(main(brand, model, year))
