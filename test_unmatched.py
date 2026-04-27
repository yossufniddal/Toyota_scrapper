"""
Diagnostic: show every listing each source returned and which Toyota trim
it got attributed to (or why it didn't match anything).

Reveals two failure modes:
  1. Source uses different naming     → e.g. YallaMotor's "GLE" ≠ Toyota's "E"
  2. Source doesn't sell that trim    → listing simply missing from inventory
"""
import asyncio
import sys

from playwright.async_api import async_playwright

from price_aggregator import (
    scrape_toyota_official, scrape_lexus_official, scrape_haraj_for_trim,
    scrape_syarah_model, scrape_motory_model, scrape_yallamotor_model,
    _best_trim_for_listing,
)


async def main(brand: str, model: str, year: int):
    print(f"\n=== {year} {brand} {model} — listing-by-listing audit ===\n")
    if brand.lower() == "lexus":
        official = await scrape_lexus_official(model, year)
        official_label = "Lexus.com.sa"
    else:
        official = await scrape_toyota_official(model, year)
        official_label = "Toyota.com.sa"
    trims_with_msrp = [(t["trim"], t["price"]) for t in official]
    trim_names = [t["trim"] for t in official]
    print(f"{official_label} trims: {trim_names}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])

        async def _syarah():
            return await scrape_syarah_model(browser, brand, model, year)

        async def _motory():
            return await scrape_motory_model(browser, brand, model, year)

        syarah, motory = await asyncio.gather(_syarah(), _motory())
        yallamotor = await scrape_yallamotor_model(browser, brand, model, year)

        await browser.close()

    for source_name, listings in (
        ("syarah.com", syarah),
        ("ksa.Motory.com", motory),
        ("ksa.yallamotor.com", yallamotor),
    ):
        print(f"── {source_name}  ({len(listings)} listings) ──")
        for l in listings:
            title = l["title"]
            price = l["price"]
            best = _best_trim_for_listing(title, price, trims_with_msrp)
            if best:
                tag = f"→ {best}"
            else:
                tag = f"→ NO MATCH (trim differs from {official_label})"
            print(f"  {price:>8,} SAR  {title[:70]:<70} {tag}")
        print()


def _is_year(s: str) -> bool:
    return s.isdigit() and 1990 <= int(s) <= 2100


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        brand, model, year = "toyota", "camry", 2025
    elif len(args) == 1:
        brand, model, year = "toyota", args[0], 2025
    elif len(args) == 2:
        if _is_year(args[1]):
            brand, model, year = "toyota", args[0], int(args[1])
        else:
            brand, model, year = args[0], args[1], 2025
    else:
        brand, model, year = args[0], args[1], int(args[2])
    asyncio.run(main(brand, model, year))
