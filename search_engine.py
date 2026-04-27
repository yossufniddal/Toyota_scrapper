"""
Search orchestration logic: httpx scrapers, AI fallback, trim fetching,
source stats attachment, post-search persistence, and history enrichment.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime

logger = logging.getLogger("hj-price-intel.search")

import httpx

from shared import (
    sources_store, ANTHROPIC_API_URL, SCRAPE_TIMEOUT,
    PLAYWRIGHT_OK, FAST_SCRAPERS_OK,
    pw_run, TrimInfo, _trim_cache, TRIM_CACHE_TTL, CarCatalog,
    normalize_city,
)
# Conditional imports from shared for Playwright functions
from shared import (
    run_fast_scrapers,
    run_all_playwright_scrapers,
)
from trims_config import get_trims, normalize_from_config
from trim_classifier import classify_and_structure
from services.cache import (
    cache_get, cache_set,
    snapshot_save, snapshot_get_history, snapshot_calc_trend,
    dom_record, dom_avg_for_trim,
)
from services.pricing_engine import _attach_dealer_pricing
from services.auto_trims import fetch_trims_from_catalog

# ── httpx fallback scrapers ─────────────────────────────────────
def _qenc(s): return s.replace(" ", "+")
def _int(v):
    if v is None: return 0
    try: return int(re.sub(r"[^\d]","",str(v)) or "0")
    except Exception: return 0

def _L(source, source_name, listed_as, condition, price, mileage,
        location, seller_name, seller_type, days_ago, image, url, note, confidence, reason):
    return {"source":source,"sourceName":source_name,"listedAs":listed_as,"condition":condition,
            "price":price,"mileage":mileage,"location":location,"city":normalize_city(location),
            "sellerName":seller_name,
            "sellerType":seller_type,"postedDaysAgo":days_ago,"imageUrl":image,"url":url,
            "priceNote":note,"matchConfidence":confidence,"matchReason":reason}

def _img(q):
    q=q.lower()
    if any(x in q for x in ["patrol","prado","land cruiser","fortuner","rav4","lx","gx"]): return "https://images.unsplash.com/photo-1519641471654-76ce0107ad1b?w=300&q=80"
    if any(x in q for x in ["hilux","f-150","ranger"]): return "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=300&q=80"
    return "https://images.unsplash.com/photo-1621007947382-bb3c3994e3fb?w=300&q=80"


async def httpx_yallamotor(query, client):
    out = []
    parts = query.split()
    year = parts[0] if parts and parts[0].isdigit() else ""
    brand = parts[1] if len(parts)>1 else ""
    model = " ".join(parts[2:]) if len(parts)>2 else ""
    for url in [f"https://www.ksa.yallamotor.com/api/new-cars?brand={brand}&model={model}&year={year}&country=sa&limit=12",
                f"https://www.ksa.yallamotor.com/api/v2/listings?q={_qenc(query)}&country=sa&limit=12"]:
        try:
            r = await client.get(url, headers={"Referer":"https://www.ksa.yallamotor.com/"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code==200 and "json" in r.headers.get("content-type",""):
                cars = r.json().get("data") or r.json().get("cars") or r.json().get("results") or []
                for c in cars[:10]:
                    p = _int(c.get("price") or c.get("base_price") or c.get("min_price"))
                    if p > 20000:
                        out.append(_L("ksa.yallamotor.com","YallaMotor.com",c.get("name") or query,
                            "جديدة",p,"0 كم",c.get("city") or "السعودية",
                            c.get("dealer_name") or "YallaMotor","dealer",0,
                            c.get("image") or _img(query),c.get("url") or "https://www.ksa.yallamotor.com",
                            (c.get("specs") or "")[:80],"high","YallaMotor — جديد"))
                if out: break
        except Exception as e: print(f"YallaMotor: {e}")
    return out

async def httpx_autotrader(query, client):
    out = []
    for url in [f"https://autotrader.sa/api/v1/cars?search={_qenc(query)}&limit=12",
                f"https://autotrader.sa/api/listings?q={_qenc(query)}"]:
        try:
            r = await client.get(url, timeout=SCRAPE_TIMEOUT)
            if r.status_code==200 and "json" in r.headers.get("content-type",""):
                cars = r.json().get("data") or r.json().get("listings") or r.json().get("cars") or []
                for c in cars[:10]:
                    p = _int(c.get("price") or c.get("asking_price"))
                    if p > 20000:
                        out.append(_L("autotrader","AutoTrader.sa",c.get("title") or c.get("name") or query,
                            c.get("condition") or "مستعملة",p,
                            f"{c.get('mileage',0):,} كم" if c.get("mileage") else "غير محدد",
                            c.get("city") or "غير محدد",c.get("seller_name") or "AutoTrader",
                            c.get("seller_type") or "dealer",0,
                            c.get("image") or _img(query),c.get("url") or "https://autotrader.sa",
                            (c.get("description") or "")[:80],"medium","AutoTrader SA"))
                if out: break
        except Exception as e: print(f"AutoTrader: {e}")
    return out

async def httpx_opensooq(query, client):
    out = []
    for url in [
        f"https://sa.opensooq.com/api/v2/posts/search?q={_qenc(query)}&category_id=84&limit=20",
        f"https://api.opensooq.com/v3/search?query={_qenc(query)}&country=sa&category=cars&limit=20",
        f"https://sa.opensooq.com/api/v1/listings?keywords={_qenc(query)}&cat=cars&limit=20",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json","Accept-Language":"ar"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                items = data.get("data") or data.get("posts") or data.get("listings") or data.get("results") or []
                for item in items[:12]:
                    price_raw = item.get("price") or item.get("asking_price") or item.get("cost") or 0
                    price = _int(price_raw)
                    if price > 20000:
                        out.append(_L(
                            "opensooq", "OpenSooq.com",
                            item.get("title") or item.get("subject") or query,
                            "مستعملة" if item.get("is_used") else item.get("condition","مستعملة"),
                            price,
                            f"{item.get('mileage',0):,} كم" if item.get("mileage") else "غير محدد",
                            item.get("city") or item.get("location") or item.get("area") or "غير محدد",
                            item.get("username") or item.get("seller_name") or "بائع OpenSooq",
                            "individual" if item.get("is_private") else "dealer",
                            0,
                            (item.get("images") or [{"url": _img(query)}])[0].get("url") if isinstance(item.get("images",[{}])[0], dict) else _img(query),
                            item.get("url") or f"https://sa.opensooq.com",
                            (item.get("description") or item.get("body") or "")[:80],
                            "medium", "OpenSooq — سوق خليجي"
                        ))
                if out: break
        except Exception as e:
            logger.error("OpenSooq %s: %s", url, e)
    return out

async def httpx_carswitch(query, client):
    out = []
    parts = query.split()
    year  = parts[0] if parts and parts[0].isdigit() else ""
    brand = parts[1] if len(parts) > 1 else ""
    model = " ".join(parts[2:]) if len(parts) > 2 else ""
    for url in [
        f"https://carswitch.com/api/v2/cars?make={brand}&model={model}&year={year}&country=sa&limit=15",
        f"https://carswitch.com/api/v1/inventory?q={_qenc(query)}&country=sa&limit=15",
        f"https://api.carswitch.com/cars?search={_qenc(query)}&country=sa",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json","Origin":"https://carswitch.com"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                cars = data.get("data") or data.get("cars") or data.get("inventory") or data.get("results") or []
                for c in cars[:10]:
                    price = _int(c.get("price") or c.get("selling_price") or c.get("sale_price"))
                    if price > 20000:
                        out.append(_L(
                            "carswitch", "CarSwitch.com",
                            c.get("title") or c.get("name") or f"{c.get('year','')} {c.get('make','')} {c.get('model','')}".strip() or query,
                            "مستعملة معتمدة",
                            price,
                            f"{c.get('mileage',0):,} كم" if c.get("mileage") else "غير محدد",
                            c.get("city") or c.get("location") or "المملكة العربية السعودية",
                            "CarSwitch Certified",
                            "dealer",
                            0,
                            c.get("image") or c.get("main_image") or _img(query),
                            c.get("url") or "https://carswitch.com",
                            f"مفحوصة {c.get('inspection_score','')} نقطة" if c.get("inspection_score") else "سيارة معتمدة",
                            "high", "CarSwitch — مستعملة معتمدة"
                        ))
                if out: break
        except Exception as e:
            logger.error("CarSwitch %s: %s", url, e)
    return out

async def httpx_dubizzle(query, client):
    out = []
    for url in [
        f"https://api.dubizzle.com/en/classifieds/motors/cars/?keywords={_qenc(query)}&country_abbr=sa&format=json&page_size=15",
        f"https://dubai.dubizzle.com/api/v1/motors/cars/?q={_qenc(query)}&country=sa&format=json&limit=15",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json","Referer":"https://dubai.dubizzle.com/"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                items = data.get("results") or data.get("data") or data.get("listings") or []
                for item in items[:10]:
                    price = _int(item.get("price") or item.get("asking_price"))
                    if price > 20000:
                        out.append(_L(
                            "dubizzle_sa", "Dubizzle Saudi",
                            item.get("title") or item.get("subject") or query,
                            "مستعملة",
                            price,
                            f"{item.get('mileage',0):,} كم" if item.get("mileage") else "غير محدد",
                            item.get("city") or item.get("location") or "غير محدد",
                            item.get("username") or "بائع Dubizzle",
                            "individual" if item.get("is_private") else "dealer",
                            0,
                            (item.get("photos") or [{"value": _img(query)}])[0].get("value","") if item.get("photos") else _img(query),
                            item.get("absolute_url") or "https://dubai.dubizzle.com",
                            (item.get("description") or "")[:80],
                            "medium", "Dubizzle Saudi"
                        ))
                if out: break
        except Exception as e:
            logger.error("Dubizzle %s: %s", url, e)
    return out

async def httpx_car_sa(query, client):
    out = []
    for url in [
        f"https://www.car.com.sa/api/cars?search={_qenc(query)}&limit=15",
        f"https://www.car.com.sa/api/v1/listings?q={_qenc(query)}&limit=15",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                cars = data.get("data") or data.get("cars") or data.get("results") or []
                for c in cars[:10]:
                    price = _int(c.get("price") or c.get("selling_price"))
                    if price > 20000:
                        out.append(_L(
                            "car_sa", "Car.com.sa",
                            c.get("title") or c.get("name") or query,
                            c.get("condition") or "مستعملة",
                            price,
                            f"{c.get('mileage',0):,} كم" if c.get("mileage") else "غير محدد",
                            c.get("city") or "غير محدد",
                            c.get("seller_name") or "Car.com.sa",
                            c.get("seller_type") or "dealer",
                            0, _img(query),
                            c.get("url") or "https://www.car.com.sa", "",
                            "medium", "Car.com.sa"
                        ))
                if out: break
        except Exception as e:
            logger.error("Car.com.sa %s: %s", url, e)
    return out

async def httpx_petromin(query, client):
    out = []
    parts = query.split()
    brand = parts[1] if len(parts) > 1 else ""
    model_q = " ".join(parts[2:]) if len(parts) > 2 else ""
    for url in [
        f"https://www.petrominexpress.com/api/cars?make={brand}&model={model_q}&limit=12",
        f"https://www.petrominexpress.com/api/v1/new-cars?q={_qenc(query)}&limit=12",
        f"https://petrominexpress.com/api/inventory?search={_qenc(query)}",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json","Referer":"https://www.petrominexpress.com/"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                cars = data.get("data") or data.get("cars") or data.get("inventory") or data.get("results") or []
                for c in cars[:10]:
                    price = _int(c.get("price") or c.get("selling_price") or c.get("sale_price"))
                    if price > 20000:
                        out.append(_L(
                            "petromin", "Petromin Express",
                            c.get("title") or c.get("name") or f"{c.get('year','')} {brand} {model_q}".strip() or query,
                            "مستعملة معتمدة",
                            price,
                            f"{c.get('mileage',0):,} كم" if c.get("mileage") else "غير محدد",
                            c.get("branch") or c.get("city") or "المملكة العربية السعودية",
                            "Petromin Express",
                            "official_dealer",
                            0,
                            c.get("image") or c.get("thumbnail") or _img(query),
                            c.get("url") or "https://www.petrominexpress.com",
                            f"مفحوصة {c.get('inspection_points','')} نقطة" if c.get("inspection_points") else "سيارة معتمدة من بترومين",
                            "high", "Petromin Express — وكيل معتمد"
                        ))
                if out: break
        except Exception as e:
            logger.error("Petromin %s: %s", url, e)
    return out

async def httpx_aljazirah(query, client):
    out = []
    for url in [
        f"https://www.aljazirah-motors.com.sa/api/vehicles?search={_qenc(query)}&limit=10",
        f"https://www.aljazirah-motors.com.sa/api/v1/new-cars?q={_qenc(query)}",
    ]:
        try:
            r = await client.get(url, headers={"Accept":"application/json"}, timeout=SCRAPE_TIMEOUT)
            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                cars = data.get("data") or data.get("vehicles") or data.get("cars") or []
                for c in cars[:8]:
                    price = _int(c.get("price") or c.get("base_price") or c.get("msrp"))
                    if price > 20000:
                        out.append(_L(
                            "aljazirah", "Al-Jazirah Motors",
                            c.get("name") or c.get("title") or query,
                            "جديدة", price, "0 كم",
                            "المملكة العربية السعودية",
                            "الجزيرة للسيارات", "official_dealer",
                            0, c.get("image") or _img(query),
                            c.get("url") or "https://www.aljazirah-motors.com.sa",
                            "سعر رسمي من الجزيرة للسيارات",
                            "high", "Al-Jazirah — وكيل رسمي"
                        ))
                if out: break
        except Exception as e:
            logger.error("Al-Jazirah %s: %s", url, e)
    return out

async def httpx_generic(source, query, client):
    out = []
    base = source.get("url","")
    sid = source.get("id","custom")
    sname = source.get("name","Custom")
    search_url = source.get("search_url","").replace("{query}",_qenc(query))
    for url in ([search_url] if search_url else []) + [f"{base}/api/cars?q={_qenc(query)}"]:
        try:
            r = await client.get(url, timeout=SCRAPE_TIMEOUT)
            if r.status_code==200 and "json" in r.headers.get("content-type",""):
                items = r.json().get("data") or r.json().get("results") or r.json().get("cars") or []
                for item in items[:8]:
                    p = _int(item.get("price") or item.get("selling_price"))
                    if p > 20000:
                        out.append(_L(sid,sname,item.get("title") or item.get("name") or query,
                            item.get("condition") or "مستعملة",p,str(item.get("mileage") or "غير محدد"),
                            item.get("city") or "غير محدد",item.get("seller") or sname,"dealer",0,
                            item.get("image") or _img(query),item.get("url") or base,
                            (item.get("description") or "")[:80],"medium",f"بحث من {sname}"))
                if out: break
        except Exception as e:
            logger.warning("httpx_generic scrape error for %s: %s", url, e)
            continue
    return out

# ── Trim normalizer ─────────────────────────────────────────────
def normalize_trim(title: str, brand: str, model: str, year: int) -> tuple[str, str, float]:
    configured = get_trims(brand, model, year)
    if configured:
        return normalize_from_config(title, brand, model, year)
    return f"{model} Standard", "مطابقة عامة", 0.50

def normalize_trim_dynamic(title: str, brand: str, model: str, year: int,
                             fetched_trims: list) -> tuple[str, str, float]:
    if not fetched_trims:
        return normalize_trim(title, brand, model, year)
    t = title.lower()
    best_match = None
    best_score = 0.0
    for trim in fetched_trims:
        score = 0.0
        for kw in trim.keywords:
            if kw and kw in t:
                score = max(score, trim.score)
                break
        name_words = trim.name.lower().split()
        matches = sum(1 for w in name_words if len(w) > 2 and w in t)
        if matches > 0:
            score = max(score, 0.65 + (matches / len(name_words)) * 0.25)
        if score > best_score:
            best_score = score
            best_match = trim
    if best_match and best_score > 0.5:
        return best_match.name, f"مطابقة ديناميكية ({best_match.keywords[0] if best_match.keywords else ''})", best_score
    return normalize_trim(title, brand, model, year)


# ── Trim fetching ──────────────────────────────────────────────
async def fetch_official_trims(brand: str, model: str, year: int,
                                anthropic_key: str) -> list[TrimInfo]:
    """
    Fetch official trims with priority:
      1. Auto-trims from HJ API (always fresh from catalog)
      2. trims_config.py (fallback if API is down)
      3. In-memory cache
      4. Playwright scrape / AI fallback
    """
    key = (brand.lower(), model.lower(), year)

    # ── Priority 1: Auto-trims from HJ catalog API ──
    try:
        auto_trims = await fetch_trims_from_catalog(brand, model, year, anthropic_key)
        if auto_trims:
            trims = [
                TrimInfo(
                    name=t["name"].lower(),
                    name_ar=t["name_ar"],
                    msrp=t.get("msrp", 0),
                    engine=t.get("engine", ""),
                    keywords=t.get("keywords", []),
                    score=t.get("match_score", 0.88),
                )
                for t in auto_trims
            ]
            _trim_cache[key] = {"trims": trims, "ts": time.time()}
            logger.info("Auto-trims: loaded %d trims for %s %s %d from HJ API",
                        len(trims), brand, model, year)
            return trims
    except Exception as e:
        logger.warning("Auto-trim fetch failed for %s %s %d: %s", brand, model, year, e)

    # ── Priority 2: trims_config.py (manual fallback) ──
    configured = get_trims(brand, model, year)
    if configured:
        return [
            TrimInfo(name=t.name.lower(), name_ar=t.name_ar, msrp=t.msrp, engine=t.engine,
                     keywords=t.keywords, score=t.match_score)
            for t in configured
        ]

    # ── Priority 3: In-memory cache ──
    if key in _trim_cache:
        entry = _trim_cache[key]
        if time.time() - entry["ts"] < TRIM_CACHE_TTL:
            return entry["trims"]

    # ── Priority 4: Playwright scrape / AI fallback ──
    trims = []
    if brand.lower() in ("toyota", "lexus") and PLAYWRIGHT_OK:
        trims = await _scrape_trims_toyota_sa(brand, model, year)
    if not trims:
        trims = await _ai_fetch_trims(brand, model, year, anthropic_key)
    if trims:
        _trim_cache[key] = {"trims": trims, "ts": time.time()}
    return trims

async def _scrape_trims_toyota_sa(brand: str, model: str, year: int) -> list[TrimInfo]:
    return await pw_run(_scrape_trims_toyota_sa_impl, brand, model, year)

async def _scrape_trims_toyota_sa_impl(brand: str, model: str, year: int) -> list[TrimInfo]:
    trims = []
    if not PLAYWRIGHT_OK:
        return trims
    slug = model.lower().replace(" ", "-")
    urls = [
        f"https://www.toyota.com.sa/en/models/{slug}",
        f"https://www.toyota.com.sa/ar/models/{slug}",
        f"https://www.toyota.com.sa/en/vehicles/{slug}",
    ]
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"]
            )
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
            )
            await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,mp4}", lambda r: r.abort())
            for url in urls:
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    if not resp or resp.status >= 400:
                        continue
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    import json as _json, re as _re
                    for ld_str in _re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, _re.DOTALL):
                        try:
                            ld = _json.loads(ld_str)
                            offers = ld.get("offers") or []
                            if isinstance(offers, dict): offers = [offers]
                            for o in offers:
                                price = _int(str(o.get("price") or o.get("lowPrice") or "0"))
                                if 40000 < price < 2000000:
                                    name = o.get("name","") or f"{model} Trim"
                                    trims.append(TrimInfo(
                                        name=name, name_ar=name,
                                        msrp=price, engine="",
                                        keywords=[name.lower(), name.split()[-1].lower()],
                                        score=0.90
                                    ))
                        except Exception as e:
                            logger.warning("JSON-LD trim parse error: %s", e)
                    if not trims:
                        pairs = _re.findall(
                            r'([A-Z][A-Z0-9 \+\-]{1,25}?)\s*(?:﷼|SAR|السعر)?\s*([0-9,]{6,})',
                            html, _re.IGNORECASE
                        )
                        seen_prices = set()
                        # FIX: removed `[:12]` cap so all detected trims are captured.
                        for trim_name, price_str in pairs:
                            price = _int(price_str)
                            if 40000 < price < 2000000 and price not in seen_prices:
                                seen_prices.add(price)
                                clean_name = trim_name.strip()
                                trims.append(TrimInfo(
                                    name=f"{model} {clean_name}",
                                    name_ar=f"{model} {clean_name}",
                                    msrp=price, engine="",
                                    keywords=[clean_name.lower()],
                                    score=0.85
                                ))
                    if trims:
                        break
                except Exception as e:
                    continue
            await browser.close()
    except Exception as e:
        logger.error("Playwright trim scrape error: %s", e)
    return trims

async def _ai_fetch_trims(brand: str, model: str, year: int,
                           anthropic_key: str) -> list[TrimInfo]:
    if not anthropic_key:
        return []
    prompt = f"""List ALL official trim levels for {year} {brand} {model} sold in Saudi Arabia.
