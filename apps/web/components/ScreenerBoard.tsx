"use client";

/**
 * The screener's interactive half (SPEC.md §8.1): preset tabs and the advanced
 * filter drawer, over rows the server already fetched.
 *
 * Filtering happens in the browser because the whole dataset is here: the three
 * presets nest, so the Wide set the server sent *is* every row any tab can
 * show. Round-tripping to Postgres for a tab change would add latency to
 * answer a question already in memory.
 *
 * The honesty rule this file has to hold: a filter never silently drops a row
 * whose value is simply unknown. `iv_rank ≤ 40` cannot be evaluated for a
 * symbol with no IV30, so that row is excluded *and counted*, and the count is
 * shown. Quietly omitting it would make the result set look like a complete
 * answer to a question it did not actually ask of every name.
 */

import { useMemo, useState } from "react";

import { ResultCard } from "@/components/ResultCard";
import type { ScanResult, Ticker } from "@/lib/types";

type Preset = "strict" | "balanced" | "wide";

const PRESETS: { key: Preset; label: string; field: keyof ScanResult; blurb: string }[] = [
  {
    key: "strict",
    label: "Strict",
    field: "passes_strict",
    blurb:
      "Stoch 20–55 and turning up · IV rank ≤ 30 · earnings ≥ 30d · spread ≤ 5% · OI ≥ 500 · all quality metrics present and Quality ≥ 60.",
  },
  {
    key: "balanced",
    label: "Balanced",
    field: "passes_balanced",
    blurb:
      "Stoch 20–70 and turning up · IV rank ≤ 50 · earnings ≥ 14d · spread ≤ 8% · OI ≥ 200 · Quality ≥ 45.",
  },
  {
    key: "wide",
    label: "Wide open",
    field: "passes_wide",
    blurb: "Trend pass · stoch 10–80 · spread ≤ 12% · OI ≥ 100. No IV, earnings or quality gate.",
  },
];

type Filters = {
  stochMin: string;
  stochMax: string;
  ivRankMax: string;
  marketCapMinB: string;
  spreadMaxPct: string;
  oiMin: string;
  earningsMinDays: string;
  sector: string;
};

const EMPTY_FILTERS: Filters = {
  stochMin: "",
  stochMax: "",
  ivRankMax: "",
  marketCapMinB: "",
  spreadMaxPct: "",
  oiMin: "",
  earningsMinDays: "",
  sector: "",
};

