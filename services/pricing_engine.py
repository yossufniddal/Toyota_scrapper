"""
Dealer Pricing Engine and Bundle Pricing logic.
"""
from shared import BUNDLE_ADDONS


def bundle_vs_discount(discount_sar: int,
                        addons: list[str] | None = None) -> dict:
    """
    Compare direct price discount vs offering add-on bundles with same perceived value.
    """
    if addons is None:
        selected, total_value, total_cost = [], 0, 0
        for name, info in sorted(BUNDLE_ADDONS.items(), key=lambda x: x[1]["value"] / x[1]["cost"], reverse=True):
            if total_value >= discount_sar:
                break
            selected.append(name)
            total_value += info["value"]
            total_cost  += info["cost"]
        addons = selected

    total_cost  = sum(BUNDLE_ADDONS[a]["cost"]  for a in addons if a in BUNDLE_ADDONS)
    total_value = sum(BUNDLE_ADDONS[a]["value"] for a in addons if a in BUNDLE_ADDONS)
    saving      = discount_sar - total_cost
    saving_pct  = round(saving / discount_sar * 100) if discount_sar else 0

    addon_details = [
        {
            "name":  a,
            "label": BUNDLE_ADDONS[a]["label"],
            "cost":  BUNDLE_ADDONS[a]["cost"],
            "value": BUNDLE_ADDONS[a]["value"],
        }
        for a in addons if a in BUNDLE_ADDONS
    ]

    if saving > 0:
        rec = (f"بدل تخفيض {discount_sar:,} ريال مباشرة، قدم إضافات بقيمة ظاهرية "
               f"{total_value:,} ريال بتكلفة فعلية {total_cost:,} ريال فقط — "
               f"توفير {saving:,} ريال ({saving_pct}%) على الهامش.")
    else:
        rec = f"التخفيض المباشر بـ {discount_sar:,} ريال أفضل في هذه الحالة."

    return {
        "discount_cost":  discount_sar,
        "bundle_cost":    total_cost,
        "bundle_value":   total_value,
        "saving":         saving,
        "saving_pct":     saving_pct,
        "addons":         addon_details,
        "recommendation": rec,
    }


