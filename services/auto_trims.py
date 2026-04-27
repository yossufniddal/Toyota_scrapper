"""
Auto-trim discovery — fetches trims from HJ API + generates keywords automatically.
Replaces manual trims_config.py dependency.
"""

import json
import logging
import re
import time

import httpx

logger = logging.getLogger("hj-price-intel.auto_trims")

# ── Abbreviation → Arabic keywords mapping ─────────────────────
ABBREV_MAP = {
    "dsl": ["ديزل", "diesel"],
    "hev": ["هايبرد", "هايبريد", "hybrid"],
    "at": ["اتوماتيك", "automatic"],
    "mt": ["قير عادي", "manual", "مانيوال"],
    "4x4": ["دبل", "فور باي فور", "دفع رباعي", "4wd"],
    "4x2": ["دفع خلفي", "2wd"],
    "v6": ["6 سلندر", "ست سلندر", "v6"],
    "v8": ["8 سلندر", "ثمان سلندر", "v8"],
    "vip": ["في اي بي", "في ايبي", "vip"],
    "sport": ["سبورت", "رياضي", "رياضية"],
    "limited": ["لميتد", "محدودة"],
    "executive": ["اكزكتف", "تنفيذي"],
    "platinum": ["بلاتينيوم", "بلاتينيم"],
    "grande": ["قراندي", "جراند"],
    "lumiere": ["لومير", "لوميار"],
    "premium": ["بريميوم", "فاخرة"],
    "gxr": ["جي اكس ار", "gxr"],
    "glx": ["جي ال اكس", "glx", "ستاندرد"],
    "sglx": ["اس جي ال اكس", "sglx"],
    "gle": ["جي ال اي", "gle"],
    "xle": ["اكس ال اي", "xle"],
    "se": ["اس اي", "se"],
    "le": ["ال اي", "le"],
    "ex": ["اي اكس", "ex"],
    "cross": ["كروس"],
    "urban": ["اربن"],
    "adventure": ["ادفنشر"],
}

# Brand name Arabic map  (BrandID → Arabic)
BRAND_AR = {"1": "تويوتا", "2": "لكزس"}

# HJ API URL
HJ_CATALOG_URL = "https://appw.hassanjameelapp.com/ar/api/finance/index"

# Cache for the raw catalog response (in-memory, refreshed every 6 hours)
_catalog_cache: dict = {}   # {"data": ..., "ts": float}
_CATALOG_TTL = 6 * 3600     # 6 hours

# Cache for AI-enriched keywords  {cache_key: {"keywords": [...], "ts": float}}
_kw_ai_cache: dict = {}
_KW_AI_TTL = 7 * 86400      # 7 days = 604800 seconds