For each trim include: official name, Arabic name, MSRP in SAR (including 15% VAT), engine, and 3-5 common informal names used in Saudi listings.
Return ONLY valid JSON array, no markdown:
[
  {{
    "name": "official trim name in English",
    "name_ar": "اسم الفئة بالعربي",
    "msrp": 120000,
    "engine": "2.5L Hybrid 226hp",
    "keywords": ["informal1", "keyword2", "arabic keyword"],
    "score": 0.90
  }}
]
RULES:
- Saudi Arabia prices only (SAR, includes VAT 15%)
- All trims available at authorized dealers RIGHT NOW ({year} model year)
- keywords = actual words buyers use in listings on Haraj/Syarah
- If model unknown, return empty array []"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_API_URL,
                headers={"Content-Type":"application/json","x-api-key":anthropic_key,"anthropic-version":"2023-06-01"},
                json={"model":"claude-sonnet-4-6","max_tokens":1500,
                      "messages":[{"role":"user","content":prompt}]}
            )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"]
        data = json.loads(re.sub(r"```json|```","",raw).strip())
        trims = []
        for item in data:
            trims.append(TrimInfo(
                name=item.get("name",""),
                name_ar=item.get("name_ar",""),
                msrp=_int(str(item.get("msrp",0))),
                engine=item.get("engine",""),
                keywords=item.get("keywords",[]),
                score=float(item.get("score",0.85))
            ))
        return trims
    except Exception as e:
        return []


