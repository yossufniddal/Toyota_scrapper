"""
Test the full Claude-powered market analysis (search_engine.ai_fallback).

Returns the rich JSON: marketInsight (Arabic), per-trim MSRP +
priceAnalysis, and competitorAnalysis (summary / opportunities /
threats / recommendation — i.e. SWOT).

Reads ANTHROPIC_API_KEY from .env.

Usage:
    python test_ai_fallback.py
    python test_ai_fallback.py toyota fortuner 2026
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from search_engine import ai_fallback, transform_catalog
from services.cache import cache_set


def load_env_key() -> str:
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get("ANTHROPIC_API_KEY", "")


async def seed_catalog_cache():
    """ai_fallback calls get_model_trims_from_catalog which reads the cache."""
    url = "https://appw.hassanjameelapp.com/en/api/finance/index"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        await cache_set("car_catalog", transform_catalog(r.json()))


async def main(brand: str, model: str, year: int):
    key = load_env_key()
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not found in .env or environment")
        sys.exit(1)

    print(f"\n=== Calling ai_fallback for {year} {brand} {model} ===\n")
    print("Seeding catalog cache from HJ API...")
    await seed_catalog_cache()
    print("Calling Claude (this takes 10-30s)...\n")

    data = await ai_fallback(brand, model, year, source_ids=[], key=key)

    # ── Pretty-print the structured response ─────────────
    print(f"vehicle:     {data.get('vehicle')}")
    print(f"searchDate:  {data.get('searchDate')}")
    print(f"isAIFallback: {data.get('isAIFallback')}")

    pr = data.get("officialPriceRange", {})
    print(f"price range: {pr.get('min'):,} - {pr.get('max'):,} SAR")

    print(f"\n--- marketInsight ---")
    print(data.get("marketInsight", "")[:500])

    print(f"\n--- trims ({len(data.get('trims', []))}) ---")
    for t in data.get("trims", []):
        msrp = t.get("officialMSRP", 0)
        pa = t.get("priceAnalysis", {})
        print(f"  • {t.get('officialName', '?'):<35} "
              f"MSRP={msrp:>8,}  "
              f"market: {pa.get('marketMin', 0):>7,}-{pa.get('marketMax', 0):>7,} "
              f"avg {pa.get('marketAvg', 0):>7,}  trend={pa.get('trend', '?')}")

    ca = data.get("competitorAnalysis", {})
    if ca:
        print(f"\n--- competitorAnalysis (SWOT) ---")
        print(f"summary:        {ca.get('summary', '')[:200]}")
        print(f"opportunities:  {ca.get('opportunities', [])}")
        print(f"threats:        {ca.get('threats', [])}")
        print(f"recommendation: {ca.get('recommendation', '')[:200]}")

    # ── Dump the raw JSON to a file so you can inspect everything ──
    out_path = Path(__file__).parent / f"ai_fallback_{brand}_{model}_{year}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n→ Full JSON written to {out_path.name}")


if __name__ == "__main__":
    args = sys.argv[1:]
    brand = args[0] if len(args) > 0 else "toyota"
    model = args[1] if len(args) > 1 else "camry"
    year = int(args[2]) if len(args) > 2 else 2025
    asyncio.run(main(brand, model, year))
