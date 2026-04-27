"""
Per-trim, per-source price aggregator.

Sources:
  - toyota.com.sa     (httpx, official MSRPs — server-rendered HTML)
  - haraj.com.sa      (Playwright, marketplace — React Router SPA)
  - syarah.com        (Playwright, dealer inventory — Next.js)
  - ksa.Motory.com    (Playwright, new cars — Angular)
  - ksa.yallamotor.com (Playwright, new cars — Next.js)

For each Toyota trim, we filter every marketplace's listings whose title
contains the trim name (case-insensitive, with HEV/Hybrid normalization),
then compute the average ask price per source.

Output (simplified — only avg + count, no min/max):
{
    "trim": "LE",
    "sources": {
        "toyota.com.sa":      {"avg": 121555, "count": 1, "type": "official"},
        "haraj.com.sa":       {"avg": 111600, "count": 5, "type": "marketplace"},
        "syarah.com":         {"avg": 118000, "count": 3, "type": "marketplace"},
        "ksa.Motory.com":     {"avg": 119500, "count": 2, "type": "new_cars"},
        "ksa.yallamotor.com": {"avg": 117800, "count": 1, "type": "new_cars"},
    },
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

logger = logging.getLogger("price_aggregator")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MODEL_AR = {
    # Toyota
    "camry": "كامري",
    "corolla": "كورولا",
    "corolla cross": "كورولا كروس",
    "fortuner": "فورتشنر",
    "land cruiser": "لاندكروزر",
    "landcruiser wagon": "لاندكروزر",
    "hilux": "هايلكس",
    "hilux double cab": "هايلكس",
    "rav4": "راف فور",
    "rav 4": "راف فور",
    "yaris": "يارس",
    "prado": "برادو",
    "highlander": "هايلاندر",
    "innova": "اينوفا",
    "raize": "رايز",
    "veloz": "فيلوز",
    "urban cruiser": "اربن كروزر",
    "crown": "كراون",
    "supra": "سوبرا",
    "gr86": "جي ار 86",
    # Lexus
    "rx": "آر اكس",
    "lx": "ال اكس",
    "es": "اي اس",
    "nx": "ان اكس",
    "is": "آي اس",
    "ls": "ال اس",
    "ux": "يو اكس",
    "lc": "ال سي",
    "gx": "جي اكس",
}


# ─────────────────────────────────────────────────────────────────
#  Toyota.com.sa — official MSRPs (httpx)
# ─────────────────────────────────────────────────────────────────

TOYOTA_URL_PREFIXES = (
    "https://www.toyota.com.sa/en/vehicles/passenger/{slug}",
    "https://www.toyota.com.sa/en/vehicles/suv/{slug}",
    "https://www.toyota.com.sa/en/vehicles/commercial/{slug}",
)

TOYOTA_SLUGS = {
    "land cruiser": "lc300",
    "landcruiser wagon": "lc300",
    "rav 4": "rav4",
    "hilux double cab": "hiluxdc",
    "hilux single cab": "hiluxsc",
    "corolla cross": "corollacross",
    "urban cruiser": "urbancruiser",
    "gr86": "gr86",
    "gr 86": "gr86",
}


def _toyota_slug(model: str) -> str:
    m = model.lower().strip()
    return TOYOTA_SLUGS.get(m, m.replace(" ", "-"))


async def scrape_toyota_official(model: str, year: int = 0) -> list[dict]:
    slug = _toyota_slug(model)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        html = None
        used_url = None
        for tmpl in TOYOTA_URL_PREFIXES:
            url = tmpl.format(slug=slug)
            try:
                r = await client.get(url, headers=headers)
                # Detect a real model page by the presence of the GTM grade
                # attribute itself — more robust than text-matching the heading,
                # which on some models (e.g. GR86) is split across HTML elements
                # so "AVAILABLE GRADES" never appears as a literal substring.
                if r.status_code == 200 and "data-gtm-property-grade" in r.text:
                    html = r.text
                    used_url = url
                    break
            except Exception as e:
                logger.warning("Toyota fetch %s: %s", url, e)
        if not html:
            return []

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for el in soup.select("[data-gtm-property-grade]"):
        grade = (el.get("data-gtm-property-grade") or "").strip()
        if not grade:
            continue
        price_raw = el.get("data-gtm-property-price") or ""
        m = re.search(r"(\d{1,3}(?:,\d{3})+|\d{5,7})", price_raw)
        if not m:
            continue
        price = int(re.sub(r"[^\d]", "", m.group(1)))
        if price < 30_000 or price > 2_000_000:
            continue
        key = (grade.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        out.append({"trim": grade, "price": price, "url": used_url})
    return out


# ─────────────────────────────────────────────────────────────────
#  Lexus.com.sa — official MSRPs (httpx)
# ─────────────────────────────────────────────────────────────────
# Lexus's site uses a Sitecore "accordion-box" component for the
# spec/pricing section. Each model has one box with
# `data-gtm-property-section="specs"` and `data-gtm-property-title="performance"`;
# the first <li> in that box contains `.right-text > h5` elements like:
#     "RX 350 BB Excellence  315,330"
# (trim name + double space + price). We split on a price-shaped number to
# pull both halves.

LEXUS_URL_TMPL = "https://www.lexus.com.sa/en/{slug}"


async def scrape_lexus_official(model: str, year: int = 0) -> list[dict]:
    """Scrape lexus.com.sa for the given model. Returns list of
    {"trim", "price", "url"}.
    """
    slug = model.lower().replace(" ", "-")
    url = LEXUS_URL_TMPL.format(slug=slug)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=headers)
        except Exception as e:
            logger.warning("Lexus fetch %s: %s", url, e)
            return []
        if r.status_code != 200:
            return []
        html = r.text

    soup = BeautifulSoup(html, "html.parser")
    target_box = None
    for box in soup.select(".accordion-box"):
        title = (box.get("data-gtm-property-title") or "").lower()
        section = (box.get("data-gtm-property-section") or "").lower()
        # The pricing list lives in the "performance"/"specs" accordion.
        if "engine" in title or "performance" in title or "spec" in section:
            target_box = box
            break
    if target_box is None:
        return []

    first_li = target_box.select_one("ul > li")
    if first_li is None:
        return []

    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    # Lexus's icon font uses Private Use Area chars (e.g. U+E900 for the
    # SAR currency symbol) which leak into the h5 text and break trim
    # comparison ('350 BB Excellence ' wouldn't match a marketplace
    # title's '350 BB Excellence'). Strip all PUA chars before normalizing.
    _PUA = re.compile(r"[-]")
    for el in first_li.select(".right-text"):
        h5 = el.find("h5")
        if not h5:
            continue
        text = _PUA.sub("", h5.get_text(" ", strip=True))
        text = re.sub(r"\s+", " ", text).strip()
        # h5 looks like "RX 350 BB Excellence  315,330" — split on the price.
        m = re.search(r"(\d{1,3}(?:,\d{3})+)", text)
        if not m:
            continue
        price = int(re.sub(r"[^\d]", "", m.group(1)))
        if price < 30_000 or price > 3_000_000:
            continue
        trim = text[:m.start()].strip()
        # Strip leading model name if present (the h5 has it, but the trim
        # label is more useful without).
        trim = re.sub(rf"^{re.escape(model)}\s+", "", trim, flags=re.IGNORECASE).strip()
        if not trim:
            continue
        key = (trim.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        out.append({"trim": trim, "price": price, "url": url})

    return out


# ─────────────────────────────────────────────────────────────────
#  Playwright helpers (shared browser context)
# ─────────────────────────────────────────────────────────────────

async def _new_context(browser):
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        locale="ar-SA",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={"Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8"},
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return ctx


async def _new_page(browser):
    ctx = await _new_context(browser)
    page = await ctx.new_page()
    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,svg,mp4}", lambda r: r.abort()
    )
    return page, ctx


def _clean_int(s: Any) -> int:
    if s is None:
        return 0
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits else 0


def _next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _walk_for_listings(data, depth: int = 0, _seen=None) -> list[dict]:
    """Walk Next.js JSON looking for any list whose items look like car listings.

    Each site nests listings at a different path; rather than maintain a
    site-specific path map per release, we walk the tree and return the first
    list of dicts that have at least 2 of: price, title, year, model.
    """
    if depth > 8:
        return []
    if _seen is None:
        _seen = set()
    obj_id = id(data)
    if obj_id in _seen:
        return []
    _seen.add(obj_id)
    if isinstance(data, list):
        head = [x for x in data[:6] if isinstance(x, dict)]
        if len(head) >= 2:
            score_kw = ("price", "cash_price", "cashPrice", "title", "name",
                         "model", "year", "starting_price", "min_price")
            hits = sum(
                1 for d in head
                if sum(1 for k in score_kw if k in d) >= 2
            )
            if hits >= 2:
                return data
        for item in data:
            r = _walk_for_listings(item, depth + 1, _seen)
            if r:
                return r
    elif isinstance(data, dict):
        for v in data.values():
            r = _walk_for_listings(v, depth + 1, _seen)
            if r:
                return r
    return []


# ─────────────────────────────────────────────────────────────────
#  Haraj (per-trim search — same as before)
# ─────────────────────────────────────────────────────────────────

async def _haraj_extract(page, model_ar: str, model_en: str, year: int) -> list[dict]:
    """Extract listings from Haraj's React Router loaderData.

    Accepts both Arabic and English model names because Saudi Haraj sellers
    mix scripts freely — 'لكزس RX 350 بنزين سعودي 2025' is typical.
    """
    return await page.evaluate(
        """({modelAr, modelEn, year}) => {
            try {
                const ctx = window.__reactRouterContext;
                if (!ctx) return [];
                const data = JSON.parse(JSON.stringify(ctx));
                let posts = [];
                const ld = data.state.loaderData || {};
                const sd = ld['en:search-keyword'] || ld['ar:search-keyword'] || {};
                const td = ld['en:tag'] || ld['ar:tag'] || {};
                const src = sd.dehydratedState ? sd : td;
                if (Array.isArray(sd.posts)) posts = sd.posts;
                if (!posts.length) {
                    const queries = (src.dehydratedState || {}).queries || [];
                    for (const q of queries) {
                        const pages = q.state?.data?.pages || [];
                        for (const pg of pages) {
                            const items = pg.search?.items || pg.posts?.items || pg.items || [];
                            posts.push(...items);
                        }
                    }
                }
                if (!posts.length) return [];
                const partsKw = ['قطع غيار','تشليح','شمعة','جنوط','جنط','طارة','اسسنار','بطارية كامري','مساعدات','مكينة'];
                const wantedKw = ['مطلوب','مطلووب','ابي البدل'];
                const ar = (modelAr || '').toLowerCase();
                const en = (modelEn || '').toLowerCase();
                const out = [];
                for (const post of posts) {
                    if (!post || !post.title) continue;
                    const title = post.title;
                    const body = post.bodyTEXT || '';
                    const all = (title + ' ' + body).toLowerCase();
                    // Accept either Arabic OR English model name.
                    const hasAr = ar && all.includes(ar);
                    const hasEn = en && all.includes(en);
                    if (!hasAr && !hasEn) continue;
                    const tl = title.toLowerCase();
                    if (partsKw.some(k => tl.includes(k))) continue;
                    if (wantedKw.some(k => tl.includes(k))) continue;
                    if (year) {
                        const ys = String(year);
                        const titleHit = title.includes(ys);
                        const bodyHit = new RegExp('(?:^|[^0-9])' + ys + '(?:[^0-9]|$)').test(body);
                        if (!titleHit && !bodyHit) continue;
                    }
                    // Trust post.price.formattedPrice exclusively — body-regex
                    // fallback was matching installment amounts and shipping
                    // fees inside 'السعر بالخاص' (price by message) listings.
                    let price = 0;
                    if (post.price && post.price.formattedPrice) {
                        const raw = post.price.formattedPrice.replace(/[,،]/g, '');
                        price = parseInt(raw) || 0;
                        // Haraj sometimes stores prices in thousands ('118' = 118,000).
                        if (price > 0 && price < 1000) price *= 1000;
                    }
                    if (!price || price < 20000 || price > 2000000) continue;
                    out.push({title, price, city: post.geoCity || post.city || '', url: 'https://haraj.com.sa/' + (post.URL || post.id || '')});
                }
                return out;
            } catch (e) { return [{error: e.message}]; }
        }""",
        {"modelAr": model_ar, "modelEn": model_en, "year": year},
    )


BRAND_AR = {
    "toyota": "تويوتا",
    "lexus":  "لكزس",
}


async def scrape_haraj_for_model(
    browser, brand: str, model: str, year: int,
) -> list[dict]:
    """One Haraj search per (brand, model, year) — gets every listing and
    leaves trim attribution to the caller.

    Replaces the older per-trim approach which was 7-9× slower and broke
    completely for Lexus (the Arabic trim name 'آر اكس 2025 350 BB Excellence'
    is too specific to match anything). Falls back through 3 query forms,
    keeping the first one that returns data.
    """
    if not PLAYWRIGHT_OK:
        return []
    model_ar = MODEL_AR.get(model.lower(), model)
    brand_ar = BRAND_AR.get(brand.lower(), "")

    # Try each query form; stop at the first that returns listings.
    queries = []
    if brand_ar:
        queries.append(f"{brand_ar} {model_ar} {year}")
    queries.append(f"{model_ar} {year}")
    queries.append(f"{model} {year}")  # English fallback
    urls = [
        f"https://haraj.com.sa/search/{urllib.parse.quote(q.strip())}/"
        for q in queries
    ]
    # Tag-based fallback also tries — sometimes returns more results.
    urls.append(
        f"https://haraj.com.sa/tags/{urllib.parse.quote(model_ar + ' ' + str(year))}"
    )

    page, ctx = await _new_page(browser)
    out: list[dict] = []
    seen_urls: set[str] = set()
    try:
        for url in urls:
            try:
                # Haraj's tracking pixels keep the network busy, so
                # `networkidle` rarely settles. Use `domcontentloaded` and
                # give __reactRouterContext a moment to hydrate.
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                # Wait for React Router to hydrate the loader data.
                try:
                    await page.wait_for_function(
                        "() => window.__reactRouterContext != null",
                        timeout=8_000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                listings = await _haraj_extract(page, model_ar, model, year)
                if not listings or (
                    isinstance(listings[0], dict) and "error" in listings[0]
                ):
                    continue
                # Dedup across query attempts.
                for l in listings:
                    u = l.get("url", "")
                    if u and u in seen_urls:
                        continue
                    if u:
                        seen_urls.add(u)
                    out.append(l)
                # Stop once we've got reasonable coverage.
                if len(out) >= 20:
                    break
            except Exception as e:
                logger.warning("Haraj %s: %s", url, e)
    finally:
        await ctx.close()
    return out


# ─────────────────────────────────────────────────────────────────
#  Syarah, Motory, YallaMotor — scrape model page once
# ─────────────────────────────────────────────────────────────────

SYARAH_SLUGS = {
    "rav 4": "rav4", "rav4": "rav4",
    "land cruiser": "land-cruiser", "landcruiser wagon": "land-cruiser",
    "hilux double cab": "hilux", "hilux single cab": "hilux",
    "corolla cross": "corolla-cross",
    "urban cruiser": "urban-cruiser",
}

MOTORY_SLUGS = {
    "rav 4": "rav4", "rav4": "rav4",
    "land cruiser": "land-cruiser",
    "corolla cross": "corolla-cross",
    "urban cruiser": "urban-cruiser",
}


async def _load_html(browser, urls: list[str], wait_for: str | None = None) -> str | None:
    """Load the first URL that returns 200 with usable content; return its HTML."""
    if not PLAYWRIGHT_OK:
        return None
    page, ctx = await _new_page(browser)
    try:
        for url in urls:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                if not resp or resp.status >= 400:
                    continue
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=5_000)
                    except Exception:
                        pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    await asyncio.sleep(0.5)
                return await page.content()
            except Exception as e:
                logger.warning("page load %s: %s", url, e)
        return None
    finally:
        await ctx.close()


# ─── Syarah ─────────────────────────────────────────────────────
# DOM pattern: `[id^="posts-card-cash-price"]` is the cash-price block; its
# nearest <a> ancestor is the listing card and contains an <h2> with the title.
# Skips installment numbers ("التقسيط" / "شهري" in surrounding text).

async def scrape_syarah_model(browser, brand: str, model: str, year: int) -> list[dict]:
    slug = SYARAH_SLUGS.get(model.lower(), model.lower().replace(" ", "-"))
    urls = [
        f"https://syarah.com/autos/{brand.lower()}/{slug}/{year}?type=new",
        f"https://syarah.com/cars/{brand.lower()}/{slug}?year={year}&type=new",
        f"https://syarah.com/cars/{brand.lower()}/{slug}",
    ]
    html = await _load_html(browser, urls, wait_for="[id^='posts-card-cash-price']")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    ml_en = model.lower()
    ml_ar = MODEL_AR.get(ml_en, "")
    def _matches_model(t: str) -> bool:
        tl = t.lower()
        return ml_en in tl or (ml_ar and ml_ar in tl)
    for block in soup.select("[id^='posts-card-cash-price']"):
        ctx_text = block.get_text(" ", strip=True)
        if "التقسيط" in ctx_text or "شهري" in ctx_text:
            continue
        price_tag = block.select_one(".font-bold") or block.find("span")
        if not price_tag:
            continue
        price = _clean_int(price_tag.get_text(strip=True))
        if not price or price < 30_000:
            continue
        card = block.find_parent("a")
        title = ""
        url_path = ""
        if card:
            url_path = card.get("href", "") or ""
            t_tag = card.select_one("h2") or card.select_one("h3")
            if t_tag:
                title = t_tag.get_text(strip=True)
        if not title:
            title = f"{brand} {model}"
        if not _matches_model(title):
            continue
        key = (title.lower()[:80], price)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "price": price,
            "city": "",
            "url": (f"https://syarah.com{url_path}" if url_path.startswith("/") else url_path),
        })
    return out


# ─── Motory ─────────────────────────────────────────────────────
# DOM pattern: Angular `<app-car-card>` wraps a `.car-card` element with
# `.title a` and `.price .value`.

async def scrape_motory_model(browser, brand: str, model: str, year: int) -> list[dict]:
    slug = MOTORY_SLUGS.get(model.lower(), model.lower().replace(" ", "-"))
    urls = [
        f"https://ksa.Motory.com/en/new-cars/{brand.lower()}/{slug}/{year}",
        f"https://ksa.Motory.com/en/new-cars/{brand.lower()}/{slug}",
    ]
    html = await _load_html(browser, urls, wait_for="app-car-card")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    ml_en = model.lower()
    ml_ar = MODEL_AR.get(ml_en, "")
    cards = soup.select("app-car-card .car-card") or soup.select(".car-card")
    for card in cards:
        title = ""
        t_tag = card.select_one(".title a") or card.select_one(".title") \
                 or card.select_one("h3") or card.select_one("h2")
        if t_tag:
            title = t_tag.get_text(" ", strip=True)
        tl = title.lower()
        if not title or (ml_en not in tl and not (ml_ar and ml_ar in tl)):
            continue
        p_tag = card.select_one(".price .value") or card.select_one(".price") \
                 or card.find(string=re.compile(r"\d{2,3},\d{3}"))
        price = _clean_int(p_tag.get_text(strip=True) if hasattr(p_tag, "get_text") else p_tag)
        if not price or price < 30_000:
            continue
        key = (title.lower()[:80], price)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "price": price, "city": "", "url": ""})
    return out


# ─── YallaMotor ─────────────────────────────────────────────────
# Server-rendered, no Angular components. Each grade card is a <h3> next to
# a price-bearing block. Walk every <h3> that matches the model, then look
# upwards for an SAR-shaped number in the same card.

async def scrape_yallamotor_model(browser, brand: str, model: str, year: int) -> list[dict]:
    slug = model.lower().replace(" ", "-")
    urls = [
        f"https://ksa.yallamotor.com/en/new-cars/{brand.lower()}/{slug}/{year}",
        f"https://ksa.yallamotor.com/new-cars/{brand.lower()}/{slug}/{year}",
        f"https://ksa.yallamotor.com/new-cars/{brand.lower()}/{slug}",
    ]
    # YallaMotor needs `networkidle` directly — `domcontentloaded` returns
    # before the trim cards have been hydrated, and the shared _load_html
    # wait_for_selector races with the redirect.
    if not PLAYWRIGHT_OK:
        return []
    page, ctx = await _new_page(browser)
    html = ""
    try:
        for url in urls:
            try:
                # YallaMotor's ad/tracking traffic keeps the network busy
                # indefinitely, so `networkidle` never fires. Use
                # `domcontentloaded` and wait specifically for the trim cards.
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                if resp and resp.status >= 400:
                    continue
                try:
                    await page.wait_for_selector(
                        "div.flex.flex-col.gap-4 h3", timeout=8_000
                    )
                except Exception:
                    # Cards never appeared on this URL — try the next.
                    continue
                # A small settle wait so the price <span>s mount.
                await asyncio.sleep(0.5)
                html = await page.content()
                if html and "flex-col" in html:
                    break
            except Exception as e:
                logger.warning("YallaMotor %s: %s", url, e)
    finally:
        await ctx.close()
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    ml_en = model.lower()
    ml_ar = MODEL_AR.get(ml_en, "")
    # YallaMotor trim cards live under `div.flex.flex-col.gap-4`. Each card
    # contains an h3 with the trim title and a span like `SAR 113,850`.
    cards = soup.select("div.flex.flex-col.gap-4")
    if not cards:
        # Fallback: walk every h3 + its nearest card-shaped ancestor.
        cards = [h.find_parent("div") for h in soup.find_all("h3") if h.find_parent("div")]
    for card in cards:
        if card is None:
            continue
        h3 = card.find("h3")
        if not h3:
            continue
        title = h3.get_text(" ", strip=True)
        tl = title.lower()
        if not title or (ml_en not in tl and not (ml_ar and ml_ar in tl)):
            continue
        text = card.get_text(" ", strip=True)
        # Skip installment cards.
        if "شهري" in text or "monthly" in text.lower() or "/mo" in text.lower():
            continue
        m = re.search(r"SAR\s*([\d,]+)", text) or re.search(r"\b(\d{2,3}(?:,\d{3})+)\b", text)
        if not m:
            continue
        price = _clean_int(m.group(1))
        if not price or price < 30_000 or price > 2_000_000:
            continue
        url_path = ""
        a = card.find("a", href=True)
        if a:
            url_path = a["href"]
        key = (title.lower()[:80], price)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "price": price,
            "city": "",
            "url": (f"https://ksa.yallamotor.com{url_path}"
                     if url_path.startswith("/") else url_path),
        })
    return out


# ─────────────────────────────────────────────────────────────────
#  Trim attribution
# ─────────────────────────────────────────────────────────────────

# Trims with these names are too short / too generic to match by substring.
# We require word-boundary match for them.
_GENERIC_SHORT_TRIMS = {"e", "le", "se", "ex", "y", "yx", "x", "rs", "v"}

# Trim-name aliases used by other Saudi sites. Keys are the alternate form
# (Arabic spelling, alternate English wording, etc.); values are the canonical
# Toyota.com.sa English name. Matching expands to include these aliases.
TRIM_ALIASES = {
    # Arabic ↔ English
    "جراندي": "GRANDE",
    "غراندي": "GRANDE",
    "قراندي": "GRANDE",
    "لومير": "Lumiere",
    "هايبرد": "HEV",
    "هايبريد": "HEV",
    "ال اي": "LE",
    "اي بلس": "E Plus",
    # New-car aggregator wording variants
    "grande hybrid": "GRANDE",       # YallaMotor calls hybrid Grande "Grande Hybrid"
    "lumiere hybrid": "Lumiere HEV",
    "le std": "LE",                   # YallaMotor's "LE STD" ≈ Toyota's base LE
}


def _normalize_for_match(s: str) -> str:
    s = s.lower()
    # Common abbreviation normalization
    s = s.replace(" hev", " hybrid").replace(" dsl", " diesel")
    return s


def _expand_with_aliases(title: str) -> str:
    """Append the canonical form of any alias in the title, so a single
    matcher pass catches both the alias and the canonical name."""
    extras = []
    tl = title.lower()
    for alias, canon in TRIM_ALIASES.items():
        if alias.lower() in tl:
            extras.append(canon)
    if extras:
        return title + " " + " ".join(extras)
    return title


def _trim_in_title(trim: str, title: str) -> bool:
    """Word-boundary trim match in a listing title (alias-aware, strict).

    All tokens require a word-boundary match. Boundaries reject BOTH letters
    and digits on either side, so:
      - 'TX' does NOT match 'TX2'      (digit after blocks)
      - 'ADV' does NOT match 'Adventure' (letter after blocks)
      - 'GRANDE' DOES match 'Camry Grande Hybrid' (space on both sides)
    """
    expanded = _expand_with_aliases(title)
    t = _normalize_for_match(expanded)
    parts = _normalize_for_match(trim).split()
    if not parts:
        return False
    for p in parts:
        if not re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", t):
            return False
    return True


def _best_trim_for_title(title: str, candidates: list[str]) -> str | None:
    """Pick the MOST SPECIFIC matching trim.

    'Toyota Camry E HEV' matches both 'E' and 'E HEV'. We want 'E HEV' (more
    tokens = more specific). Ties broken by total character length.
    """
    matches = [c for c in candidates if _trim_in_title(c, title)]
    if not matches:
        return None
    return max(matches, key=lambda c: (len(c.split()), len(c)))


# Engine-displacement tokens that listings frequently omit:
#   - Toyota-style "1.5L" / "2.0" / "1.8l"
#   - Lexus-style  "350"  / "500h"  / "350h"
_ENGINE_TOKEN_RE = re.compile(r"^(?:\d+\.\d+l?|\d{3}h?)$")
_VARIANT_SUFFIX_RE = re.compile(r"^([a-z]+)\d+$")  # matches "txl1", "vx2", "gxr3"

# Lexus uses internal 2-letter grade codes between engine size and trim name
# (e.g. "350 BB Excellence", "350h BH Hybrid", "500h FH F-Sport Hybrid").
# Marketplaces strip these — a Syarah listing for the F-SPORT trim says
# "350 F-Sport" not "350 FF F-Sport". Drop these codes during relaxed match.
# Hardcoded set covers the codes observed across RX/LX/ES/NX/IS/LS/LC/UX.
_LEXUS_GRADE_CODES = {
    "aa", "ah", "ba", "bb", "bd", "bh", "bk",
    "cc", "dd", "fa", "fb", "ff", "fh", "hh",
    "oh", "ot", "vp", "vr", "wb",
}


def _strip_engine_tokens(trim: str) -> str:
    """Remove engine-displacement tokens from a trim name.

    'Corolla 1.5L XLI Executive' → 'Corolla XLI Executive'
    """
    parts = [p for p in trim.split() if not _ENGINE_TOKEN_RE.match(p.lower())]
    return " ".join(parts) if parts else trim


def _relax_trim(trim: str) -> str:
    """Aggressive normalization for relaxed matching:
      1. Drop engine displacement tokens     ('1.5L XLI' → 'XLI')
      2. Remove hyphens within tokens        ('TX-2' → 'TX2')
      3. Strip trailing variant digits       ('TX2' → 'TX', 'TXL1' → 'TXL')

    Examples:
      'Prado TX-2'   → 'prado tx'
      'Prado TXL-1'  → 'prado txl'
      'Prado VXL-3'  → 'prado vxl'
      '1.5L XLI'     → 'xli'

    Listings often drop the hyphen ('TX2') and/or the variant number
    ('TXL' for any of TXL-1/TXL-2/TXL-3); fully-relaxed matching catches
    all of these. Price proximity then disambiguates among the variants.
    """
    parts = []
    for p in trim.lower().split():
        if _ENGINE_TOKEN_RE.match(p):
            continue
        # Drop Lexus internal grade codes (BB/FF/BH/etc.) — marketplaces
        # don't echo them in listing titles.
        if p in _LEXUS_GRADE_CODES:
            continue
        p = p.replace("-", "")
        m = _VARIANT_SUFFIX_RE.match(p)
        if m:
            p = m.group(1)
        if p:
            parts.append(p)
    return " ".join(parts) if parts else trim.lower()


def _normalize_for_relaxed_match(s: str) -> str:
    """For relaxed matching:
      - lowercase + alias expansion (via _normalize_for_match)
      - drop hyphens within tokens                'TX-2' → 'TX2'
      - strip trailing variant digits from words  'TXL1' → 'TXL'

    The title-side normalization mirrors what _relax_trim does to candidates,
    so a Toyota trim 'TXL-1' (relaxed → 'txl') matches a listing title written
    as either 'TXL-1' or 'TXL1' or just 'TXL'.
    """
    s = _normalize_for_match(s)
    s = s.replace("-", "")
    # Strip trailing variant digit from any whole word: 'txl1' → 'txl', 'tx2' → 'tx'.
    s = re.sub(r"\b([a-z]{2,})\d+\b", r"\1", s)
    return s


def _trim_in_title_relaxed(relaxed_trim: str, title: str) -> bool:
    """Match a pre-relaxed trim against a relaxed-normalized title.

    Word-boundary on both sides (same rule as _trim_in_title) — letters and
    digits both block, so 'adv' won't substring-match into 'adventure'.
    """
    expanded = _expand_with_aliases(title)
    t = _normalize_for_relaxed_match(expanded)
    parts = relaxed_trim.split()
    if not parts:
        return False
    for p in parts:
        if not re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", t):
            return False
    return True


def _best_trim_for_listing(
    title: str, price: int,
    candidates_with_msrp: list[tuple[str, int]],
) -> str | None:
    """Two-pass attribution:

    Pass 1 — STRICT: every token of the trim must appear as a word in the
             title (with the existing alias map applied). Most-specific
             trim wins (most tokens, then longest string).

    Pass 2 — RELAXED: drop engine-displacement tokens, remove hyphens, and
             strip trailing variant digits before matching. Tie-breaks:
               (a) most-specific by relaxed-token count
               (b) price proximity to the listing's asking price

    The relaxation handles three real-world title patterns:
      - Marketplace drops engine size:    'Corolla 1.5L XLI' → 'Corolla XLI'
      - Marketplace drops the hyphen:     'Prado TX-2'       → 'Prado TX2'
      - Marketplace drops the variant #:  'Prado TXL-1'      → 'Prado TXL'
    """
    candidates = [c for c, _ in candidates_with_msrp]

    # Pass 1: strict
    strict = [c for c in candidates if _trim_in_title(c, title)]
    if strict:
        return max(strict, key=lambda c: (len(c.split()), len(c)))

    # Pass 2: fully relaxed (engine + hyphen + variant-suffix)
    relaxed = []
    for c, msrp in candidates_with_msrp:
        rc = _relax_trim(c)
        if rc and _trim_in_title_relaxed(rc, title):
            relaxed.append((c, msrp))
    if not relaxed:
        return None

    max_spec = max(len(_relax_trim(c).split()) for c, _ in relaxed)
    finalists = [
        (c, msrp) for c, msrp in relaxed
        if len(_relax_trim(c).split()) == max_spec
    ]
    if len(finalists) == 1:
        return finalists[0][0]
    if price > 0:
        return min(finalists, key=lambda x: abs(x[1] - price))[0]
    return finalists[0][0]


def _is_garbage_price(price: int) -> bool:
    s = str(price)
    if re.search(r"(\d)\1{4,}", s):
        return True
    if s in ("1234567", "12345678", "1111", "9999"):
        return True
    return False


def _filter_prices(
    listings: list[dict], official_msrp: int,
    *, low_pct: float = 0.50, high_pct: float = 1.50,
) -> list[dict]:
    if not official_msrp:
        return [l for l in listings if not _is_garbage_price(l.get("price", 0))]
    lo = int(official_msrp * low_pct)
    hi = int(official_msrp * high_pct)
    out = []
    for l in listings:
        p = l.get("price", 0)
        if _is_garbage_price(p):
            continue
        if lo <= p <= hi:
            out.append(l)
    return out


def _attribute_to_trim(
    listings: list[dict], trim: str, msrp: int,
    *, all_trims_with_msrp: list[tuple[str, int]] | None = None,
) -> list[dict]:
    """Attribute listings to a trim — most-specific wins, then engine-relaxed
    with price proximity as fallback.
    """
    matched = []
    for l in listings:
        title = l.get("title", "")
        listing_price = l.get("price", 0) or 0
        if all_trims_with_msrp:
            best = _best_trim_for_listing(title, listing_price, all_trims_with_msrp)
            if best == trim:
                matched.append(l)
        else:
            if _trim_in_title(trim, title):
                matched.append(l)
    return _filter_prices(matched, msrp)


def _avg(values: list[int]) -> int:
    return int(sum(values) / len(values)) if values else 0


# ─────────────────────────────────────────────────────────────────
#  Schema helpers — convert raw scraped data to the market-intelligence shape
# ─────────────────────────────────────────────────────────────────

SOURCE_NAMES = {
    "toyota.com.sa":      "Toyota.com.sa",
    "lexus.com.sa":       "Lexus.com.sa",
    "haraj.com.sa":       "Haraj.com.sa",
    "syarah.com":         "Syarah.com",
    "ksa.Motory.com":     "Motory.com",
    "ksa.yallamotor.com": "YallaMotor.com",
}

# Source category → priceType / sellerType / default condition
_SOURCE_META = {
    "toyota.com.sa":      ("official_msrp", "official_dealer", "جديدة"),
    "lexus.com.sa":       ("official_msrp", "official_dealer", "جديدة"),
    "haraj.com.sa":       ("actual",        "individual",      "غير محدد"),
    "syarah.com":         ("actual",        "dealer",          "غير محدد"),
    "ksa.Motory.com":     ("actual",        "dealer",          "جديدة"),
    "ksa.yallamotor.com": ("actual",        "dealer",          "جديدة"),
}


def _engine_from_trim(trim: str) -> str:
    """Pull engine displacement and fuel hints out of a trim string.
    'Toyota Corolla 1.5L XLI' → '1.5L'; 'RX 350h BH Hybrid' → '350h Hybrid'.
    Best-effort — used only for the schema's `engine` field which the model
    can override later via AI enrichment.
    """
    tokens = trim.split()
    out = []
    for t in tokens:
        tl = t.lower()
        if _ENGINE_TOKEN_RE.match(tl):
            out.append(t.upper() if tl.endswith("l") else t)
        elif tl in ("hybrid", "hev", "diesel", "dsl", "petrol", "gasoline"):
            out.append(t.title())
    return " ".join(out)


def _aliases_for_trim(trim: str) -> list[str]:
    """Reverse-lookup TRIM_ALIASES → which alternate names map to this trim."""
    out = []
    for alias, canon in TRIM_ALIASES.items():
        if canon.lower() == trim.lower():
            out.append(alias)
    return out


def _detect_condition(title: str, source_id: str) -> str:
    """Detect جديدة / مستعملة / غير محدد from listing title + source defaults."""
    t = (title or "").lower()
    new_kw = ("جديد", "جديدة", "اصفار", "أصفار", "زيرو", "new", "brand new")
    used_kw = ("مستعمل", "مستعملة", "نظيف", "ممشى", "used", "pre-owned")
    if any(k in t for k in new_kw) and not any(k in t for k in used_kw):
        return "جديدة"
    if any(k in t for k in used_kw):
        return "مستعملة"
    # fall back to per-source default
    return _SOURCE_META.get(source_id, (None, None, "غير محدد"))[2]


def _to_schema_listing(
    raw: dict, source_id: str, brand: str, model: str,
    *, is_official: bool = False, official_msrp: int = 0,
) -> dict:
    """Translate one raw scraped listing into the schema's listing object."""
    price_type, seller_type, default_condition = _SOURCE_META.get(
        source_id, ("actual", "dealer", "غير محدد")
    )
    title = raw.get("title") or f"{brand} {model}"
    if is_official:
        condition = "جديدة"
        mileage = "0 كم"
        price_note = "السعر الرسمي شامل ضريبة 15%"
        confidence = "high"
    else:
        condition = _detect_condition(title, source_id)
        mileage = raw.get("mileage") or ("0 كم" if condition == "جديدة" else "غير محدد")
        if official_msrp and raw.get("price"):
            diff_pct = (raw["price"] - official_msrp) / official_msrp * 100
            price_note = f"vs MSRP: {diff_pct:+.1f}%"
        else:
            price_note = ""
        confidence = "medium"
    return {
        "source": source_id,
        "sourceName": SOURCE_NAMES.get(source_id, source_id),
        "listedAs": title,
        "priceType": price_type,
        "matchConfidence": confidence,
        "condition": condition,
        "price": raw.get("price", 0),
        "mileage": mileage,
        "location": raw.get("city") or "السعودية",
        "priceNote": price_note,
        "sellerType": seller_type,
        "sellerName": raw.get("sellerName") or SOURCE_NAMES.get(source_id, ""),
        "postedDaysAgo": raw.get("postedDaysAgo", 0),
        "imageUrl": raw.get("imageUrl", ""),
        "url": raw.get("url", ""),
    }