async def get_model_trims_from_catalog(brand: str, model: str, year: int) -> list[str]:
    cached = await cache_get("car_catalog")
    if not cached:
        return []
    model_types = cached.get("modelTypes", [])
    groups      = cached.get("groups", [])
    brands_list = cached.get("brands", [])
    brand_id = None
    for b in brands_list:
        desc = (b.get("DescriptionEn") or b.get("DescriptionAr") or "").lower()
        if brand.lower() in desc:
            brand_id = b.get("BrandID")
            break
    if not brand_id:
        return []
    group_id = None
    for g in groups:
        if g.get("brandID") != brand_id:
            continue
        g_year = str(g.get("Year", ""))
        g_desc = (g.get("DescriptionEn") or g.get("DescriptionAr") or "").lower()
        if model.lower() in g_desc and str(year) in g_year:
            group_id = g.get("productGroupID")
            break
    if not group_id:
        for g in groups:
            if g.get("brandID") != brand_id:
                continue
            g_desc = (g.get("DescriptionEn") or g.get("DescriptionAr") or "").lower()
            if model.lower() in g_desc:
                group_id = g.get("productGroupID")
                break
    if not group_id:
        return []
    trim_set = set()
    for mt in model_types:
        mt_model = str(mt.get("Model", ""))
        if mt.get("productGroupID") == group_id and str(year) in mt_model:
            name = mt.get("descriptionEn") or mt.get("descriptionAr") or mt.get("description") or ""
            if not name:
                continue
            clean_name = name.lower()
            pattern = r'\b(' + re.escape(brand.lower()) + r'|' + re.escape(model.lower()) + r'|' + str(year) + r')\b'
            clean_name = re.sub(pattern, '', clean_name)
            clean_name = " ".join(clean_name.split())
            if clean_name:
                trim_set.add(clean_name)
    # FIX: was `return list(unique_list)[:5]` — the [:5] cap silently dropped
    # trims for any model with more than 5 grades (Camry has 7, Land Cruiser
    # 12+). Returning the full set is the bug fix.
    return list(trim_set)


