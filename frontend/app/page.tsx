"use client";

import { useEffect, useState } from "react";

// ─── Static metadata for the dropdowns ─────────────────────────
const MODELS_BY_BRAND: Record<string, { label: string; models: { id: string; label: string }[] }> = {
  toyota: {
    label: "تويوتا",
    models: [
      { id: "camry",          label: "كامري" },
      { id: "corolla",        label: "كورولا" },
      { id: "corolla cross",  label: "كورولا كروس" },
      { id: "fortuner",       label: "فورتشنر" },
      { id: "yaris",          label: "يارس" },
      { id: "rav4",           label: "راف فور" },
      { id: "prado",          label: "برادو" },
      { id: "land cruiser",   label: "لاندكروزر" },
      { id: "highlander",     label: "هايلاندر" },
      { id: "hilux",          label: "هايلكس" },
      { id: "innova",         label: "اينوفا" },
      { id: "raize",          label: "رايز" },
      { id: "veloz",          label: "فيلوز" },
      { id: "urban cruiser",  label: "اربن كروزر" },
      { id: "crown",          label: "كراون" },
      { id: "supra",          label: "سوبرا" },
      { id: "gr86",           label: "جي ار 86" },
    ],
  },
  lexus: {
    label: "لكزس",
    models: [
      { id: "rx", label: "آر اكس" },
      { id: "lx", label: "ال اكس" },
      { id: "es", label: "اي اس" },
      { id: "nx", label: "ان اكس" },
      { id: "is", label: "آي اس" },
      { id: "ls", label: "ال اس" },
      { id: "ux", label: "يو اكس" },
      { id: "lc", label: "ال سي" },
    ],
  },
};
const YEARS = [2026, 2025, 2024, 2023];

const SOURCE_SHORT: Record<string, string> = {
  "toyota.com.sa":      "تويوتا",
  "lexus.com.sa":       "لكزس",
  "haraj.com.sa":       "حراج",
  "syarah.com":         "سيارة",
  "ksa.Motory.com":     "موتوري",
  "ksa.yallamotor.com": "يلاموتور",
};

const TREND_LABEL: Record<string, string> = {
  up: "صاعد",
  down: "هابط",
  stable: "مستقر",
};

const API_URL = "http://127.0.0.1:8000/prices";

// ─── Backend schema types ──────────────────────────────────────
type Listing = {
  source: string; sourceName: string; listedAs: string; price: number;
  priceType: string; condition: string; mileage: string; location: string;
  priceNote: string; sellerType: string; sellerName: string;
  url: string;
};
type Trim = {
  officialName: string;
  officialNameAr: string;
  officialMSRP: number;
  engine: string;
  commonAliases: string[];
  listings: Listing[];
  priceAnalysis: {
    marketMin: number; marketMax: number; marketAvg: number;
    vsOfficialPct: number; trend: string; listingCount: number;
  };
};
type CompetitorAnalysis = {
  summary?: string;
  opportunities?: string[];
  threats?: string[];
  recommendation?: string;
};
type ApiResponse = {
  vehicle: string;
  brand: string; model: string; year: number;
  searchDate: string;
  isAIFallback: boolean;
  officialPriceRange: { min: number; max: number };
  references: Record<string, { name: string; url: string }>;
  sources_queried: string[];
  totals: Record<string, number>;
  marketInsight: string;
  competitorAnalysis: CompetitorAnalysis;
  trims: Trim[];
  error?: string;
};

const fmt = (n: number) => (n ? n.toLocaleString() : "—");

function avgBySource(listings: Listing[]): Record<string, { avg: number; count: number }> {
  const buckets: Record<string, number[]> = {};
  for (const l of listings) {
    if (!l.price) continue;
    if (!buckets[l.source]) buckets[l.source] = [];
    buckets[l.source].push(l.price);
  }
  const out: Record<string, { avg: number; count: number }> = {};
  for (const [src, prices] of Object.entries(buckets)) {
    const avg = Math.round(prices.reduce((a, b) => a + b, 0) / prices.length);
    out[src] = { avg, count: prices.length };
  }
  return out;
}

// ─── Theme toggle hook ─────────────────────────────────────────
function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">("dark");
  useEffect(() => {
    const t = document.documentElement.classList.contains("dark") ? "dark" : "light";
    setTheme(t);
  }, []);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    try { localStorage.setItem("theme", next); } catch {}
  };
  return { theme, toggle };
}

