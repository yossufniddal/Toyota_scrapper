"""Quick probe: fetch each marketplace URL and show what's there."""
import asyncio
import re
from playwright.async_api import async_playwright

URLS = [
    ("syarah.com",         "https://syarah.com/autos/toyota/camry/2025?type=new"),
    ("syarah.com (alt)",   "https://syarah.com/cars/toyota/camry?year=2025&type=new"),
    ("ksa.Motory.com",     "https://ksa.Motory.com/en/new-cars/toyota/camry/2025"),
    ("ksa.Motory.com (alt)", "https://ksa.Motory.com/sa/new-cars/toyota/camry"),
    ("yallamotor en",      "https://ksa.yallamotor.com/en/new-cars/toyota/camry/2025"),
    ("yallamotor root",    "https://ksa.yallamotor.com/new-cars/toyota/camry/2025"),
    ("yallamotor short",   "https://ksa.yallamotor.com/new-cars/toyota/camry"),
]

async def probe(browser, label, url):
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="ar-SA",
        viewport={"width": 1366, "height": 768},
    )
    page = await ctx.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,svg,mp4}", lambda r: r.abort())
    print(f"\n--- {label}\n    {url}")
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        print(f"    status: {resp.status if resp else '?'}  final url: {page.url}")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        html = await page.content()
        print(f"    html size: {len(html):,} bytes")
        # NEXT_DATA presence
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        print(f"    has __NEXT_DATA__: {bool(nd)} (len={len(nd.group(1)):,} if present)" if nd else "    has __NEXT_DATA__: False")
        # Price-like numbers
        prices = re.findall(r"\b\d{2,3}(?:,\d{3})\b", html)
        print(f"    price-shaped numbers in HTML: {len(prices)} (sample: {prices[:5]})")
        # Specific selectors
        cards1 = await page.query_selector_all("[id^='posts-card-cash-price']")
        cards2 = await page.query_selector_all("app-car-card")
        cards3 = await page.query_selector_all(".car-card")
        cards4 = await page.query_selector_all("h3")
        print(f"    selectors: posts-card-cash-price={len(cards1)} app-car-card={len(cards2)} .car-card={len(cards3)} h3={len(cards4)}")
        # First 200 chars of body text
        try:
            text = await page.evaluate("() => document.body.innerText.substring(0, 200)")
            print(f"    body text (first 200): {text!r}")
        except Exception:
            pass
    except Exception as e:
        print(f"    ERROR: {e}")
    finally:
        await ctx.close()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for label, url in URLS:
            await probe(browser, label, url)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