# ── AI fallback ─────────────────────────────────────────────────
async def ai_fallback(brand, model, year, source_ids, key):
    """
    يسأل Claude عن معرفته بأسعار السيارة في السوق السعودي.
    الـ AI لا يتصفح مواقع — يعطي بيانات بناءً على معرفته السوقية.
    """
    catalog_trims = await get_model_trims_from_catalog(brand, model, year)
    if catalog_trims:
        trims_section = json.dumps(catalog_trims, ensure_ascii=False)
    else:
        trims_section = f"جميع الفئات الرسمية لـ {year} {brand} {model} في السعودية"

    logger.info("AI fallback for %s %s %s, trims: %s", year, brand, model, trims_section[:100])

    prompt = f"""أنت خبير في سوق السيارات السعودي. أحتاج معلومات عن أسعار {year} {brand} {model} في المملكة العربية السعودية.

الفئات الرسمية: {trims_section}

لكل فئة، أعطني:
- السعر الرسمي MSRP (شامل ضريبة 15%)
- نطاق الأسعار في السوق (أدنى / متوسط / أعلى)
- حالة العرض والطلب
- المحرك والمواصفات الأساسية
- الأسماء الشائعة التي يستخدمها البائعون في حراج وسيارة

أرجع JSON فقط بدون أي نص إضافي:
{{
  "vehicle": "{year} {brand} {model}",
  "brand": "{brand}",
  "model": "{model}",
  "year": {year},
  "searchDate": "{datetime.now().strftime('%B %Y')}",
  "isAIFallback": true,
  "officialPriceRange": {{"min": 0, "max": 0}},
  "marketInsight": "ملخص عربي عن وضع السيارة في السوق السعودي",
  "trims": [
    {{
      "officialName": "اسم الفئة بالإنجليزي",
      "officialNameAr": "اسم الفئة بالعربي",
      "officialMSRP": 120000,
      "engine": "2.5L Hybrid 226hp",
      "commonAliases": ["الاسم الشائع 1", "alias2"],
      "listings": [
        {{
          "source": "ai-estimate",
          "sourceName": "تقدير AI",
          "listedAs": "عنوان الإعلان بالعربي",
          "priceType": "estimated",
          "matchConfidence": "medium",
          "condition": "جديدة",
          "price": 120000,
          "mileage": "0 كم",
          "location": "السعودية",
          "priceNote": "تقدير بناءً على معرفة السوق — وليس إعلان فعلي",
          "sellerType": "dealer",
          "sellerName": "",
          "postedDaysAgo": 0,
          "imageUrl": ""
        }}
      ],
      "priceAnalysis": {{
        "marketMin": 110000,
        "marketMax": 135000,
        "marketAvg": 120000,
        "vsOfficialPct": 0,
        "trend": "stable"
      }}
    }}
  ],
  "competitorAnalysis": {{
    "summary": "ملخص تحليل المنافسة بالعربي",
    "opportunities": ["فرصة 1", "فرصة 2"],
    "threats": ["تهديد 1"],
    "recommendation": "التوصية بالعربي"
  }}
}}

قواعد:
- كل الأسعار بالريال السعودي شاملة ضريبة 15%
- لو ما تعرف سعر فئة معينة، قدّر بناءً على معرفتك (لا ترجع فاضي)
- priceType دائماً "estimated" (هذه تقديرات وليست إعلانات فعلية)
- الـ priceNote يوضح إن هذا تقدير AI
- أرجع JSON فقط — بدون markdown أو نص إضافي"""

    await cache_set('ai_fallback_prompt', {"content": prompt})

    try:
        async with httpx.AsyncClient(timeout=60.0, http2=False) as client:
            r = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Connection": "close"
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 8000,
                    "temperature": 0.3,
                    "system": (
                        "أنت خبير سوق سيارات سعودي. تعرف أسعار السيارات الجديدة والمستعملة "
                        "في المملكة العربية السعودية، أسعار الوكلاء الرسميين، واتجاهات السوق. "
                        "أرجع JSON فقط بدون أي نص أو تعليقات خارج الـ JSON."
                    ),
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
            )
        r.raise_for_status()
    except Exception as e:
        logger.error("AI fallback HTTP error: %s", e)
        raise

    try:
        data = r.json()
    except Exception as e:
        logger.error("AI fallback JSON parse error: %s | raw: %s", e, r.text[:200])
        raise

    text = ""
    if "content" in data and len(data["content"]) > 0:
        text = data["content"][0].get("text", "")
    else:
        raise ValueError("Invalid AI response format")

    # استخراج JSON من الرد
    clean_text = text.strip()
    brace_match = re.search(r"\{.*\}", clean_text, re.DOTALL)
    if brace_match:
        clean_text = brace_match.group(0)
    else:
        clean_text = re.sub(r"```json|```", "", clean_text).strip()

    try:
        result = json.loads(clean_text)
        logger.info("AI fallback: %d trims returned", len(result.get("trims", [])))
        return result
    except Exception as e:
        logger.error("AI fallback JSON parse failed: %s | text: %s", e, clean_text[:300])
        raise