def _build_references(brand: str, model: str, year: int,
                       official_url: str | None,
                       official_source_id: str) -> dict:
    """Per-source landing URLs the user can click to verify data."""
    brand_l = brand.lower()
    model_slug_dash = model.lower().replace(" ", "-")
    syarah_slug = SYARAH_SLUGS.get(model.lower(), model_slug_dash)
    motory_slug = MOTORY_SLUGS.get(model.lower(), model_slug_dash)
    model_ar = MODEL_AR.get(model.lower(), model)
    haraj_q = urllib.parse.quote(f"{model_ar} {year}".strip())
    refs = {
        official_source_id: {
            "name": SOURCE_NAMES.get(official_source_id, official_source_id),
            "url": official_url or f"https://www.{official_source_id}/",
        },
        "haraj.com.sa": {
            "name": SOURCE_NAMES["haraj.com.sa"],
            "url": f"https://haraj.com.sa/search/{haraj_q}/",
        },
        "syarah.com": {
            "name": SOURCE_NAMES["syarah.com"],
            "url": f"https://syarah.com/autos/{brand_l}/{syarah_slug}/{year}?type=new",
        },
        "ksa.Motory.com": {
            "name": SOURCE_NAMES["ksa.Motory.com"],
            "url": f"https://ksa.Motory.com/en/new-cars/{brand_l}/{motory_slug}/{year}",
        },
        "ksa.yallamotor.com": {
            "name": SOURCE_NAMES["ksa.yallamotor.com"],
            "url": f"https://ksa.yallamotor.com/en/new-cars/{brand_l}/{model_slug_dash}/{year}",
        },
    }
    return refs


