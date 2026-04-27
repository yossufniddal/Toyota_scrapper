"""
Shared state and configuration for HJ Motors Price Intelligence Backend.
All mutable state (sources_store, caches, etc.) lives here so modules can import it.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("hj-price-intel")
from pathlib import Path
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

# ── Windows fix: Playwright needs ProactorEventLoop ───────────
_pw_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="pw")

def _run_pw_sync(coro_fn, *args, **kwargs):
    """Run Playwright coroutine in a new ProactorEventLoop (thread-safe)"""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(*args, **kwargs))
    finally:
        loop.close()

async def pw_run(coro_fn, *args, **kwargs):
    """Run Playwright function in a separate thread with ProactorEventLoop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _pw_executor, lambda: _run_pw_sync(coro_fn, *args, **kwargs)
    )

# ── Playwright scrapers ───────────────────────────────────────
try:
    from scraper_playwright import (
        playwright_syarah, playwright_haraj,
        playwright_toyota_sa, playwright_lexus_sa, playwright_motory, playwright_yallamotor, PLAYWRIGHT_OK,
        run_playwright_safe, run_all_playwright_scrapers,
    )
except ImportError as e:
    PLAYWRIGHT_OK = False
    async def playwright_syarah(q, **kw): return []
    async def playwright_haraj(q, **kw): return []
    async def playwright_toyota_sa(m, **kw): return []
    async def playwright_lexus_sa(m, **kw): return []
    async def playwright_yallamotor(q, **kw): return []
    async def playwright_motory(q, **kw): return []
    async def run_playwright_safe(*a, **kw): return []
    async def run_all_playwright_scrapers(*a, **kw): return []

# ── Fast httpx scrapers ────────────────────────────────────────
try:
    from scraper_fast import run_fast_scrapers
    FAST_SCRAPERS_OK = True
except ImportError as e:
    FAST_SCRAPERS_OK = False
    logger.warning("scraper_fast not found: %s", e)
    async def run_fast_scrapers(brand, model, year, active_source_ids): return {}

# ── Optional: Redis ───────────────────────────────────────────
try:
    import redis.asyncio as aioredis
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = None
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
SCRAPE_TIMEOUT    = int(os.getenv("SCRAPE_TIMEOUT", "20"))
CACHE_TTL         = int(os.getenv("CACHE_TTL", "3600"))
SOURCES_FILE      = Path(os.getenv("SOURCES_FILE", "/app/sources.json"))
_mem_cache: dict  = {}

# ── Default Sources ───────────────────────────────────────────
DEFAULT_SOURCES = {
    "toyota.com.sa":  {"id":"toyota.com.sa",  "name":"Toyota.com.sa",  "name_ar":"تويوتا السعودية", "url":"https://www.toyota.com.sa",  "color":"#00C49A","enabled":True, "type":"playwright","priority":1,"search_url":"https://www.toyota.com.sa/en/models/{model}", "category":"official"},
    "lexus.com.sa":   {"id":"lexus.com.sa",   "name":"Lexus.com.sa",   "name_ar":"لكزس",            "url":"https://www.lexus.com.sa",    "color":"#00C49A","enabled":True, "type":"playwright","priority":2,"search_url":"https://www.lexus.com.sa/en/models/{model}", "category":"official"},
    "ksa.Motory.com":     {"id":"ksa.Motory.com",     "name":"ksa.Motory.com",  "name_ar":"موتوري",          "url":"https://ksa.Motory.com",          "color":"#f5a623","enabled":True, "type":"playwright","priority":3,"search_url":"https://ksa.Motory.com/sa/new-cars/search?q={query}", "category":"new_cars"},
    "haraj.com.sa":      {"id":"haraj.com.sa",      "name":"Haraj.com.sa",   "name_ar":"حراج",            "url":"https://haraj.com.sa",        "color":"#ff8055","enabled":True, "type":"playwright","priority":4,"search_url":"https://haraj.com.sa/search/{query}",            "category":"marketplace"},
    "ksa.yallamotor.com": {"id":"ksa.yallamotor.com", "name":"ksa.yallamotor.com",     "name_ar":"يلا موتور",       "url":"https://www.ksa.yallamotor.com",  "color":"#a78bfa","enabled":True, "type":"playwright",    "priority":5,"search_url":"https://ksa.yallamotor.com/ar/new-cars?query={query}&country=sa", "category":"new_cars"},
    "syarah.com":     {"id":"syarah.com",     "name":"Syarah.com",     "name_ar":"سيارة",           "url":"https://syarah.com",          "color":"#4da6ff","enabled":True, "type":"playwright","priority":6,"search_url":"https://syarah.com/filters?text={query}",        "category":"marketplace"},
}

def _load_sources() -> dict:
    if SOURCES_FILE.exists():
        try:
            saved = json.loads(SOURCES_FILE.read_text())
            merged = {**DEFAULT_SOURCES}
            for sid, data in saved.items():
                merged[sid] = {**merged.get(sid, {}), **data}
            return merged
        except Exception as e:
            logger.error("sources.json error: %s", e)
    return dict(DEFAULT_SOURCES)

def _save_sources(store):
    try:
        SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SOURCES_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error("save sources error: %s", e)

sources_store: dict = _load_sources()

# ── Pydantic Models ───────────────────────────────────────────
class SearchRequest(BaseModel):
    brand: str
    model: str
    year: int
    anthropic_key: str
    source_ids: Optional[list[str]] = None
    disableCache: bool = False
    new_only: bool = True
    claude_ai: bool = True
    # Post-search filters
    cities: Optional[list[str]] = None
    condition_filter: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    sort_by: Optional[str] = "price_asc"