# ── Source stats / enrichment helpers ──────────────────────────
def _source_color(source_id: str) -> str:
    return {
        "toyota.com.sa":  "#00C49A",
        "ksa.Motory.com":     "#f5a623",
        "haraj.com.sa":      "#ff8055",
        "ksa.yallamotor.com": "#a78bfa",
        "syarah.com":     "#4da6ff",
    }.get(source_id, "#6B7280")


def _attach_source_stats(data: dict):
    """
    For each trim: group listings by source, compute min/avg/max/count/spread.
    """
    brand = data.get("brand", "")
    model = data.get("model", "")

    _static_ranges = {
        "yaris":         (48000,  95000),
        "yaris cross":   (70000,  150000),
        "urban cruiser":  (70000,  150000),
        "raize":         (58000,  120000),
        "veloz":         (72000,  145000),
        "rush":          (78000,  155000),
        "corolla":       (72000,  160000),
        "camry":         (95000,  175000),
        "rav4":          (90000,  200000),
        "fortuner":      (105000, 260000),
        "hilux":         (82000,  215000),
        "prado":         (145000, 340000),
        "land cruiser":  (250000, 620000),
        "patrol":        (145000, 500000),
    }

    def _in_range(price: int, msrp: int = 0) -> bool:
        if price <= 0:
            return False
        if msrp and msrp > 0:
            return int(msrp * 0.85) <= price <= int(msrp * 1.20)
        model_l = (model or "").lower()
        for key, (lo, hi) in _static_ranges.items():
            if key in model_l:
                return lo <= price <= hi
        return 25000 <= price <= 700000

    for trim in data.get("trims", []):
        listings = trim.get("listings", [])
        trim_msrp = trim.get("officialMSRP", 0) or 0

        cleaned = []
        for l in listings:
            price = l.get("price", 0)
            condition = str(l.get("condition", "")).strip()
            src_id = l.get("source", "")
            # فقط لو المصدر ما رجع حالة أصلاً
            if not condition or condition == "غير محدد":
                # المواقع الرسمية = جديدة دائماً (ما يبيعون مستعمل)
                if src_id in ("toyota.com.sa", "lexus.com.sa"):
                    l["condition"] = "جديدة"
                # حراج = غالباً مستعمل
                elif src_id == "haraj.com.sa":
                    l["condition"] = "مستعملة"
                # باقي المصادر (سيارة، موتوري، يلاموتور) = نحاول نكتشف من النص
                else:
                    listing_text = str(l.get("listedAs", "") + " " + l.get("priceNote", "") + " " + l.get("matchReason", "")).lower()
                    mileage = str(l.get("mileage", "")).lower()
                    used_kw = ["مستعمل", "مستعملة", "used", "pre-owned"]
                    new_kw = ["جديد", "جديدة", "new", "زيرو"]
                    # لو المسافة أكثر من 0 كم = مستعملة
                    has_mileage = mileage and mileage != "0 كم" and mileage != "غير محدد" and mileage != "0"
                    if any(k in listing_text for k in used_kw) or has_mileage:
                        l["condition"] = "مستعملة"
                    elif any(k in listing_text for k in new_kw):
                        l["condition"] = "جديدة"
                    else:
                        l["condition"] = "غير محدد"
            if not l.get("vat_status"):
                try:
                    from scraper_playwright import infer_vat_status
                    listing_text = " ".join(filter(None, [
                        l.get("listedAs",""),
                        l.get("priceNote",""),
                        l.get("note",""),
                    ]))
                    vat = infer_vat_status(price, trim_msrp, src_id,
                                           listing_text=listing_text)
                    l["vat_status"]   = vat["vat_status"]
                    l["vat_confidence"] = vat["confidence"]
                    l["price_ex_vat"] = vat["price_ex_vat"]
                    l["price_inc_vat"]= vat["price_inc_vat"]
                    l["priceNote"]    = l.get("priceNote") or vat["reason"]
                except Exception as e:
                    if src_id == "haraj.com.sa":
                        l["vat_status"] = "unknown"
                        l["priceNote"]  = l.get("priceNote") or "قد لا يشمل VAT"
                    else:
                        l["vat_status"] = "included"
                        l["priceNote"]  = l.get("priceNote") or "شامل VAT 15%"
            cleaned.append(l)

        trim["listings"] = cleaned
        listings = cleaned

        by_source: dict = {}
        for l in listings:
            price = l.get("price", 0)
            if not price:
                continue
            src_name = l.get("source") or "غير محدد"
            if src_name not in by_source:
                by_source[src_name] = {
                    "sourceId":   l.get("source", ""),
                    "sourceName": src_name,
                    "color":      _source_color(l.get("source", "")),
                    "prices":     [],
                    "listings":   [],
                }
            by_source[src_name]["prices"].append(price)
            by_source[src_name]["listings"].append(l)

        source_stats = []
        for src_name, info in by_source.items():
            prices = sorted(info["prices"])
            n = len(prices)
            avg = int(sum(prices) / n) if n else 0
            source_stats.append({
                "sourceId":   info["sourceId"],
                "sourceName": src_name,
                "color":      info["color"],
                "count":      n,
                "min":        prices[0]  if prices else 0,
                "avg":        avg,
                "max":        prices[-1] if prices else 0,
                "spread":     prices[-1] - prices[0] if len(prices) > 1 else 0,
                "listings":   info["listings"],
            })

        source_stats.sort(key=lambda x: x["min"] if x["min"] else 999999999)
        trim["sourceStats"] = source_stats

        all_prices = [l.get("price", 0) for l in listings if l.get("price")]
        if all_prices:
            msrp = trim.get("officialMSRP", 0)
            market_avg = int(sum(all_prices) / len(all_prices))
            vs_pct = round((market_avg - msrp) / msrp * 100, 1) if msrp else 0
            trim["priceAnalysis"].update({
                "marketMin":    min(all_prices),
                "marketMax":    max(all_prices),
                "marketAvg":    market_avg,
                "vsOfficialPct": vs_pct,
                "listingCount": len(all_prices),
            })

        total_new  = sum(1 for l in listings if "جديد" in str(l.get("condition","")).lower() or l.get("condition","") in ("New","جديدة"))
        total_used = len(listings) - total_new
        trim["supplyStats"] = {
            "totalListings": len(listings),
            "newListings":   total_new,
            "usedListings":  total_used,
            "newPct":        round(total_new / len(listings) * 100) if listings else 0,
        }