async def _ai_enrich_market_insight(
    brand: str, model: str, year: int,
    trims: list[dict], anthropic_key: str,
) -> dict:
    """Ask Claude to write `marketInsight` + `competitorAnalysis` for the
    REAL aggregated data (not estimates). Returns
    {marketInsight: str, competitorAnalysis: dict}. Empty dict on failure.
    """
    if not anthropic_key:
        return {}
    # Build a compact summary of trims to feed Claude.
    summary = []
    for t in trims:
        pa = t.get("priceAnalysis", {})
        summary.append({
            "trim": t.get("officialName", ""),
            "msrp": t.get("officialMSRP", 0),
            "marketMin": pa.get("marketMin", 0),
            "marketMax": pa.get("marketMax", 0),
            "marketAvg": pa.get("marketAvg", 0),
            "listingCount": pa.get("listingCount", 0),
        })
    prompt = (
        f"You are a Saudi automotive market expert. Below is REAL aggregated "
        f"price data for the {year} {brand} {model} from "
        f"toyota/lexus.com.sa, haraj.com.sa, syarah.com, ksa.Motory.com, "
        f"and ksa.yallamotor.com. Write two Arabic sections:\n\n"
        f"1. marketInsight — a 2-3 sentence summary of demand, supply, "
        f"and pricing trends in the Saudi market for this vehicle.\n"
        f"2. competitorAnalysis — with `summary` (Arabic), "
        f"`opportunities` (Arabic array of 2-4 strings), `threats` "
        f"(Arabic array of 1-3 strings), and `recommendation` (Arabic).\n\n"
        f"Real data:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
        f"Return ONLY a JSON object with two top-level keys: marketInsight "
        f"(string) and competitorAnalysis (object). No markdown, no extra prose."
    )
    try:
        async with httpx.AsyncClient(timeout=60, http2=False) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        # Strip markdown fences if any.
        text = re.sub(r"```(?:json)?\s*|\s*```", "", text)
        return json.loads(text)
    except Exception as e:
        logger.warning("AI enrichment failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────
#  Main aggregator
# ─────────────────────────────────────────────────────────────────

async def aggregate_prices(
    brand: str, model: str, year: int,
    *,
    haraj_concurrency: int = 3,
    enrich_with_ai: bool = False,
    anthropic_key: str = "",
) -> dict:
    """
    Returns per-trim avg prices from each source.

    The official source is brand-dependent:
      - brand="toyota"  → scrape toyota.com.sa
      - brand="lexus"   → scrape lexus.com.sa
    Other sources (Haraj, Syarah, Motory, YallaMotor) are queried for both
    brands; their URL paths already include the brand name.
    """
    brand_l = brand.lower()
    if brand_l == "lexus":
        official_source_id = "lexus.com.sa"
        print(f"  → {official_source_id}: scraping official trims...")
        official = await scrape_lexus_official(model, year)
        not_found_msg = ("Could not load Lexus.com.sa official trims (model "
                          "slug missing, no spec section, or page redesign).")
    else:
        official_source_id = "toyota.com.sa"
        print(f"  → {official_source_id}: scraping official trims...")
        official = await scrape_toyota_official(model, year)
        not_found_msg = ("Could not load Toyota.com.sa official trims "
                          "(model slug missing or page redesign).")

    if not official:
        return {
            "vehicle": f"{year} {brand} {model}",
            "brand": brand, "model": model, "year": year,
            "trims": [],
            "error": not_found_msg,
        }
    print(f"  → {official_source_id}: found {len(official)} trims")

    haraj_listings: list[dict] = []
    syarah_listings: list[dict] = []
    motory_listings: list[dict] = []
    yallamotor_listings: list[dict] = []

    if PLAYWRIGHT_OK:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            try:
                # ── Phase 1a: Syarah + Motory + Haraj (concurrent) ──
                # All three are single per-model fetches now. YallaMotor still
                # runs solo afterward because its ad/tracker traffic prevents
                # `networkidle` from settling under contention.
                print(f"     • syarah.com + ksa.Motory.com + haraj.com.sa (parallel)")

                async def _syarah():
                    syarah_listings.extend(
                        await scrape_syarah_model(browser, brand, model, year)
                    )

                async def _motory():
                    motory_listings.extend(
                        await scrape_motory_model(browser, brand, model, year)
                    )

                async def _haraj():
                    haraj_listings.extend(
                        await scrape_haraj_for_model(browser, brand, model, year)
                    )

                await asyncio.gather(_syarah(), _motory(), _haraj(), return_exceptions=False)

                # ── Phase 1b: YallaMotor (sequential — bot-detects concurrent loads) ──
                print(f"     • ksa.yallamotor.com (sequential)")
                yallamotor_listings.extend(
                    await scrape_yallamotor_model(browser, brand, model, year)
                )
            finally:
                await browser.close()
    else:
        logger.warning("Playwright not installed — only the official source will be queried.")

    # ── Build per-trim per-source aggregates ─────────────────
    trim_names = [t["trim"] for t in official]
    trims_with_msrp = [(t["trim"], t["price"]) for t in official]

    # Pre-compute which listings *failed* to attribute to ANY trim.
    # These end up in the "Other" row so they're visible in the table
    # rather than silently dropped.
    def _unattributed(listings: list[dict]) -> list[dict]:
        return [
            l for l in listings
            if _best_trim_for_listing(
                l.get("title", ""), l.get("price", 0) or 0, trims_with_msrp,
            ) is None
        ]

    haraj_other = _unattributed(haraj_listings)
    syarah_other = _unattributed(syarah_listings)
    motory_other = _unattributed(motory_listings)
    yalla_other = _unattributed(yallamotor_listings)

    trims_out: list[dict] = []
    for off in official:
        name = off["trim"]
        msrp = off["price"]

        haraj_match  = _attribute_to_trim(haraj_listings,      name, msrp, all_trims_with_msrp=trims_with_msrp)
        syarah_match = _attribute_to_trim(syarah_listings,     name, msrp, all_trims_with_msrp=trims_with_msrp)
        motory_match = _attribute_to_trim(motory_listings,     name, msrp, all_trims_with_msrp=trims_with_msrp)
        yalla_match  = _attribute_to_trim(yallamotor_listings, name, msrp, all_trims_with_msrp=trims_with_msrp)

        # Build the full per-listing array — first the official MSRP entry,
        # then attributed marketplace listings.
        listings: list[dict] = []
        listings.append(_to_schema_listing(
            {"title": f"{brand} {model} {name}", "price": msrp, "url": off["url"]},
            official_source_id, brand, model,
            is_official=True, official_msrp=msrp,
        ))
        for src_id, src_listings in (
            ("haraj.com.sa",       haraj_match),
            ("syarah.com",         syarah_match),
            ("ksa.Motory.com",     motory_match),
            ("ksa.yallamotor.com", yalla_match),
        ):
            for l in src_listings:
                listings.append(_to_schema_listing(
                    l, src_id, brand, model,
                    is_official=False, official_msrp=msrp,
                ))

        # priceAnalysis from marketplace prices only (excluding the MSRP
        # itself so vsOfficialPct stays meaningful).
        market_prices = [
            l["price"] for l in listings
            if l["priceType"] == "actual" and l["price"] > 0
        ]
        market_min = min(market_prices) if market_prices else 0
        market_max = max(market_prices) if market_prices else 0
        market_avg = _avg(market_prices)
        vs_pct = round((market_avg - msrp) / msrp * 100, 1) if msrp and market_avg else 0
        if vs_pct > 1.5:
            trend = "up"
        elif vs_pct < -1.5:
            trend = "down"
        else:
            trend = "stable"

        trims_out.append({
            "officialName":   name,
            "officialNameAr": name,         # No Arabic conversion for now
            "officialMSRP":   msrp,
            "engine":         _engine_from_trim(name),
            "commonAliases":  _aliases_for_trim(name),
            "listings":       listings,
            "priceAnalysis": {
                "marketMin":     market_min,
                "marketMax":     market_max,
                "marketAvg":     market_avg,
                "vsOfficialPct": vs_pct,
                "trend":         trend,
                "listingCount":  len(market_prices),
            },
        })

    # ── "Other" row: listings each source had that don't map to any official trim ──
    other_all = []
    for src_id, leftovers in (
        ("haraj.com.sa",       haraj_other),
        ("syarah.com",         syarah_other),
        ("ksa.Motory.com",     motory_other),
        ("ksa.yallamotor.com", yalla_other),
    ):
        for l in leftovers:
            if not isinstance(l.get("price"), int) or l["price"] <= 0:
                continue
            other_all.append(_to_schema_listing(l, src_id, brand, model))
    if other_all:
        other_prices = [l["price"] for l in other_all]
        trims_out.append({
            "officialName":   f"Other (not on {official_source_id})",
            "officialNameAr": "أخرى",
            "officialMSRP":   0,
            "engine":         "",
            "commonAliases":  [],
            "listings":       other_all,
            "priceAnalysis": {
                "marketMin":     min(other_prices),
                "marketMax":     max(other_prices),
                "marketAvg":     _avg(other_prices),
                "vsOfficialPct": 0,
                "trend":         "stable",
                "listingCount":  len(other_prices),
            },
        })

    # ── Top-level fields: vehicle / officialPriceRange / references / etc ──
    all_msrps = [t["officialMSRP"] for t in trims_out if t["officialMSRP"]]
    official_url = official[0]["url"] if official else None
    references = _build_references(brand, model, year, official_url, official_source_id)
    search_date = datetime.now().strftime("%B %Y")

    result = {
        "vehicle":  f"{year} {brand} {model}",
        "brand":    brand,
        "model":    model,
        "year":     year,
        "searchDate": search_date,
        "isAIFallback": False,
        "officialPriceRange": {
            "min": min(all_msrps) if all_msrps else 0,
            "max": max(all_msrps) if all_msrps else 0,
        },
        "references":     references,
        "official_source": official_source_id,
        "sources_queried": list(references.keys()),
        "totals": {
            "haraj_listings":     len(haraj_listings),
            "syarah_listings":    len(syarah_listings),
            "motory_listings":    len(motory_listings),
            "yallamotor_listings": len(yallamotor_listings),
        },
        "marketInsight": "",         # filled by AI if enrich_with_ai=True
        "competitorAnalysis": {},    # filled by AI if enrich_with_ai=True
        "trims": trims_out,
    }

    # Optional AI enrichment for marketInsight + competitorAnalysis.
    if enrich_with_ai and anthropic_key:
        print(f"  → enriching with AI (marketInsight + competitorAnalysis)...")
        ai = await _ai_enrich_market_insight(brand, model, year, trims_out, anthropic_key)
        if ai:
            if isinstance(ai.get("marketInsight"), str):
                result["marketInsight"] = ai["marketInsight"]
            if isinstance(ai.get("competitorAnalysis"), dict):
                result["competitorAnalysis"] = ai["competitorAnalysis"]

    return result