function parse(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

type Outcome = "keep" | "drop" | "unknown";

/** One bound against one value; `unknown` when the row cannot answer the question. */
function test(
  bound: number | null,
  value: number | null | undefined,
  compare: (value: number, bound: number) => boolean,
): Outcome {
  if (bound === null) return "keep";
  if (value === null || value === undefined) return "unknown";
  return compare(value, bound) ? "keep" : "drop";
}

const atLeast = (value: number, bound: number) => value >= bound;
const atMost = (value: number, bound: number) => value <= bound;

export function ScreenerBoard({
  rows,
  tickers,
  sparklines,
}: {
  rows: ScanResult[];
  tickers: Record<string, Ticker>;
  sparklines: Record<string, number[]>;
}) {
  const counts = useMemo(
    () =>
      Object.fromEntries(
        PRESETS.map(({ key, field }) => [key, rows.filter((row) => row[field] === true).length]),
      ) as Record<Preset, number>,
    [rows],
  );

  // Open on the strictest tier that actually has candidates, falling back to
  // Wide when nothing clears anything. This hides nothing: every tab carries
  // its own count, so a week where Strict and Balanced are both empty says so
  // on the tabs themselves — it just avoids opening on a blank list when
  // stricter tiers are routinely empty (LEAPS open interest is the usual
  // binding gate).
  const [preset, setPreset] = useState<Preset>(
    () => PRESETS.find((entry) => counts[entry.key] > 0)?.key ?? "wide",
  );
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const sectors = useMemo(
    () =>
      [...new Set(Object.values(tickers).map((ticker) => ticker.sector).filter(Boolean))].sort() as string[],
    [tickers],
  );

  const { visible, unknownCount } = useMemo(() => {
    const field = PRESETS.find((entry) => entry.key === preset)!.field;

    // The drawer takes billions and percents; the columns hold raw dollars and
    // fractions, so the bounds are converted once here rather than per row.
    const capBillions = parse(filters.marketCapMinB);
    const spreadPercent = parse(filters.spreadMaxPct);
    const bounds = {
      stochMin: parse(filters.stochMin),
      stochMax: parse(filters.stochMax),
      ivRankMax: parse(filters.ivRankMax),
      marketCapMin: capBillions === null ? null : capBillions * 1e9,
      spreadMax: spreadPercent === null ? null : spreadPercent / 100,
      oiMin: parse(filters.oiMin),
      earningsMin: parse(filters.earningsMinDays),
    };

    const kept: ScanResult[] = [];
    let unknown = 0;

    for (const row of rows) {
      if (row[field] !== true) continue;

      const ticker = tickers[row.symbol];
      if (filters.sector && ticker?.sector !== filters.sector) continue;

      const outcomes: Outcome[] = [
        test(bounds.stochMin, row.stoch_k, atLeast),
        test(bounds.stochMax, row.stoch_k, atMost),
        test(bounds.ivRankMax, row.iv_rank, atMost),
        test(bounds.marketCapMin, ticker?.market_cap, atLeast),
        test(bounds.spreadMax, row.opt_spread_pct, atMost),
        test(bounds.oiMin, row.opt_oi, atLeast),
        test(bounds.earningsMin, row.earnings_dte, atLeast),
      ];

      if (outcomes.includes("drop")) continue;
      if (outcomes.includes("unknown")) {
        unknown += 1;
        continue;
      }
      kept.push(row);
    }

    return { visible: kept, unknownCount: unknown };
  }, [rows, tickers, preset, filters]);

  const active = PRESETS.find((entry) => entry.key === preset)!;
  const nullScores = visible.filter((row) => row.score === null).length;
  const filtersDirty = JSON.stringify(filters) !== JSON.stringify(EMPTY_FILTERS);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <div
          role="tablist"
          aria-label="Preset"
          className="inline-flex rounded-md border border-[var(--border)] p-0.5"
        >
          {PRESETS.map((entry) => (
            <button
              key={entry.key}
              role="tab"
              aria-selected={preset === entry.key}
              onClick={() => setPreset(entry.key)}
              title={entry.blurb}
              className={`rounded px-3 py-1.5 text-sm transition-colors ${
                preset === entry.key
                  ? "bg-[var(--foreground)] text-[var(--background)]"
                  : "text-[var(--muted)] hover:text-[var(--foreground)]"
              }`}
            >
              {entry.label}
              <span className="ml-1.5 tabular-nums opacity-70">{counts[entry.key]}</span>
            </button>
          ))}
        </div>

        <button
          onClick={() => setDrawerOpen((open) => !open)}
          aria-expanded={drawerOpen}
          className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--muted)] hover:text-[var(--foreground)]"
        >
          Filters {filtersDirty && <span className="text-[var(--foreground)]">·</span>}
        </button>

        {filtersDirty && (
          <button
            onClick={() => setFilters(EMPTY_FILTERS)}
            className="text-xs text-[var(--muted)] underline hover:text-[var(--foreground)]"
          >
            Reset
          </button>
        )}
      </div>

      <p className="text-xs leading-relaxed text-[var(--muted)]">{active.blurb}</p>

      {drawerOpen && (
        <div className="grid grid-cols-2 gap-4 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4 sm:grid-cols-4">
          <Field label="Stoch %K ≥" value={filters.stochMin} onChange={(v) => setFilters({ ...filters, stochMin: v })} />
          <Field label="Stoch %K ≤" value={filters.stochMax} onChange={(v) => setFilters({ ...filters, stochMax: v })} />
          <Field label="IV rank ≤" value={filters.ivRankMax} onChange={(v) => setFilters({ ...filters, ivRankMax: v })} />
          <Field label="Market cap ≥ ($B)" value={filters.marketCapMinB} onChange={(v) => setFilters({ ...filters, marketCapMinB: v })} />
          <Field label="Spread ≤ (%)" value={filters.spreadMaxPct} onChange={(v) => setFilters({ ...filters, spreadMaxPct: v })} />
          <Field label="Open interest ≥" value={filters.oiMin} onChange={(v) => setFilters({ ...filters, oiMin: v })} />
          <Field label="Earnings ≥ (days)" value={filters.earningsMinDays} onChange={(v) => setFilters({ ...filters, earningsMinDays: v })} />
          <label className="block space-y-1 text-xs">
            <span className="text-[var(--muted)]">Sector</span>
            <select
              value={filters.sector}
              onChange={(event) => setFilters({ ...filters, sector: event.target.value })}
              className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-sm"
            >
              <option value="">All</option>
              {sectors.map((sector) => (
                <option key={sector} value={sector}>
                  {sector}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <div className="space-y-1 text-xs text-[var(--muted)]">
        <p>
          Showing {visible.length} of {counts[preset]} names in {active.label}.
        </p>
        {unknownCount > 0 && (
          <p>
            {unknownCount} more {unknownCount === 1 ? "name is" : "names are"} hidden because a
            filtered value is unknown for {unknownCount === 1 ? "it" : "them"} — an unknown IV rank
            or earnings date cannot be tested against a bound, so it is excluded rather than assumed
            to pass.
          </p>
        )}
        {nullScores > 0 && (
          <p>
            {nullScores} shown {nullScores === 1 ? "name has" : "names have"} no composite: a
            subscore is missing, so the weighted total is undefined and is not ranked. Those sort
            last rather than as a score of zero.
          </p>
        )}
      </div>

      {visible.length === 0 ? (
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
          Nothing clears {active.label} with these filters. That is a real answer, not an error —
          the strategy is supposed to sit out weeks where nothing qualifies.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {visible.map((row) => (
            <ResultCard
              key={row.symbol}
              row={row}
              ticker={tickers[row.symbol] ?? null}
              sparkline={sparklines[row.symbol] ?? []}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block space-y-1 text-xs">
      <span className="text-[var(--muted)]">{label}</span>
      <input
        type="number"
        inputMode="decimal"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="any"
        className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-sm tabular-nums"
      />
    </label>
  );
}