async def _post_search_persist(data: dict, brand: str, model: str, year: int):
    for trim in data.get("trims", []):
        trim_name = trim.get("officialName", "")
        pa = trim.get("priceAnalysis", {})
        if pa.get("marketMin") and pa.get("marketAvg"):
            await snapshot_save(brand, model, year, trim_name,
                                 pa["marketMin"], pa["marketAvg"],
                                 pa.get("marketMax", 0), pa.get("listingCount", 0))
        for l in trim.get("listings", []):
            url = l.get("url", "")
            src = l.get("source", "")
            if url and src:
                await dom_record(src, url, trim_name)


async def _enrich_with_history(data: dict, brand: str, model: str, year: int):
    for trim in data.get("trims", []):
        trim_name = trim.get("officialName", "")
        history = await snapshot_get_history(brand, model, year, trim_name, weeks=6)
        trend   = await snapshot_calc_trend(history)
        trim["priceTrend"] = {**trend, "history": history}
        dom = await dom_avg_for_trim(brand, model, trim_name)
        trim["daysOnMarket"] = dom


# ── Car Catalog Functions ───────────────────────────────────────
def transform_catalog(data):
    return {
        "brands": [
            {
                "BrandID": b.get("BrandID") or b.get("brandID") or "",
                "DescriptionAr": b.get("Description") or "",
                "DescriptionEn": b.get("Description") or "",
                "Description": b.get("Description") or ""
            }
            for b in data.get("brands", [])
        ],
        "groups": [
            {
                "ListTreeGroups": g.get("ListTreeGroups") or "",
                "brandID": g.get("BrandId") or "",
                "Year": g.get("Year") or "",
                "DescriptionAr": g.get("DescriptionAr") or "",
                "DescriptionEn": g.get("DescriptionEn") or "",
                "Description": g.get("Description") or g.get("DescriptionEn") or g.get("DescriptionAr") or "",
                "productGroupID": g.get("ProductGroupId") or g.get("productGroupID") or "",
            }
            for g in data.get("groups", [])
        ],
        "modelTypes": [
            {
                "ModelCode": m.get("ModelCode") or "",
                "guid": m.get("guid") or "",
                "ProductTypeId": m.get("ProductTypeId") or "",
                "Model": m.get("Model") or "",
                "descriptionAr": m.get("descriptionAr") or "",
                "descriptionEn": m.get("ShortDescriptionEn") or m.get("DescriptionEn") or "",
                "Description": m.get("Description") or m.get("descriptionEn") or m.get("descriptionAr") or"",
                "productGroupID": m.get("ProductGroupId") or m.get("productGroupID") or "",
                "Image": m.get("Image") or None
            }
            for m in data.get("modelTypes", [])
        ]
    }

async def fetch_car_catalog() -> CarCatalog:
    url = "https://appw.hassanjameelapp.com/en/api/finance/index"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        fixed_data = transform_catalog(data)
        return CarCatalog(**fixed_data)