# ── Helpers ──────────────────────────────────────────────────
async def _fetch_catalog_raw() -> dict:
    """Fetch raw catalog JSON from HJ API, with in-memory caching."""
    now = time.time()
    if _catalog_cache.get("data") and (now - _catalog_cache.get("ts", 0)) < _CATALOG_TTL:
        return _catalog_cache["data"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(HJ_CATALOG_URL)
            r.raise_for_status()
            data = r.json()
            _catalog_cache["data"] = data
            _catalog_cache["ts"] = now
            logger.info("HJ catalog fetched: %d brands, %d groups, %d modelTypes",
                        len(data.get("brands", [])),
                        len(data.get("groups", [])),
                        len(data.get("modelTypes", [])))
            return data
    except Exception as e:
        logger.error("Failed to fetch HJ catalog: %s", e)
        # Return stale cache if available
        if _catalog_cache.get("data"):
            logger.warning("Returning stale HJ catalog from cache")
            return _catalog_cache["data"]
        return {}


def _find_brand_id(brands: list[dict], brand_name: str) -> str | None:
    """Find BrandID by brand name (case-insensitive, supports English + Arabic)."""
    brand_lower = brand_name.lower()
    for b in brands:
        desc = (b.get("Description") or "").lower()
        bid = b.get("BrandID") or ""
        # Direct match on description
        if brand_lower in desc:
            return bid
        # Match on known Arabic names
        ar_name = BRAND_AR.get(str(bid), "")
        if ar_name and ar_name in brand_lower:
            return bid
        # "toyota" → BrandID 1, "lexus" → BrandID 2
        if brand_lower == "toyota" and bid in ("1", 1):
            return str(bid)
        if brand_lower == "lexus" and bid in ("2", 2):
            return str(bid)
    return None


def _normalize(s: str) -> str:
    """Strip spaces, hyphens, underscores for fuzzy comparison."""
    return re.sub(r'[\s\-_]+', '', s).lower()


def _find_group_ids(groups: list[dict], brand_id: str, model_name: str, year: int) -> list[str]:
    """Find ProductGroupId(s) matching brand, model name, and year."""
    model_lower = model_name.lower().strip()
    model_norm = _normalize(model_name)
    matched = []

    # First pass: exact model + year match
    for g in groups:
        g_brand = str(g.get("BrandId") or g.get("brandID") or "")
        if g_brand != str(brand_id):
            continue
        g_desc_en = (g.get("DescriptionEn") or "").lower()
        g_desc_ar = (g.get("DescriptionAr") or "").lower()
        g_year = str(g.get("Year") or "")
        if model_norm in _normalize(g_desc_en) or model_lower in g_desc_ar:
            if str(year) in g_year:
                gid = g.get("ProductGroupId") or g.get("productGroupID") or ""
                if gid:
                    matched.append(gid)

    # Second pass: model match without year constraint
    if not matched:
        for g in groups:
            g_brand = str(g.get("BrandId") or g.get("brandID") or "")
            if g_brand != str(brand_id):
                continue
            g_desc_en = (g.get("DescriptionEn") or "").lower()
            g_desc_ar = (g.get("DescriptionAr") or "").lower()
            if model_norm in _normalize(g_desc_en) or model_lower in g_desc_ar:
                gid = g.get("ProductGroupId") or g.get("productGroupID") or ""
                if gid:
                    matched.append(gid)

    return list(set(matched))


async def generate_keywords_from_name(short_desc: str) -> list[str]:
    """
    Generates Arabic + English keywords from trim short description.
    Example: "SGLX 2.4 4X4 DSL AT" → ["sglx", "4x4", "ديزل", "دبل", "اتوماتيك", "2.4"]
    """
    keywords = []
    tokens = re.split(r'[\s/\-]+', short_desc.strip())

    for token in tokens:
        token_lower = token.lower().strip()
        if not token_lower:
            continue

        # Always add the raw token as a keyword
        keywords.append(token_lower)

        # Check abbreviation map
        if token_lower in ABBREV_MAP:
            keywords.extend(ABBREV_MAP[token_lower])
        else:
            # Try partial matches for compound tokens (e.g., "4X4" might be stored as "4x4")
            for abbrev, expansions in ABBREV_MAP.items():
                if token_lower == abbrev:
                    keywords.extend(expansions)
                    break

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            unique.append(kw_clean)
    return unique


async def enrich_keywords_with_ai(brand: str, model: str, trim_name: str,
                                   existing_keywords: list[str],
                                   anthropic_key: str) -> list[str]:
    """
    Asks Claude for additional Saudi-market keywords for this trim.
    Called once per trim, cached for 7 days.
    """
    if not anthropic_key:
        return existing_keywords

    cache_key = f"hj:trim_kw:{brand.lower()}:{model.lower()}:{trim_name.lower().replace(' ', '_')}"
    now = time.time()

    # Check in-memory cache
    if cache_key in _kw_ai_cache:
        entry = _kw_ai_cache[cache_key]
        if now - entry["ts"] < _KW_AI_TTL:
            return entry["keywords"]

    prompt = (
        f"You are a Saudi automotive market expert. "
        f"What do Saudi car buyers commonly call the {brand} {model} trim '{trim_name}' "
        f"on marketplaces like Haraj and Syarah? "
        f"Return ONLY a JSON array of 5-10 additional Arabic/English keywords or slang terms "
        f"that buyers actually use in listings. No markdown, just the JSON array. "
        f"Already known keywords: {json.dumps(existing_keywords, ensure_ascii=False)}"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            raw_text = r.json()["content"][0]["text"].strip()
            # Parse JSON array from response
            clean = re.sub(r"```json|```", "", raw_text).strip()
            ai_keywords = json.loads(clean)
            if isinstance(ai_keywords, list):
                combined = list(existing_keywords)
                seen = set(k.lower() for k in combined)
                for kw in ai_keywords:
                    kw_str = str(kw).strip().lower()
                    if kw_str and kw_str not in seen:
                        combined.append(kw_str)
                        seen.add(kw_str)
                # Cache result
                _kw_ai_cache[cache_key] = {"keywords": combined, "ts": now}
                logger.info("AI enriched keywords for %s %s %s: +%d keywords",
                            brand, model, trim_name, len(combined) - len(existing_keywords))
                return combined
    except Exception as e:
        logger.warning("AI keyword enrichment failed for %s %s %s: %s",
                       brand, model, trim_name, e)

    return existing_keywords


async def fetch_trims_from_catalog(brand: str, model: str, year: int,
                                    anthropic_key: str = "") -> list[dict]:
    """
    Fetches trims from HJ API and generates keywords automatically.

    Returns list of dicts compatible with TrimInfo:
    [
        {
            "id": "MODEL_CODE",
            "name": "SGLX 2.4 4X4 DSL AT",
            "name_ar": "...",
            "msrp": 0,
            "engine": "2.4 DSL AT 4X4",
            "keywords": ["sglx", "4x4", "ديزل", "دبل", "اتوماتيك"],
            "match_score": 0.88,
            "image": "https://...",
        },
        ...
    ]
    """
    # 1. Fetch catalog from API (or cache)
    data = await _fetch_catalog_raw()
    if not data:
        logger.warning("No catalog data available for auto-trim discovery")
        return []

    brands_list = data.get("brands", [])
    groups = data.get("groups", [])
    model_types = data.get("modelTypes", [])

    # 2. Find brand by name → get BrandId
    brand_id = _find_brand_id(brands_list, brand)
    if not brand_id:
        logger.warning("Brand '%s' not found in HJ catalog", brand)
        return []

    # 3. Find groups matching model name + year → get ProductGroupId(s)
    group_ids = _find_group_ids(groups, brand_id, model, year)
    if not group_ids:
        logger.warning("No product group found for %s %s %d", brand, model, year)
        return []

    # 4. Filter modelTypes by ProductGroupId + year
    trims = []
    seen_names = set()

    for mt in model_types:
        mt_group = mt.get("ProductGroupId") or mt.get("productGroupID") or ""
        mt_year = str(mt.get("Model") or "")

        if mt_group not in group_ids:
            continue
        if str(year) not in mt_year:
            continue

        # Deduplicate by trim name — remove parenthesized codes like "(1L5)"
        short_desc = (mt.get("ShortDescriptionEn") or mt.get("descriptionEn") or "").strip()
        # Clean: "E Automatic (1L5)" → "E Automatic"
        clean_name = re.sub(r'\s*\([^)]*\)\s*', ' ', short_desc).strip().lower()
        if not clean_name or clean_name in seen_names:
            continue
        seen_names.add(clean_name)

        # Use cleaned name (without parenthesized codes)
        short_desc_en = re.sub(r'\s*\([^)]*\)\s*', ' ',
            (mt.get("ShortDescriptionEn") or mt.get("descriptionEn") or mt.get("Description") or "")
        ).strip()
        desc_ar = mt.get("descriptionAr") or ""
        image = mt.get("Image") or ""

        if not short_desc_en:
            continue

        # 5. Generate keywords from ShortDescriptionEn using ABBREV_MAP
        keywords = await generate_keywords_from_name(short_desc_en)
        # Add full name as keyword (most specific match)
        full_lower = short_desc_en.lower().strip()
        if full_lower not in keywords:
            keywords.insert(0, full_lower)
        # Add "HEV" variants as "hybrid" for better matching with Toyota.com.sa
        if "hybrid" in keywords and "hev" not in keywords:
            keywords.append("hev")
        if "hev" in keywords and "hybrid" not in keywords:
            keywords.append("hybrid")

        # Build engine string from description tokens
        engine_parts = []
        tokens = short_desc_en.split()
        for tok in tokens:
            tok_l = tok.lower()
            # Engine displacement (e.g., "2.4", "3.5")
            if re.match(r'^\d+\.\d+$', tok_l):
                engine_parts.append(tok)
            # Fuel/drivetrain abbreviations
            elif tok_l in ("dsl", "hev", "at", "mt", "4x4", "4x2", "v6", "v8"):
                engine_parts.append(tok.upper())
        engine_str = " ".join(engine_parts) if engine_parts else short_desc_en

        # Build Arabic name: model_ar + short_desc highlights
        brand_ar = BRAND_AR.get(str(brand_id), brand)
        name_ar = desc_ar if desc_ar else f"{brand_ar} {short_desc_en}"

        trim_entry = {
            "id": (mt.get("ModelCode") or "") or f"{model.lower()[:3]}{year % 100}-{short_desc_en[:20].replace(' ', '_').lower()}",
            "name": short_desc_en,
            "name_ar": name_ar,
            "msrp": 0,  # HJ API doesn't include prices
            "engine": engine_str,
            "keywords": keywords,
            "match_score": 0.88,
            "image": image,
        }
        trims.append(trim_entry)

    logger.info("Auto-trim discovery: found %d trims for %s %s %d from HJ catalog",
                len(trims), brand, model, year)

    # 6. Optionally enrich keywords with AI (cached 7 days)
    if anthropic_key and trims:
        for trim_entry in trims:
            trim_entry["keywords"] = await enrich_keywords_with_ai(
                brand, model, trim_entry["name"],
                trim_entry["keywords"], anthropic_key
            )

    return trims
