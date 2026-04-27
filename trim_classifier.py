"""
HJ Motors — Trim Classifier Engine
====================================
Classifies listings collected from sources into proper trims.

Original problem:
  searchURLAndAI dumped all listings into a single bucket.
  Example: Camry E (105k) and Camry Lumiere (149k) ended up in the same trim.

Fix:
  1. Smart multi-stage matching (keywords → name → price proximity)
  2. Clear bucketing per trim with confidence scoring
  3. Reject implausible listings + dedup
  4. "Unknown" bucket for ambiguous listings
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from collections import defaultdict
from statistics import median, stdev

# ── Import trims_config ──────────────────────────────────────
from trims_config import get_trims, TrimDefinition

logger = logging.getLogger("hj-price-intel.trim_classifier")


# ──────────────────────────────────────────────────────────────
#  1. TRIM MATCHER
# ──────────────────────────────────────────────────────────────

@dataclass
class TrimMatch:
    """Result of matching a single listing against a trim"""
    trim_name: str
    trim_name_ar: str
    trim_id: str
    msrp: int
    engine: str
    confidence: float          # 0.0 – 1.0
    match_method: str          # "keyword" | "name" | "price_proximity" | "fallback"
    match_detail: str          # human-readable detail

def normalize(text):
    return text.lower().strip()


# Words that appear in HJ-catalog trim names ("toyota camry e 2.5 sedan 4x2
# petrol automatic") but are spec descriptors, not the distinctive trim
# identifier. Listing titles in marketplaces rarely repeat them, so they
# shouldn't be counted as "misses" when scoring word_overlap.
_SPEC_STOP_WORDS = frozenset({
    # body / drive
    "sedan", "suv", "coupe", "hatchback", "wagon", "pickup", "pick-up",
    "4x2", "4x4", "2wd", "4wd", "awd",
    # engine size (covers 1.5 .. 6.4)
    "1.5", "1.6", "1.8", "2.0", "2.4", "2.5", "2.7", "2.8", "3.0",
    "3.5", "4.0", "5.0", "5.7", "6.0", "6.4",
    # fuel
    "petrol", "gasoline", "diesel", "hybrid", "electric", "ev", "phev", "hev",
    # transmission
    "automatic", "manual", "cvt", "dct", "amt", "at",
    # other padding
    "new", "used", "model", "edition",
})


# Spec terms used as DISAMBIGUATORS when scoring trim matches. These are in
# _SPEC_STOP_WORDS (so word_overlap ignores them — they appear in every long
# trim name), but when BOTH the listing title AND a trim name specify a value
# from one of these groups, that match/conflict is meaningful: e.g. a Toyota
# listing tagged "E PLUS HEV" should bucket under the HJ trim
# "...petrol hybrid", not under the "...petrol automatic" sibling.
_FUEL_TERMS  = frozenset({"petrol", "gasoline", "diesel", "hybrid",
                           "electric", "ev", "phev", "hev"})
_TRANS_TERMS = frozenset({"automatic", "manual", "cvt", "dct", "amt", "at"})
_DRIVE_TERMS = frozenset({"4x2", "4x4", "2wd", "4wd", "awd"})


def _spec_terms_in(text: str, vocab: frozenset) -> set:
    """Return the subset of ``vocab`` that appears as whole words in ``text``."""
    if not text:
        return set()
    words = set(re.findall(r"[\w-]+", text.lower()))
    return {t for t in vocab if t in words}


def classify_listing(listing: dict, brand: str, model: str, year: int,
                     dynamic_trims: list = None) -> TrimMatch:

    title = (listing.get("listedAs") or "").lower().strip()
    # Normalize common abbreviations in title for better matching
    title = title.replace(" hev", " hybrid").replace(" dsl", " diesel").replace(" at", " automatic")
    price = listing.get("price", 0)

    trims = _build_trim_list(brand, model, year, dynamic_trims)

    # ── If no trims ─────────────────────────────────────
    if not trims:
        return TrimMatch(
            trim_name=f"{model} Standard",
            trim_name_ar=f"{model} ستاندرد",
            trim_id="fallback",
            msrp=0,
            engine="",
            confidence=0.35,
            match_method="fallback_no_trims",
            match_detail="No trims available — fallback to default"
        )

    scored_trims = []

    # ── Score each trim ────────────────────────────────
    for trim in trims:
        score = 0.0
        reasons = []

        # 1. Keyword Match — word-boundary matching, longer keywords first
        keywords = sorted(
            [normalize(k) for k in trim["keywords"] if k and len(k) >= 2],
            key=len, reverse=True
        )
        title_words = set(title.split())
        for kw in keywords:
            kw_words = kw.split()
            # Check if ALL words of the keyword exist as whole words in the title
            if all(w in title_words for w in kw_words):
                kw_bonus = min(0.15, len(kw_words) * 0.05)
                score += 0.9 + kw_bonus
                reasons.append(f"keyword:{kw}")
                break

        # 2. Name Match (full name match)
        if trim["name"].lower() in title or trim["name_ar"].lower() in title:
            score += 0.8
            reasons.append("name_match")

        # 3. Word Overlap — only count distinctive words (drop spec stop-words
        # and the brand/model name itself, which appear in every trim).
        all_words = trim["name"].lower().split()
        brand_model_words = {w for w in (brand or "").lower().split()} | \
                            {w for w in (model or "").lower().split() if len(w) >= 3}
        distinctive = [w for w in all_words
                       if w not in _SPEC_STOP_WORDS and w not in brand_model_words]
        if distinctive:
            hits = sum(1 for w in distinctive if w in title_words)
            overlap = hits / len(distinctive)

            # Penalty only for missing distinctive words. Lighter than before
            # because we already pruned spec descriptors.
            misses = len(distinctive) - hits
            penalty = misses * 0.10
            net_overlap_score = max(0.0, (overlap * 0.7) - penalty)

            if overlap > 0:
                score += net_overlap_score
                reasons.append(
                    f"overlap:{overlap:.2f}/{len(distinctive)} penalty:{penalty:.2f}")

        # 4. Price Proximity — widened to 35% to keep promotional discounts
        # and dealer markups attached to the right trim.
        if price > 0 and trim["msrp"] > 0:
            diff = abs(trim["msrp"] - price) / trim["msrp"]
            if diff <= 0.35:
                price_score = (1 - diff) * 0.7
                score += price_score
                reasons.append(f"price:{diff:.2f}")

        # 5. Spec disambiguators (fuel / transmission / drive)
        # When the HJ catalog has BOTH "...petrol hybrid" and "...petrol
        # automatic" variants of the same grade, keyword + name + overlap
        # can score them identically — leaving the classifier to pick
        # whichever was added first and bucketing Toyota's "E PLUS HEV"
        # listing under the wrong variant. This step uses fuel/trans/drive
        # terms in title vs trim name to break the tie:
        #   - both sides specify a term and they MATCH      → small bonus
        #   - both sides specify a term and they CONFLICT   → larger penalty
        #   - one side doesn't specify                       → no effect
        trim_text = trim["name"].lower() + " " + " ".join(trim.get("keywords") or [])
        for vocab, bonus, penalty, label in (
            (_FUEL_TERMS,  0.20, 0.30, "fuel"),
            (_TRANS_TERMS, 0.10, 0.15, "trans"),
            (_DRIVE_TERMS, 0.10, 0.15, "drive"),
        ):
            t_terms = _spec_terms_in(title, vocab)
            r_terms = _spec_terms_in(trim_text, vocab)
            if not t_terms or not r_terms:
                continue
            if t_terms & r_terms:
                score += bonus
                reasons.append(f"{label}_match:{sorted(t_terms & r_terms)}")
            else:
                score -= penalty
                reasons.append(
                    f"{label}_conflict:title={sorted(t_terms)} trim={sorted(r_terms)}")

        # 6. Small bias (avoid zeros)
        score += 0.05

        scored_trims.append({
            "trim": trim,
            "score": score,
            "reasons": reasons
        })

    # ── Pick best ───────────────────────────────────────
    best = max(scored_trims, key=lambda x: x["score"])

    best_trim = best["trim"]
    best_score = best["score"]

    # ── Smart confidence ────────────────────────────────
    confidence = min(0.95, max(0.35, best_score))

    # ── Determine match type ────────────────────────────
    if any("keyword" in r for r in best["reasons"]):
        method = "keyword"
    elif "name_match" in best["reasons"]:
        method = "name"
    elif any("overlap" in r for r in best["reasons"]):
        method = "word_overlap"
    elif any("price" in r for r in best["reasons"]):
        method = "price_proximity"
    else:
        method = "fallback_best_match"

    return TrimMatch(
        trim_name=best_trim["name"],
        trim_name_ar=best_trim["name_ar"],
        trim_id=best_trim["id"],
        msrp=best_trim["msrp"],
        engine=best_trim["engine"],
        confidence=confidence,
        match_method=method,
        match_detail=" | ".join(best["reasons"]) if best["reasons"] else "default scoring"
    )

def _build_trim_list(brand: str, model: str, year: int,
                     dynamic_trims: list = None) -> list[dict]:
    """Merge configured trims (trims_config) + dynamic trims (HJ catalog / AI)
    into a single canonical list.

    Dedup is exact-name match. We considered fancier substring dedup so that
    a configured 'TXL-3' would merge with a dynamic 'prado 2.4 suv petrol
    automatic 4x4 txl-3', but every heuristic I tried risked merging real
    distinct trims (e.g. 'LE' with 'LE Hybrid'). Losing those distinctions
    hides listings, which is worse than having a couple of cosmetically
    duplicated empty buckets.
    """
    result: list[dict] = []
    seen_names = set()

    def _g(obj, key, default=""):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # Priority 1: configured trims (manually curated — cleaner names, real MSRPs).
    for t in get_trims(brand, model, year):
        name_l = (t.name or "").lower()
        if not name_l or name_l in seen_names:
            continue
        result.append({
            "id": t.id, "name": name_l, "name_ar": t.name_ar,
            "msrp": t.msrp, "engine": t.engine,
            "keywords": [k.lower() for k in t.keywords],
            "score": t.match_score,
        })
        seen_names.add(name_l)

    # Priority 2: dynamic trims (HJ catalog / AI).
    if dynamic_trims:
        for dt in dynamic_trims:
            name = _g(dt, "name", "")
            name_l = (name or "").lower()
            if not name_l or name_l in seen_names:
                continue
            result.append({
                "id": _g(dt, "id", ""),
                "name": name_l,
                "name_ar": _g(dt, "name_ar", name),
                "msrp": _g(dt, "msrp", 0),
                "engine": _g(dt, "engine", ""),
                "keywords": [k.lower() for k in (_g(dt, "keywords", []) or [])],
                "score": _g(dt, "score", 0.80),
            })
            seen_names.add(name_l)

    return result


# ──────────────────────────────────────────────────────────────
#  2. DEDUPLICATION
# ──────────────────────────────────────────────────────────────

def deduplicate_listings(listings: list[dict]) -> list[dict]:
    """
    Removes duplicate listings based on:
      1. Same URL
      2. Same price + same title (approximately)
      3. Same price + same source + same seller
    Keeps the highest-confidence listing.
    """
    seen_urls = set()
    seen_fingerprints = set()
    unique = []

    for l in listings:
        url = l.get("url", "")
        price = l.get("price", 0)
        source = l.get("source", "")
        title = _normalize_title(l.get("listedAs", ""))
        seller = l.get("sellerName", "")

        # Filter 1: duplicate URL (skip official sources — their trims share URLs)
        is_official = source in ("toyota.com.sa", "lexus.com.sa")
        if url and url in seen_urls and not is_official:
            continue

        # Filter 2: fingerprint (price + short title + source)
        fp = f"{price}:{title[:30]}:{source}"
        if fp in seen_fingerprints:
            continue

        # Filter 3: same price + same source + same seller
        fp2 = f"{price}:{source}:{seller}"
        if seller and fp2 in seen_fingerprints:
            continue

        if url:
            seen_urls.add(url)
        seen_fingerprints.add(fp)
        if seller:
            seen_fingerprints.add(fp2)
        unique.append(l)

    return unique


def _normalize_title(title: str) -> str:
    """Normalize title for comparison"""
    return re.sub(r"\s+", " ", (title or "").lower().strip())


# ──────────────────────────────────────────────────────────────
#  3. OUTLIER REJECTION
# ──────────────────────────────────────────────────────────────

def reject_outliers(listings: list[dict], msrp: int = 0,
                    low_pct: float = 0.70, high_pct: float = 1.35) -> list[dict]:
    """
    Rejects out-of-range prices.

    If MSRP known: accept MSRP × low_pct → MSRP × high_pct
    If MSRP=0: use IQR (interquartile range) to remove outliers
    """
    if not listings:
        return []

    prices = [l["price"] for l in listings if l.get("price", 0) > 0]
    if not prices:
        return listings

    if msrp and msrp > 0:
        lo = int(msrp * low_pct)
        hi = int(msrp * high_pct)
        return [l for l in listings if lo <= l.get("price", 0) <= hi or l.get("price", 0) == 0]

    # IQR-based outlier detection
    if len(prices) < 4:
        return listings  # not enough for stats

    sorted_p = sorted(prices)
    q1 = sorted_p[len(sorted_p) // 4]
    q3 = sorted_p[3 * len(sorted_p) // 4]
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr

    # Never reject official source listings (their prices are exact)
    official_sources = ("toyota.com.sa", "lexus.com.sa")
    return [l for l in listings
            if l.get("source", "") in official_sources
            or lo <= l.get("price", 0) <= hi
            or l.get("price", 0) == 0]


# ──────────────────────────────────────────────────────────────
#  4. TRIM BUCKETING
# ──────────────────────────────────────────────────────────────
def bucket_listings_by_trim(listings: list[dict], brand: str, model: str, year: int,
                            dynamic_trims: list = None) -> dict:
    """
    Distributes listings across buckets by trim.
    Pre-creates a bucket for every trim from get_trims — even if no listings.
    """
    # ── 1. Create empty buckets for every configured trim ──────
    # All bucket keys are lowercase so they match what classify_listing
    # returns (which lowercases via _build_trim_list). Without this, the
    # configured trim "Grande" and a classified "grande" became two separate
    # buckets — the configured one stayed empty and the user saw duplicates.
    configured_trims = get_trims(brand, model, year)
    buckets: dict[str, dict] = {}

    for t in configured_trims:
        key = (t.name or "").lower()
        if not key:
            continue
        buckets[key] = {
            "trim_info": {
                "name": key,
                "name_ar": t.name_ar,
                "id": t.id,
                "msrp": t.msrp,
                "engine": t.engine,
            },
            "listings": [],
            "match_stats": {},
        }

    # ── 1b. Empty buckets for every dynamic_trim (HJ catalog) ──
    if dynamic_trims:
        def _dg(obj, key, default=""):
            return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
        for dt in dynamic_trims:
            name = _dg(dt, "name", "")
            if not name or name.lower() in {k.lower() for k in buckets}:
                continue
            buckets[name.lower()] = {
                "trim_info": {
                    "name": name.lower(),
                    "name_ar": _dg(dt, "name_ar", name),
                    "id": _dg(dt, "id", ""),
                    "msrp": _dg(dt, "msrp", 0),
                    "engine": _dg(dt, "engine", ""),
                },
                "listings": [],
                "match_stats": {},
            }

    match_stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    # ── 2. Classify each listing and append to matching bucket ─
    for listing in listings:
        match = classify_listing(listing, brand, model, year, dynamic_trims)
        listing["matchedTrim"]     = match.trim_name
        listing["matchReason"]     = match.match_detail
        listing["matchConfidence"] = (
            "high"   if match.confidence > 0.85 else
            "medium" if match.confidence > 0.60 else
            "low"
        )
        listing["matchMethod"] = match.match_method

        trim_key = match.trim_name

        # ── If trim came from dynamic_trims and not in config buckets ──
        if trim_key not in buckets:
            buckets[trim_key] = {
                "trim_info": {
                    "name": match.trim_name,
                    "name_ar": match.trim_name_ar,
                    "id": match.trim_id,
                    "msrp": match.msrp,
                    "engine": match.engine,
                },
                "listings": [],
                "match_stats": {},
            }

        buckets[trim_key]["listings"].append(listing)
        match_stats[trim_key][match.match_method] += 1

    # ── 3. Add match stats ──────────────────────────────────────
    for key in buckets:
        buckets[key]["match_stats"] = dict(match_stats.get(key, {}))

    return buckets

def build_trims_response(buckets: dict, brand: str, model: str, year: int) -> list[dict]:
    """
    Converts buckets into the API's expected shape:
      trims: [{ officialName, officialMSRP, listings, priceAnalysis, ... }]

    With:
      - Preserve every trim from get_trims even if empty
      - Dedupe within each trim
      - Reject outliers based on MSRP
      - Compute priceAnalysis
    """
    trims_out = []

    # ── 1. Start with all known trims (configured + dynamic) ──
    configured_trims = get_trims(brand, model, year)
    configured_names = [t.name for t in configured_trims]

    # All known trims (configured + dynamic) except "Unknown"
    known_keys = sorted(
        [k for k in buckets.keys() if k.lower() != "unknown"],
        key=lambda k: buckets[k]["trim_info"].get("msrp", 0) or 0,
        reverse=True
    )

    # Unknown last
    extra_keys = [k for k in buckets.keys() if k.lower() == "unknown"]

    sorted_keys = known_keys + extra_keys

    # ── 2. Compute price range across known trims ─────────────
    known_msrps = [
        buckets[k]["trim_info"]["msrp"]
        for k in known_keys
        if buckets[k]["trim_info"].get("msrp", 0) > 0
    ]
    model_price_floor = int(min(known_msrps) * 0.70) if known_msrps else 0
    model_price_ceil  = int(max(known_msrps) * 1.35) if known_msrps else 0

    for trim_key in sorted_keys:
        bucket = buckets[trim_key]
        info   = bucket["trim_info"]
        listings = bucket["listings"]

        # ── Dedup ──────────────────────────────────────────────
        before_dedup = len(listings)
        listings = deduplicate_listings(listings)
        after_dedup = len(listings)

        # ── Reject outliers ────────────────────────────────────
        listings = reject_outliers(listings, info.get("msrp", 0))
        after_outlier = len(listings)
        if before_dedup != after_outlier:
            logger.debug("[%s] %d -> dedup:%d -> outlier:%d",
                         trim_key, before_dedup, after_dedup, after_outlier)

        # ── Extra filter for Unknown ───────────────────────────
        if trim_key == "Unknown" and model_price_floor > 0:
            listings = [
                l for l in listings
                if model_price_floor <= l.get("price", 0) <= model_price_ceil
                or l.get("price", 0) == 0
            ]

        # ── Compute priceAnalysis ──────────────────────────────
        prices = [l["price"] for l in listings if l.get("price", 0) > 0]
        msrp   = info.get("msrp", 0)

        if prices:
            market_avg = int(sum(prices) / len(prices))
            vs_pct     = round((market_avg - msrp) / msrp * 100, 1) if msrp else 0
        else:
            market_avg = 0
            vs_pct     = 0

        trim_obj = {
            "officialName":   info["name"],
            "officialNameAr": info.get("name_ar", info["name"]),
            "officialMSRP":   msrp,
            "engine":         info.get("engine", ""),
            "commonAliases":  [],
            "listings":       listings,
            "priceAnalysis": {
                "marketMin":    min(prices) if prices else 0,
                "marketMax":    max(prices) if prices else 0,
                "marketAvg":    market_avg,
                "vsOfficialPct": vs_pct,
                "trend":        "stable",
                "listingCount": len(prices),
            },
            "matchStats": bucket.get("match_stats", {}),
        }

        trims_out.append(trim_obj)

    return trims_out
# ──────────────────────────────────────────────────────────────
#  5. MAIN ENTRY
# ──────────────────────────────────────────────────────────────

def classify_and_structure(raw_listings: list[dict],
                           brand: str, model: str, year: int,
                           dynamic_trims: list = None,
                           ai_data: dict = None) -> dict:
    """
    Main entry: takes raw listings, returns a fully structured JSON.

    Logic:
      1. Classify each listing by trim
      2. Dedupe duplicates
      3. Reject outliers
      4. Merge with AI data if present
      5. Return final shape

    Parameters:
      raw_listings: listings from scrapers
      brand, model, year: car identifiers
      dynamic_trims: trims from AI or scrape (optional)
      ai_data: AI fallback data (optional, merged in)
    """
    # ── Classify and bucket ────────────────────────────────
    buckets = bucket_listings_by_trim(raw_listings, brand, model, year, dynamic_trims)
    # ── Merge with AI (if present) ──────────────────────────
    if ai_data and ai_data.get("trims"):
        _merge_ai_trims(buckets, ai_data, brand, model, year)

    # ── Build final response ────────────────────────────────
    trims = build_trims_response(buckets, brand, model, year)

    # ── Demote low-confidence listings to "Other" ───────────
    # Demotion rules (kept conservative — better to show a slightly mis-bucketed
    # listing under its best-guess trim than to hide it in "Other"):
    #   - Never demote listings from official sources (toyota.com.sa / lexus.com.sa).
    #     Their prices ARE the canonical prices; if they didn't bucket cleanly
    #     it means the trim name is just spelled differently.
    #   - Never demote when the trim already has nothing else in it (otherwise
    #     we end up with a fully-empty trim card on the UI).
    #   - Demote only listings that scored "low" via the fallback method, not
    #     ones that hit a keyword or a name match (those are deliberate matches).
    other_listings = []
    for trim in trims:
        kept = []
        for listing in trim["listings"]:
            conf = listing.get("matchConfidence", "low")
            method = listing.get("matchMethod", "")
            src = listing.get("source", "")
            is_official = src in ("toyota.com.sa", "lexus.com.sa")
            should_demote = (
                conf == "low"
                and not is_official
                and method.startswith("fallback")
                and trim["officialName"] != "أخرى"
            )
            if should_demote:
                other_listings.append(listing)
            else:
                kept.append(listing)
        trim["listings"] = kept
        # Update price analysis
        valid_prices = [l["price"] for l in kept if l.get("price", 0) > 0]
        trim["priceAnalysis"]["listingCount"] = len(valid_prices)
        if valid_prices:
            trim["priceAnalysis"]["marketMin"] = min(valid_prices)
            trim["priceAnalysis"]["marketMax"] = max(valid_prices)
            trim["priceAnalysis"]["marketAvg"] = int(sum(valid_prices) / len(valid_prices))

    if other_listings:
        other_prices = [l["price"] for l in other_listings if l.get("price", 0) > 0]
        trims.append({
            "officialName": "أخرى",
            "officialNameAr": "أخرى",
            "officialMSRP": 0,
            "engine": "",
            "commonAliases": [],
            "listings": other_listings,
            "priceAnalysis": {
                "marketMin": min(other_prices) if other_prices else 0,
                "marketMax": max(other_prices) if other_prices else 0,
                "marketAvg": int(sum(other_prices) / len(other_prices)) if other_prices else 0,
                "vsOfficialPct": 0,
                "trend": "stable",
                "listingCount": len(other_prices),
            },
            "matchStats": {"keyword": 0, "name": 0, "price_proximity": 0, "word_overlap": 0, "fallback": len(other_listings), "ai_fallback": 0},
        })

    # ── MSRP fallback ────────────────────────────────────────
    # For trims with a known MSRP but no ACTUAL listing from the official
    # source, inject a synthetic "Official Price" entry so the comparison
    # matrix always shows the canonical price for every known trim.
    # Marketplaces only have what's currently for sale, so most cars will
    # have many trims that no one is selling — without this, the matrix
    # looks half-empty even though we DO know the official price.
    #
    # Only Toyota and Lexus have a configured official-source column in
    # our matrix. For other brands the MSRP is still exposed at
    # trim.officialMSRP for the UI to display, but we don't fabricate
    # a fake Toyota.com.sa entry.
    brand_l = (brand or "").lower()
    OFFICIAL_BY_BRAND = {
        "toyota": ("toyota.com.sa", "Toyota.com.sa"),
        "lexus":  ("lexus.com.sa",  "Lexus.com.sa"),
    }
    official_source_id, official_source_name = OFFICIAL_BY_BRAND.get(
        brand_l, (None, None))
    # Other brands: MSRP is still exposed at trim.officialMSRP for the UI;
    # we just don't fabricate a fake Toyota.com.sa / Lexus.com.sa entry.
    if official_source_id is not None:
        for trim in trims:
            msrp = trim.get("officialMSRP", 0)
            if not msrp or trim.get("officialName") == "أخرى":
                continue
            # Skip if a real listing from the official source already exists.
            already_official = any(
                l.get("source") == official_source_id
                for l in trim.get("listings", [])
            )
            if already_official:
                continue
            synthetic = {
                "source": official_source_id,
                "sourceName": official_source_name,
                "listedAs": trim.get("officialName", "") or trim.get("officialNameAr", ""),
                "condition": "جديدة",
                "price": int(msrp),
                "mileage": "0 كم",
                "location": "المملكة العربية السعودية",
                "city": "غير محدد",
                "sellerName": "الوكيل المعتمد",
                "sellerType": "official_dealer",
                "postedDaysAgo": 0,
                "imageUrl": "",
                "url": f"https://www.{official_source_id}",
                "priceNote": "السعر الرسمي — لا يوجد إعلان حي حالياً",
                "matchConfidence": "high",
                "matchReason": "Official MSRP fallback — no live listing found",
                "matchMethod": "official_msrp_fallback",
                "matchedTrim": trim.get("officialName", ""),
            }
            trim.setdefault("listings", []).append(synthetic)
            # Keep priceAnalysis aligned so the UI renders the price summary.
            valid_prices = [l["price"] for l in trim["listings"] if l.get("price", 0) > 0]
            if valid_prices:
                trim["priceAnalysis"] = {
                    **trim.get("priceAnalysis", {}),
                    "marketMin": min(valid_prices),
                    "marketMax": max(valid_prices),
                    "marketAvg": int(sum(valid_prices) / len(valid_prices)),
                    "listingCount": len(valid_prices),
                }

    # ── Build final shape ────────────────────────────────────
    all_prices = []
    for t in trims:
        all_prices.extend(l["price"] for l in t["listings"] if l.get("price", 0) > 0)

    result = {
        "vehicle": f"{year} {brand} {model}",
        "brand": brand, "model": model, "year": year,
        "officialPriceRange": {
            "min": min(all_prices) if all_prices else 0,
            "max": max(all_prices) if all_prices else 0,
        },
        "trims": trims,
    }

    # ── Copy extra AI fields (if present) ────────────────────
    if ai_data:
        for key in ("marketInsight", "priceHistory", "competitorAnalysis"):
            if key in ai_data:
                result[key] = ai_data[key]

    return result


def _merge_ai_trims(buckets: dict, ai_data: dict, brand: str, model: str, year: int):
    """
    Merge AI listings into existing buckets using the same classify_listing path.
    Each AI listing flows through the smart-classification (keyword/name/overlap/price)
    so it gets distributed across the configured trims — exactly like live listings.
    """
    # ── 1. Collect all AI listings across all trims ────────
    all_ai_listings = []
    for ai_trim in ai_data.get("trims", []):
        ai_name = ai_trim.get("officialName", "")
        ai_msrp = ai_trim.get("officialMSRP", 0)
        for ai_listing in ai_trim.get("listings", []):
            ai_listing["_ai_original_trim"] = ai_name
            ai_listing["_ai_original_msrp"] = ai_msrp
            all_ai_listings.append(ai_listing)

    if not all_ai_listings:
        return

    # ── 2. Build fingerprints of existing listings to dedup ─
    existing_fps = set()
    for bucket in buckets.values():
        for l in bucket["listings"]:
            fp = f"{l.get('price', 0)}:{l.get('source', '')}:{l.get('listedAs', '')[:30]}"
            existing_fps.add(fp)

    # ── 3. Classify each AI listing using classify_listing ──
    from collections import defaultdict
    match_stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    for ai_listing in all_ai_listings:
        # Skip duplicate
        fp = f"{ai_listing.get('price', 0)}:{ai_listing.get('source', '')}:{ai_listing.get('listedAs', '')[:30]}"
        if fp in existing_fps:
            continue

        # ── Smart classification ──────────────────────────
        match = classify_listing(ai_listing, brand, model, year)

        # Annotate listing with classification metadata
        ai_listing["matchedTrim"]     = match.trim_name
        ai_listing["matchMethod"]     = f"ai_fallback+{match.match_method}"
        ai_listing["matchConfidence"] = (
            "high"   if match.confidence > 0.85 else
            "medium" if match.confidence > 0.60 else
            "low"
        )
        ai_listing["matchReason"] = f"AI → {match.match_detail}"

        trim_key = match.trim_name

        # ── If trim exists in buckets, append; else create new ──
        if trim_key in buckets:
            buckets[trim_key]["listings"].append(ai_listing)
            match_stats[trim_key]["ai_fallback"] += 1
        else:
            buckets[trim_key] = {
                "trim_info": {
                    "name": match.trim_name,
                    "name_ar": match.trim_name_ar,
                    "id": match.trim_id,
                    "msrp": match.msrp,
                    "engine": match.engine,
                },
                "listings": [ai_listing],
                "match_stats": {"ai_fallback": 1},
            }

        existing_fps.add(fp)

    # ── 4. Update match stats ─────────────────────────────
    for key, stats in match_stats.items():
        if key in buckets:
            for method, count in stats.items():
                buckets[key]["match_stats"][method] = \
                    buckets[key]["match_stats"].get(method, 0) + count