def calc_dealer_price(
    msrp:         int,
    market_min:   int,
    market_avg:   int,
    market_max:   int,
    total_listings: int,
    new_listings:   int,
    trend_direction: str = "stable",
    dom_avg:    float | None = None,
) -> dict:
    """
    Compute suggested dealer price based on market data, supply, trend, and DOM.
    """
    if not market_avg or not msrp:
        return {"suggested": 0, "range_low": 0, "range_high": 0,
                "supply_score": "unknown", "pressure": 0,
                "adjustments": [], "logic": "بيانات غير كافية"}

    adjustments = []
    base = market_avg

    # ── 1. supply pressure ──────────────────────────────────────
    new_pct = (new_listings / total_listings * 100) if total_listings > 0 else 50

    if total_listings == 0:
        supply_score = "unknown"
        pressure     = 0
    elif new_listings <= 2:
        supply_score = "scarce"
        pressure     = 2
        adj = int(market_avg * 0.025)
        base += adj
        adjustments.append({
            "factor":  "supply_scarce",
            "label":   f"عرض شحيح جداً (جديدة: {new_listings} فقط) → +{adj:,}",
            "delta":   adj,
        })
    elif new_listings <= 5:
        supply_score = "low"
        pressure     = 1
        adj = int(market_avg * 0.015)
        base += adj
        adjustments.append({
            "factor":  "supply_low",
            "label":   f"عرض منخفض (جديدة: {new_listings}) → +{adj:,}",
            "delta":   adj,
        })
    elif new_listings <= 12:
        supply_score = "medium"
        pressure     = 0
        adjustments.append({
            "factor":  "supply_medium",
            "label":   f"عرض متوازن (جديدة: {new_listings}) → لا تعديل",
            "delta":   0,
        })
    elif new_listings <= 25:
        supply_score = "high"
        pressure     = -1
        adj = int(market_avg * 0.015)
        base -= adj
        adjustments.append({
            "factor":  "supply_high",
            "label":   f"عرض مرتفع (جديدة: {new_listings}) → −{adj:,}",
            "delta":   -adj,
        })
    else:
        supply_score = "very_high"
        pressure     = -2
        adj = int(market_avg * 0.030)
        base -= adj
        adjustments.append({
            "factor":  "supply_very_high",
            "label":   f"عرض مرتفع جداً (جديدة: {new_listings}) → −{adj:,}",
            "delta":   -adj,
        })

    # ── 2. new vs used ratio ────────────────────────────────────
    if total_listings > 0 and new_pct < 30:
        adj = int(market_avg * 0.010)
        base += adj
        adjustments.append({
            "factor":  "mostly_used",
            "label":   f"معظم الإعلانات مستعملة ({new_pct:.0f}% جديدة) → +{adj:,}",
            "delta":   adj,
        })

    # ── 3. price trend ──────────────────────────────────────────
    if trend_direction == "up":
        adj = int(market_avg * 0.010)
        base += adj
        adjustments.append({
            "factor":  "trend_up",
            "label":   f"السوق في ارتفاع → +{adj:,}",
            "delta":   adj,
        })
    elif trend_direction == "down":
        adj = int(market_avg * 0.010)
        base -= adj
        adjustments.append({
            "factor":  "trend_down",
            "label":   f"السوق في انخفاض (سعّر تحسباً) → −{adj:,}",
            "delta":   -adj,
        })

    # ── 4. days on market ───────────────────────────────────────
    if dom_avg is not None:
        if dom_avg <= 7:
            adj = int(market_avg * 0.010)
            base += adj
            adjustments.append({
                "factor":  "dom_fast",
                "label":   f"إعلانات تنباع سريعاً (متوسط {dom_avg:.0f} أيام) → +{adj:,}",
                "delta":   adj,
            })
        elif dom_avg >= 30:
            adj = int(market_avg * 0.015)
            base -= adj
            adjustments.append({
                "factor":  "dom_slow",
                "label":   f"إعلانات تبقى طويلاً (متوسط {dom_avg:.0f} يوم) → −{adj:,}",
                "delta":   -adj,
            })

    # ── 5. floor ────────────────────────────────────────────────
    floor = market_min + 500
    if base < floor:
        adjustments.append({
            "factor":  "floor",
            "label":   f"لا تبيع أقل من أدنى السوق+500 ({floor:,}) → تعديل للأعلى",
            "delta":   floor - base,
        })
        base = floor

    # ── 6. ceiling ──────────────────────────────────────────────
    ceiling = int(market_max * 1.03)
    if base > ceiling:
        adjustments.append({
            "factor":  "ceiling",
            "label":   f"أعلى من سقف السوق → ضبط إلى {ceiling:,}",
            "delta":   ceiling - base,
        })
        base = ceiling

    range_low  = int(base * 0.98)
    range_high = int(base * 1.02)

    supply_labels = {
        "scarce":    "شحيح جداً",
        "low":       "منخفض",
        "medium":    "متوازن",
        "high":      "مرتفع",
        "very_high": "مرتفع جداً",
        "unknown":   "غير محدد",
    }
    pressure_labels = {
        2:  "فرصة ذهبية — الطلب أعلى من العرض",
        1:  "فرصة — العرض منخفض نسبياً",
        0:  "متوازن — سعّر قريب من المتوسط",
        -1: "ضغط تنافسي — العرض مرتفع",
        -2: "ضغط شديد — نافس بالخدمة لا السعر",
    }

    logic = (
        f"متوسط السوق {market_avg:,} ريال | "
        f"عرض الجديدة: {supply_labels[supply_score]} ({new_listings} إعلان) | "
        f"الضغط: {pressure_labels.get(pressure, '')} | "
        f"السعر المقترح: {int(base):,} ريال"
    )

    return {
        "suggested":     int(base),
        "range_low":     range_low,
        "range_high":    range_high,
        "supply_score":  supply_score,
        "pressure":      pressure,
        "new_listings":  new_listings,
        "total_listings": total_listings,
        "new_pct":       round(new_pct, 0),
        "adjustments":   adjustments,
        "logic":         logic,
    }


def _attach_dealer_pricing(data: dict):
    """
    Adds trim["dealerPricing"] = calc_dealer_price(...) for each trim.
    Should be called after _attach_source_stats.
    """
    for trim in data.get("trims", []):
        pa      = trim.get("priceAnalysis", {})
        ss      = trim.get("supplyStats", {})
        trend   = trim.get("priceTrend",  {})
        dom     = trim.get("daysOnMarket", {})

        msrp         = trim.get("officialMSRP", 0)
        market_min   = pa.get("marketMin", 0)
        market_avg   = pa.get("marketAvg", 0)
        market_max   = pa.get("marketMax", 0)
        total        = ss.get("totalListings", 0)
        new_l        = ss.get("newListings", 0)
        trend_dir    = trend.get("direction", "stable")
        dom_avg      = dom.get("avg", None)

        trim["dealerPricing"] = calc_dealer_price(
            msrp=msrp,
            market_min=market_min,
            market_avg=market_avg,
            market_max=market_max,
            total_listings=total,
            new_listings=new_l,
            trend_direction=trend_dir,
            dom_avg=dom_avg,
        )
