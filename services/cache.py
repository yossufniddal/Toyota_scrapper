"""
Redis / in-memory cache functions, price snapshots, and days-on-market tracking.
"""
import hashlib
import json
import logging
import time

logger = logging.getLogger("hj-price-intel.cache")
from datetime import date, timedelta

from shared import (
    REDIS_AVAILABLE, CACHE_TTL, _mem_cache,
    SNAP_TTL, DOM_TTL,
)

# ── Redis connection ──────────────────────────────────────────
# After a failed connect we cache the failure for REDIS_RETRY_COOLDOWN seconds.
# Without this, every cache_get/cache_set/snapshot_save/dom_record call re-tries
# the connection and blocks ~4s on TCP timeout — adding minutes per search when
# Redis is offline.
_redis_client = None
_redis_unavailable_until: float = 0.0
REDIS_RETRY_COOLDOWN = 60  # seconds


async def _get_redis():
    global _redis_client, _redis_unavailable_until
    if not REDIS_AVAILABLE:
        return None
    if _redis_client is not None:
        return _redis_client
    if time.time() < _redis_unavailable_until:
        return None
    try:
        import redis.asyncio as aioredis
        from shared import REDIS_URL
        client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
        await client.ping()
        _redis_client = client
        return _redis_client
    except Exception as e:
        logger.warning("Redis unavailable, suppressing retries for %ds: %s",
                       REDIS_RETRY_COOLDOWN, e)
        _redis_unavailable_until = time.time() + REDIS_RETRY_COOLDOWN
        _redis_client = None
        return None

def _mark_redis_broken(reason: str):
    """Drop the cached client and start the cooldown so the next call short-circuits."""
    global _redis_client, _redis_unavailable_until
    _redis_client = None
    _redis_unavailable_until = time.time() + REDIS_RETRY_COOLDOWN
    logger.warning("Redis marked unavailable for %ds: %s", REDIS_RETRY_COOLDOWN, reason)


async def cache_get(key):
    r = await _get_redis()
    if r:
        try:
            val = await r.get(f"hj:{key}")
            if val:
                return {**json.loads(val), "fromCache": True}
        except Exception as e:
            _mark_redis_broken(f"cache_get '{key}': {e}")
    if key in _mem_cache:
        ts, data = _mem_cache[key]
        if time.time() - ts < CACHE_TTL:
            return {**data, "fromCache": True, "cacheAge": int(time.time() - ts)}
    return None

async def cache_set(key, data, ttl=CACHE_TTL):
    r = await _get_redis()
    if r:
        try:
            value = json.dumps(data, ensure_ascii=False)
            redis_key = f"hj:{key}"
            if ttl is None:
                await r.set(redis_key, value)
            else:
                await r.setex(redis_key, ttl, value)
        except Exception as e:
            _mark_redis_broken(f"cache_set '{key}': {e}")
    _mem_cache[key] = (time.time(), data)

async def cache_clear():
    r = await _get_redis()
    if r:
        try:
            keys = await r.keys("hj:*")
            if keys:
                await r.delete(*keys)
        except Exception as e:
            logger.warning("Redis cache_clear error: %s", e)
    _mem_cache.clear()

# ──────────────────────────────────────────────────────────────
#  PRICE TREND — weekly snapshots stored in Redis
# ──────────────────────────────────────────────────────────────

def _week_key() -> str:
    d = date.today().isocalendar()
    return f"{d[0]}-{d[1]:02d}"

def _prev_week_key(n: int = 1) -> str:
    d = (date.today() - timedelta(weeks=n)).isocalendar()
    return f"{d[0]}-{d[1]:02d}"

async def snapshot_save(brand: str, model: str, year: int, trim_name: str,
                         min_p: int, avg_p: int, max_p: int, count: int):
    r = await _get_redis()
    if not r:
        return
    wk  = _week_key()
    key = f"hj:snap:{brand.lower()}:{model.lower()}:{year}:{trim_name.lower().replace(' ','_')}:{wk}"
    payload = json.dumps({"min": min_p, "avg": avg_p, "max": max_p,
                           "count": count, "week": wk, "saved_at": int(time.time())},
                          ensure_ascii=False)
    try:
        await r.setex(key, SNAP_TTL, payload)
    except Exception as e:
        _mark_redis_broken(f"snapshot_save: {e}")