// ─── Page ───────────────────────────────────────────────────────
export default function Home() {
  const { theme, toggle: toggleTheme } = useTheme();
  const [brand, setBrand] = useState("toyota");
  const [model, setModel] = useState("camry");
  const [year, setYear] = useState(2025);
  const [enrich, setEnrich] = useState(true);
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);

  const brandData = MODELS_BY_BRAND[brand];
  const models = brandData?.models || [];

  const handleBrandChange = (b: string) => {
    setBrand(b);
    const list = MODELS_BY_BRAND[b]?.models || [];
    if (!list.find((m) => m.id === model)) setModel(list[0]?.id || "");
  };

  const execute = async () => {
    setLoading(true);
    setError(null);
    setData(null);
    setElapsed(null);
    const t0 = performance.now();
    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brand, model, year, enrich_with_ai: enrich }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status} — ${text.slice(0, 200)}`);
      }
      const json: ApiResponse = await res.json();
      if (json.error) throw new Error(json.error);
      setData(json);
      setElapsed(Math.round(performance.now() - t0) / 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="min-h-screen px-6 py-10 max-w-7xl mx-auto">
      {/* ── Header ── */}
      <header className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-sky-600 dark:text-sky-400">
            محرك أسعار السيارات السعودي
          </h1>
          <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
            متوسط الأسعار لكل فئة من ٥ مصادر — تويوتا/لكزس + حراج + سيارة + موتوري + يلا موتور
          </p>
        </div>
        <button
          onClick={toggleTheme}
          aria-label="تبديل الوضع"
          className="shrink-0 w-10 h-10 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center justify-center text-lg"
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
      </header>

      {/* ── Form ── */}
      <section className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-5 mb-6 shadow-sm">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">الصنع</label>
            <select
              value={brand}
              onChange={(e) => handleBrandChange(e.target.value)}
              disabled={loading}
              className="w-full bg-gray-50 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              {Object.entries(MODELS_BY_BRAND).map(([id, info]) => (
                <option key={id} value={id}>{info.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">الموديل</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={loading}
              className="w-full bg-gray-50 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">السنة</label>
            <select
              value={year}
              onChange={(e) => setYear(parseInt(e.target.value))}
              disabled={loading}
              className="w-full bg-gray-50 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              {YEARS.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={execute}
              disabled={loading}
              className="w-full bg-sky-600 hover:bg-sky-500 disabled:bg-gray-300 dark:disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-md px-4 py-2 text-sm transition"
            >
              {loading ? "جاري الجلب… (١٥-٤٠ ث)" : "بحث"}
            </button>
          </div>
        </div>
        <label className="flex items-center gap-2 mt-4 text-sm text-gray-600 dark:text-gray-400">
          <input
            type="checkbox"
            checked={enrich}
            onChange={(e) => setEnrich(e.target.checked)}
            disabled={loading}
            className="accent-sky-500"
          />
          تحليل بالذكاء الاصطناعي (رؤية السوق + SWOT) — يضيف ١٠–١٥ ثانية
        </label>
      </section>

      {loading && (
        <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-6 mb-6 text-center text-gray-600 dark:text-gray-400">
          <span className="inline-block w-5 h-5 border-2 border-sky-500 border-t-transparent rounded-full animate-spin ml-2 align-middle" />
          جاري الاستعلام من ٥ مصادر بالتوازي…
        </div>
      )}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-700 text-red-800 dark:text-red-200 rounded-xl p-4 mb-6">
          <div className="font-medium mb-1">فشل الطلب</div>
          <div className="text-sm font-mono" dir="ltr">{error}</div>
        </div>
      )}

      {data && <Results data={data} elapsed={elapsed} />}
    </main>
  );
}

function Results({ data, elapsed }: { data: ApiResponse; elapsed: number | null }) {
  const sources = data.sources_queried;
  const range = data.officialPriceRange;
  const totals = data.totals || {};
  const totalListings = data.trims.reduce((acc, t) => acc + t.listings.length, 0);

  return (
    <>
      <section className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-5 mb-6 shadow-sm">
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
          <h2 className="text-xl font-semibold">{data.vehicle}</h2>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            تاريخ البحث: {data.searchDate}
          </span>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            السعر الرسمي: <span dir="ltr">{fmt(range.min)} – {fmt(range.max)}</span> ر.س
          </span>
          {elapsed != null && (
            <span className="text-sm text-gray-400 dark:text-gray-500 ms-auto" dir="ltr">
              {elapsed.toFixed(1)}s
            </span>
          )}
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-500 mt-2">
          الإعلانات المجمّعة: حراج={totals.haraj_listings ?? 0} · سيارة={totals.syarah_listings ?? 0}
          {" "}· موتوري={totals.motory_listings ?? 0} · يلاموتور={totals.yallamotor_listings ?? 0}
        </div>
      </section>

      {/* ── Table 1 ── */}
      <section className="mb-6">
        <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">
          متوسط الأسعار لكل فئة حسب المصدر
        </h3>
        <div className="overflow-x-auto border border-gray-200 dark:border-gray-800 rounded-xl bg-white dark:bg-gray-900">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <tr>
                <th className="text-start px-4 py-3 font-medium">الفئة</th>
                {sources.map((s) => (
                  <th key={s} className="text-end px-4 py-3 font-medium">{SOURCE_SHORT[s] ?? s}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.trims.map((t) => {
                const cells = avgBySource(t.listings);
                return (
                  <tr key={t.officialName} className="border-t border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-900/50">
                    <td className="px-4 py-3 font-medium">{t.officialName}</td>
                    {sources.map((s) => {
                      const c = cells[s];
                      return (
                        <td key={s} className="px-4 py-3 text-end font-mono" dir="ltr">
                          {c ? (
                            <span>
                              {fmt(c.avg)}
                              <span className="text-gray-400 dark:text-gray-500 ms-1">(n={c.count})</span>
                            </span>
                          ) : <span className="text-gray-400 dark:text-gray-600">—</span>}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ── Table 2 ── */}
      <section className="mb-6">
        <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">
          تحليل الأسعار مقابل السعر الرسمي
        </h3>
        <div className="overflow-x-auto border border-gray-200 dark:border-gray-800 rounded-xl bg-white dark:bg-gray-900">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <tr>
                <th className="text-start px-4 py-3 font-medium">الفئة</th>
                <th className="text-end px-4 py-3 font-medium">السعر الرسمي</th>
                <th className="text-end px-4 py-3 font-medium">أدنى السوق</th>
                <th className="text-end px-4 py-3 font-medium">متوسط السوق</th>
                <th className="text-end px-4 py-3 font-medium">أعلى السوق</th>
                <th className="text-end px-4 py-3 font-medium">الفرق %</th>
                <th className="text-center px-4 py-3 font-medium">الاتجاه</th>
                <th className="text-end px-4 py-3 font-medium">العدد</th>
              </tr>
            </thead>
            <tbody>
              {data.trims.map((t) => {
                const pa = t.priceAnalysis;
                const trendColor =
                  pa.trend === "up"   ? "text-emerald-600 dark:text-emerald-400"
                  : pa.trend === "down" ? "text-rose-600 dark:text-rose-400"
                  : "text-gray-500 dark:text-gray-400";
                const vsColor =
                  pa.vsOfficialPct > 0 ? "text-emerald-600 dark:text-emerald-400"
                  : pa.vsOfficialPct < 0 ? "text-rose-600 dark:text-rose-400"
                  : "text-gray-500 dark:text-gray-400";
                return (
                  <tr key={t.officialName} className="border-t border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-900/50">
                    <td className="px-4 py-3 font-medium">{t.officialName}</td>
                    <td className="px-4 py-3 text-end font-mono" dir="ltr">{fmt(t.officialMSRP)}</td>
                    <td className="px-4 py-3 text-end font-mono" dir="ltr">{fmt(pa.marketMin)}</td>
                    <td className="px-4 py-3 text-end font-mono" dir="ltr">{fmt(pa.marketAvg)}</td>
                    <td className="px-4 py-3 text-end font-mono" dir="ltr">{fmt(pa.marketMax)}</td>
                    <td className={`px-4 py-3 text-end font-mono ${vsColor}`} dir="ltr">
                      {pa.vsOfficialPct > 0 ? "+" : ""}{pa.vsOfficialPct.toFixed(1)}%
                    </td>
                    <td className={`px-4 py-3 text-center ${trendColor}`}>
                      {TREND_LABEL[pa.trend] ?? pa.trend}
                    </td>
                    <td className="px-4 py-3 text-end text-gray-500 font-mono" dir="ltr">{pa.listingCount}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ── Table 3: every listing ── */}
      <section className="mb-6">
        <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">
          جميع الإعلانات ({totalListings})
        </h3>
        <div className="overflow-x-auto border border-gray-200 dark:border-gray-800 rounded-xl bg-white dark:bg-gray-900">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400 border-b border-gray-200 dark:border-gray-800">
              <tr>
                <th className="text-start  px-3 py-3 font-medium">الفئة</th>
                <th className="text-start  px-3 py-3 font-medium">المصدر</th>
                <th className="text-start  px-3 py-3 font-medium">العنوان</th>
                <th className="text-end    px-3 py-3 font-medium">السعر</th>
                <th className="text-start  px-3 py-3 font-medium">الحالة</th>
                <th className="text-start  px-3 py-3 font-medium">الممشى</th>
                <th className="text-start  px-3 py-3 font-medium">الموقع</th>
                <th className="text-start  px-3 py-3 font-medium">الفرق</th>
                <th className="text-center px-3 py-3 font-medium">الرابط</th>
              </tr>
            </thead>
            <tbody>
              {data.trims.flatMap((t) =>
                t.listings.map((l, i) => {
                  const isOfficial = l.priceType === "official_msrp";
                  return (
                    <tr
                      key={`${t.officialName}-${i}`}
                      className={`border-t border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-900/50 ${
                        isOfficial ? "bg-sky-50/70 dark:bg-sky-950/20" : ""
                      }`}
                    >
                      <td className="px-3 py-2.5 text-gray-700 dark:text-gray-300">{t.officialName}</td>
                      <td className="px-3 py-2.5">
                        <span
                          className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                            isOfficial
                              ? "bg-sky-600 text-white dark:bg-sky-700 dark:text-sky-100"
                              : "bg-gray-200 text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                          }`}
                        >
                          {SOURCE_SHORT[l.source] ?? l.source}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 max-w-md truncate" dir="auto">{l.listedAs}</td>
                      <td className="px-3 py-2.5 text-end font-mono" dir="ltr">{fmt(l.price)}</td>
                      <td className="px-3 py-2.5 text-gray-500 dark:text-gray-400 text-xs" dir="auto">
                        {l.condition}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 dark:text-gray-400 text-xs" dir="auto">
                        {l.mileage}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 dark:text-gray-400 text-xs" dir="auto">
                        {l.location}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 dark:text-gray-400 text-xs" dir="auto">
                        {l.priceNote}
                      </td>
                      <td className="px-3 py-2.5 text-center">
                        {l.url ? (
                          <a
                            href={l.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-sky-600 dark:text-sky-400 hover:text-sky-500 underline"
                          >
                            فتح ↗
                          </a>
                        ) : (
                          <span className="text-gray-400 dark:text-gray-600">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-500 mt-2">
          الصفوف الزرقاء = الأسعار الرسمية. الباقي = إعلانات السوق.
        </div>
      </section>

      {/* ── Market insight ── */}
      {data.marketInsight && (
        <section className="mb-6">
          <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">رؤية السوق</h3>
          <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-5 leading-relaxed">
            {data.marketInsight}
          </div>
        </section>
      )}

      {/* ── SWOT ── */}
      {data.competitorAnalysis && (data.competitorAnalysis.summary || data.competitorAnalysis.opportunities?.length) && (
        <section className="mb-6">
          <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">
            تحليل المنافسة (SWOT)
          </h3>
          <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-5 space-y-4">
            {data.competitorAnalysis.summary && (
              <div>
                <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">الملخص</div>
                <div className="leading-relaxed">{data.competitorAnalysis.summary}</div>
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {!!data.competitorAnalysis.opportunities?.length && (
                <div className="bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800/50 rounded-lg p-4">
                  <div className="text-xs font-medium text-emerald-700 dark:text-emerald-400 mb-2">
                    ✓ الفرص
                  </div>
                  <ul className="space-y-2 text-sm leading-relaxed">
                    {data.competitorAnalysis.opportunities!.map((o, i) => (
                      <li key={i} className="flex gap-2">
                        <span className="text-emerald-600 dark:text-emerald-500 shrink-0">•</span><span>{o}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {!!data.competitorAnalysis.threats?.length && (
                <div className="bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-800/50 rounded-lg p-4">
                  <div className="text-xs font-medium text-rose-700 dark:text-rose-400 mb-2">
                    ⚠ التهديدات
                  </div>
                  <ul className="space-y-2 text-sm leading-relaxed">
                    {data.competitorAnalysis.threats!.map((t, i) => (
                      <li key={i} className="flex gap-2">
                        <span className="text-rose-600 dark:text-rose-500 shrink-0">•</span><span>{t}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            {data.competitorAnalysis.recommendation && (
              <div className="bg-sky-50 dark:bg-sky-900/20 border border-sky-200 dark:border-sky-800/50 rounded-lg p-4">
                <div className="text-xs font-medium text-sky-700 dark:text-sky-400 mb-2">→ التوصية</div>
                <div className="text-sm leading-relaxed">{data.competitorAnalysis.recommendation}</div>
              </div>
            )}
          </div>
        </section>
      )}

      {/* ── References ── */}
      <section className="mb-6">
        <h3 className="text-lg font-semibold mb-3 text-sky-600 dark:text-sky-400">روابط المصادر</h3>
        <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-4">
          <ul className="space-y-1 text-sm">
            {Object.entries(data.references || {}).map(([sid, info]) => (
              <li key={sid} className="flex gap-3">
                <span className="text-gray-500 dark:text-gray-400 w-24 shrink-0">{SOURCE_SHORT[sid] ?? sid}</span>
                <a
                  href={info.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sky-600 dark:text-sky-400 hover:text-sky-500 underline truncate"
                  dir="ltr"
                >
                  {info.url}
                </a>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </>
  );
}