class SourceUpdate(BaseModel):
    name: Optional[str] = None
    name_ar: Optional[str] = None
    url: Optional[str] = None
    search_url: Optional[str] = None
    enabled: Optional[bool] = None
    color: Optional[str] = None
    priority: Optional[int] = None

class NewSource(BaseModel):
    id: str
    name: str
    name_ar: str
    url: str
    search_url: str
    color: str = "#6B7280"
    type: str = "httpx"
    priority: int = 99

class TrimsRequest(BaseModel):
    brand: str
    model: str
    year: int
    anthropic_key: str = ""
    force_refresh: bool = False

class Brand(BaseModel):
    BrandID: str = None
    DescriptionAr: Optional[str] = None
    DescriptionEn: Optional[str] = None
    Description: Optional[str] = None

class Group(BaseModel):
    ListTreeGroups: str  = None
    brandID: str = None
    year: int = None
    DescriptionAr: str = None
    DescriptionEn: str = None
    Description: str = None
    productGroupID: str = None

class ModelType(BaseModel):
    ModelCode: str = None
    guid: str = None
    ProductTypeId: str = None
    Model: str = None
    descriptionAr: str = None
    descriptionEn: str = None
    Description: str = None
    productGroupID: str = None
    Image: Optional[str] = None

class CarCatalog(BaseModel):
    brands: list[Brand]
    groups: list[Group] = []
    modelTypes: list[ModelType] = []

# ── Trim Registry — dynamic cache ─────────────────────────────
_trim_cache: dict = {}   # key -> {"trims": [...], "ts": timestamp}
TRIM_CACHE_TTL = 86400   # 24 hours

class TrimInfo:
    """Represents one official trim with keywords for matching"""
    def __init__(self, name: str, name_ar: str, msrp: int, engine: str,
                 keywords: list[str], score: float = 0.85):
        self.name      = name
        self.name_ar   = name_ar
        self.msrp      = msrp
        self.engine    = engine
        self.keywords  = [k.lower() for k in keywords]
        self.score     = score

# ── Rate limit state ──────────────────────────────────────────
_rate_limit: dict[str, list[float]] = {}
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = 60

# ── Bundle Addons Config ──────────────────────────────────────
BUNDLE_ADDONS = {
    "عازل حراري نانو":          {"cost": 350,  "value": 1500, "label": "عازل حراري نانو سيراميك"},
    "تشميع بدن":                 {"cost": 150,  "value": 700,  "label": "تشميع لحماية البدن"},
    "حماية نوافذ السيارة PPF":  {"cost": 400,  "value": 1800, "label": "حماية PPF للنوافذ"},
    "صيانة أولى مجانية":        {"cost": 200,  "value": 800,  "label": "صيانة أولى مجانية"},
    "تأمين السنة الأولى":        {"cost": 800,  "value": 1800, "label": "تأمين شامل السنة الأولى"},
    "أطراف أبواب ومباخات":      {"cost": 80,   "value": 400,  "label": "حماية أطراف الأبواب"},
    "ريموت إضافي":               {"cost": 120,  "value": 500,  "label": "ريموت بديل إضافي"},
}

# ── Snapshot / DOM constants ──────────────────────────────────
SNAP_RETENTION_WEEKS = 13
SNAP_TTL = 60 * 60 * 24 * 7 * SNAP_RETENTION_WEEKS
DOM_TTL = 60 * 60 * 24 * 90

# ── City Normalization ────────────────────────────────────────
SAUDI_CITIES = {
    "riyadh": "الرياض", "الرياض": "الرياض", "riyad": "الرياض",
    "jeddah": "جدة", "جدة": "جدة", "jedda": "جدة", "jiddah": "جدة", "جده": "جدة",
    "dammam": "الدمام", "الدمام": "الدمام",
    "makkah": "مكة", "mecca": "مكة", "مكة": "مكة", "مكه": "مكة",
    "madinah": "المدينة", "medina": "المدينة", "المدينة": "المدينة", "المدينه": "المدينة",
    "khobar": "الخبر", "الخبر": "الخبر",
    "tabuk": "تبوك", "تبوك": "تبوك",
    "abha": "أبها", "أبها": "أبها",
    "jizan": "جيزان", "جيزان": "جيزان",
    "hail": "حائل", "حائل": "حائل",
    "qassim": "القصيم", "القصيم": "القصيم", "buraydah": "القصيم", "بريدة": "القصيم",
    "najran": "نجران", "نجران": "نجران",
    "taif": "الطائف", "الطائف": "الطائف", "الطايف": "الطائف",
    "yanbu": "ينبع", "ينبع": "ينبع",
    "jubail": "الجبيل", "الجبيل": "الجبيل",
    "dhahran": "الظهران", "الظهران": "الظهران",
    "khamis mushait": "خميس مشيط", "خميس مشيط": "خميس مشيط",
    "حفر الباطن": "حفر الباطن", "hafar al batin": "حفر الباطن",
    "الشرقية": "المنطقة الشرقية", "eastern": "المنطقة الشرقية",
    "المملكة العربية السعودية": "غير محدد", "السعودية": "غير محدد", "saudi": "غير محدد",
}

def normalize_city(raw_location: str) -> str:
    if not raw_location:
        return "غير محدد"
    raw = raw_location.strip().lower()
    if raw in SAUDI_CITIES:
        return SAUDI_CITIES[raw]
    for key, city in SAUDI_CITIES.items():
        if key in raw or city in raw_location:
            return city
    return "غير محدد"