async def snapshot_get_history(brand: str, model: str, year: int,
                                trim_name: str, weeks: int = 6) -> list[dict]:
    r = await _get_redis()
    if not r:
        return []
    results = []
    slug = trim_name.lower().replace(" ", "_")
    prefix = f"hj:snap:{brand.lower()}:{model.lower()}:{year}:{slug}:"
    for n in range(weeks):
        wk  = _prev_week_key(n)
        key = f"{prefix}{wk}"
        try:
            raw = await r.get(key)
            if raw:
                results.append(json.loads(raw))
        except Exception as e:
            _mark_redis_broken(f"snapshot_get_history '{key}': {e}")
            break
    return results

async def snapshot_calc_trend(history: list[dict]) -> dict:
    if len(history) < 2:
        return {"direction": "stable", "change_pct": 0.0,
                "change_sar": 0, "weeks_of_data": len(history), "label": "بيانات غير كافية"}
    latest   = history[0]["avg"]
    compare  = history[min(3, len(history)-1)]["avg"]
    if compare == 0:
        return {"direction": "stable", "change_pct": 0.0,
                "change_sar": 0, "weeks_of_data": len(history), "label": "بيانات غير كافية"}
    change_sar = latest - compare
    change_pct = round(change_sar / compare * 100, 1)
    if change_pct > 1.5:
        direction, label = "up",   f"↑ ارتفع {abs(change_pct)}% خلال 4 أسابيع"
    elif change_pct < -1.5:
        direction, label = "down", f"↓ انخفض {abs(change_pct)}% خلال 4 أسابيع"
    else:
        direction, label = "stable", "→ مستقر خلال 4 أسابيع"
    return {"direction": direction, "change_pct": change_pct,
            "change_sar": change_sar, "weeks_of_data": len(history), "label": label}

# ──────────────────────────────────────────────────────────────
#  DAYS ON MARKET
# ──────────────────────────────────────────────────────────────

async def dom_record(source: str, url: str, trim_name: str):
    r = await _get_redis()
    if not r or not url:
        return
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    key = f"hj:dom:{source}:{url_hash}"
    try:
        exists = await r.exists(key)
        if not exists:
            await r.setex(key, DOM_TTL, json.dumps({
                "first_seen": int(time.time()),
                "url": url,
                "trim": trim_name,
                "source": source
            }, ensure_ascii=False))
    except Exception as e:
        _mark_redis_broken(f"dom_record: {e}")

async def dom_get_age_days(source: str, url: str) -> int | None:
    r = await _get_redis()
    if not r or not url:
        return None
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    key = f"hj:dom:{source}:{url_hash}"
    try:
        raw = await r.get(key)
        if raw:
            data = json.loads(raw)
            return max(0, int((time.time() - data["first_seen"]) / 86400))
    except Exception as e:
        _mark_redis_broken(f"dom_get_age_days: {e}")
    return None

async def dom_avg_for_trim(brand: str, model: str, trim_name: str,
                            source: str = "") -> dict:
    r = await _get_redis()
    if not r:
        return {"avg": None, "min": None, "max": None, "count": 0}
    pattern = f"hj:dom:{source}:*" if source else "hj:dom:*:*"
    ages = []
    try:
        keys = await r.keys(pattern)
        for k in keys[:200]:
            raw = await r.get(k)
            if not raw:
                continue
            d = json.loads(raw)
            if trim_name.lower() in d.get("trim", "").lower():
                age = max(0, int((time.time() - d["first_seen"]) / 86400))
                ages.append(age)
    except Exception as e:
        _mark_redis_broken(f"dom_avg_for_trim: {e}")
    if not ages:
        return {"avg": None, "min": None, "max": None, "count": 0}
    return {
        "avg":   round(sum(ages) / len(ages), 1),
        "min":   min(ages),
        "max":   max(ages),
        "count": len(ages),
        "label": f"متوسط {round(sum(ages)/len(ages),0):.0f} يوم في السوق"
    }
