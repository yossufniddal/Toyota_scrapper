"""
Per-trim, per-source price aggregator (5 sources) → market-intelligence schema.

Usage:
    python test_price_aggregator.py
    python test_price_aggregator.py corolla 2025
    python test_price_aggregator.py toyota fortuner 2026
    python test_price_aggregator.py toyota "land cruiser" 2025
    python test_price_aggregator.py lexus rx 2025

    # add `--ai` to also fill marketInsight + competitorAnalysis via Claude
    python test_price_aggregator.py lexus rx 2025 --ai
"""
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from price_aggregator import aggregate_prices


def fmt(n: int) -> str:
    return f"{n:,}" if n else "—"


def load_anthropic_key() -> str:
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get("ANTHROPIC_API_KEY", "")


async def main(brand: str, model: str, year: int, enrich: bool):
    print(f"\n=== {year} {brand} {model} — per-trim per-source averages ===\n")
    data = await aggregate_prices(
        brand, model, year,
        enrich_with_ai=enrich,
        anthropic_key=load_anthropic_key() if enrich else "",
    )

    if data.get("error"):
        print(f"\nERROR: {data['error']}")
        return

    # ── Source columns: official source first, then marketplaces. ──
    sources = data.get("sources_queried") or [
        "toyota.com.sa", "haraj.com.sa", "syarah.com",
        "ksa.Motory.com", "ksa.yallamotor.com",
    ]
    short = {
        "toyota.com.sa": "Toyota",
        "lexus.com.sa": "Lexus",
        "haraj.com.sa": "Haraj",
        "syarah.com": "Syarah",
        "ksa.Motory.com": "Motory",
        "ksa.yallamotor.com": "YallaMotor",
    }

    # Bucket each trim's listings by source so we can compute avg+count per cell.
    print()
    cols = ["Trim"] + [short.get(s, s) for s in sources]
    widths = [35] + [13] * len(sources)
    line = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("-" * len(line))

    for trim in data["trims"]:
        per_source: dict[str, list[int]] = defaultdict(list)
        for l in trim["listings"]:
            p = l.get("price", 0)
            if p > 0:
                per_source[l["source"]].append(p)
        cells = [trim["officialName"][:35].ljust(35)]
        for s in sources:
            ps = per_source.get(s, [])
            if not ps:
                cells.append("—".ljust(13))
            else:
                avg = int(sum(ps) / len(ps))
                cells.append(f"{fmt(avg)} (n={len(ps)})".ljust(13))
        print(" | ".join(cells))

    # ── Totals + reference URLs ──
    t = data.get("totals", {})
    print(f"\nListings: haraj={t.get('haraj_listings', 0)}  "
          f"syarah={t.get('syarah_listings', 0)}  "
          f"motory={t.get('motory_listings', 0)}  "
          f"yallamotor={t.get('yallamotor_listings', 0)}")
    print(f"Official price range: {data['officialPriceRange']['min']:,} – "
          f"{data['officialPriceRange']['max']:,} SAR")

    print("\nReference URLs:")
    for sid, info in (data.get("references") or {}).items():
        print(f"  {short.get(sid, sid):<11}  {info['url']}")

    # ── AI-enriched fields (if --ai was used) ──
    if data.get("marketInsight"):
        print(f"\nmarketInsight:\n  {data['marketInsight']}")
    ca = data.get("competitorAnalysis") or {}
    if ca:
        print(f"\ncompetitorAnalysis:")
        if ca.get("summary"):
            print(f"  summary:        {ca['summary'][:200]}")
        for key in ("opportunities", "threats"):
            vals = ca.get(key, [])
            if vals:
                print(f"  {key}:")
                for v in vals:
                    print(f"    - {v}")
        if ca.get("recommendation"):
            print(f"  recommendation: {ca['recommendation'][:200]}")

    # ── Dump full JSON ──
    out = Path(__file__).parent / f"prices_{brand}_{model.replace(' ', '_')}_{year}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n→ Full JSON: {out.name}\n")


def _is_year(s: str) -> bool:
    return s.isdigit() and 1990 <= int(s) <= 2100


def _parse_args(args: list[str]) -> tuple[str, str, int, bool]:
    enrich = "--ai" in args
    args = [a for a in args if a != "--ai"]
    if not args:
        return "toyota", "camry", 2025, enrich
    if len(args) == 1:
        return "toyota", args[0], 2025, enrich
    if len(args) == 2:
        if _is_year(args[1]):
            return "toyota", args[0], int(args[1]), enrich
        return args[0], args[1], 2025, enrich
    return args[0], args[1], int(args[2]), enrich


if __name__ == "__main__":
    brand, model, year, enrich = _parse_args(sys.argv[1:])
    asyncio.run(main(brand, model, year, enrich))
