"use client";

/**
 * Side-by-side comparison of up to four names (SPEC.md §8.3).
 *
 * Raw scores, same as everywhere else: the columns are not normalised against
 * each other. It would be easy — and wrong — to scale each factor bar so the
 * best of the four fills the track; that would make the strongest of four weak
 * candidates look strong. Every bar here is on the same fixed 0–100 axis as on
 * the screener, so a comparison of four mediocre names looks like four mediocre
 * names.
 */

import Link from "next/link";
import { Fragment, useMemo, useState } from "react";

import { FactorRow } from "@/components/ScoreBar";
import { EM_DASH, fmtCompactUsd, fmtInteger, fmtPercent, fmtScore, fmtUsd } from "@/lib/format";
import { FACTORS, cushion } from "@/lib/metrics";
import type { ScanResult, Ticker } from "@/lib/types";

const MAX_COMPARE = 4;

type Metric = { label: string; render: (row: ScanResult, ticker: Ticker | null) => string };

const METRICS: { group: string; rows: Metric[] }[] = [
  {
    group: "Underlying",
    rows: [
      { label: "Price", render: (row) => fmtUsd(row.spot) },
      { label: "% off 52w high", render: (row) => fmtPercent(row.pct_off_52w_high, 1, { signed: true }) },
      { label: "Sector", render: (_row, ticker) => ticker?.sector ?? EM_DASH },
      { label: "Market cap", render: (_row, ticker) => fmtCompactUsd(ticker?.market_cap ?? null) },
    ],
  },
  {
    group: "Signals",
    rows: [
      { label: "Stoch %K", render: (row) => fmtScore(row.stoch_k) },
      { label: "Stoch %D", render: (row) => fmtScore(row.stoch_d) },
      { label: "Turning up", render: (row) => yesNo(row.turning_up) },
      { label: "Trend pass", render: (row) => yesNo(row.trend_pass) },
      { label: "Earnings in", render: (row) => (row.earnings_dte === null ? "unknown" : `${row.earnings_dte}d`) },
    ],
  },
  {
    group: "Volatility",
    rows: [
      {
        label: "IV rank",
        render: (row) =>
          row.iv_rank === null
            ? EM_DASH
            : `${fmtScore(row.iv_rank)}${row.iv_rank_status === "warming_up" ? " (warming up)" : ""}`,
      },
      { label: "IV30", render: (row) => fmtPercent(row.iv30) },
    ],
  },
  {
    group: "LEAP contract",
    rows: [
      { label: "Strike", render: (row) => fmtUsd(row.opt_strike, 0) },
      { label: "Expiry", render: (row) => row.opt_expiry ?? EM_DASH },
      { label: "DTE", render: (row) => fmtInteger(row.opt_dte) },
      { label: "Delta", render: (row) => (row.opt_delta === null ? EM_DASH : row.opt_delta.toFixed(3)) },
      { label: "Mid", render: (row) => fmtUsd(row.opt_mid) },
      { label: "Spread", render: (row) => fmtPercent(row.opt_spread_pct) },
      { label: "Open interest", render: (row) => fmtInteger(row.opt_oi) },
      { label: "Breakeven", render: (row) => fmtUsd(row.breakeven) },
      { label: "Breakeven vs spot", render: (row) => fmtPercent(row.breakeven_pct, 1, { signed: true }) },
      { label: "Cost, % of spot", render: (row) => fmtPercent(row.cost_pct_spot) },
      { label: "Cushion vs target", render: (row) => fmtPercent(cushion(row), 1, { signed: true }) },
    ],
  },
  {
    group: "Presets",
    rows: [
      { label: "Strict", render: (row) => yesNo(row.passes_strict) },
      { label: "Balanced", render: (row) => yesNo(row.passes_balanced) },
      { label: "Wide open", render: (row) => yesNo(row.passes_wide) },
    ],
  },
];

function yesNo(value: boolean | null): string {
  if (value === null) return EM_DASH;
  return value ? "yes" : "no";
}

export function CompareBoard({
  rows,
  tickers,
}: {
  rows: ScanResult[];
  tickers: Record<string, Ticker>;
}) {
  const [selected, setSelected] = useState<string[]>(() =>
    rows.filter((row) => row.passes_wide).slice(0, 3).map((row) => row.symbol),
  );

  const bySymbol = useMemo(
    () => Object.fromEntries(rows.map((row) => [row.symbol, row])),
    [rows],
  );
  const chosen = selected.map((symbol) => bySymbol[symbol]).filter(Boolean);
  const available = rows.filter((row) => !selected.includes(row.symbol));

  function add(symbol: string) {
    if (!symbol || selected.length >= MAX_COMPARE || selected.includes(symbol)) return;
    setSelected([...selected, symbol]);
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value=""
          onChange={(event) => add(event.target.value)}
          disabled={selected.length >= MAX_COMPARE}
          className="rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1.5 text-sm disabled:opacity-50"
        >
          <option value="">
            {selected.length >= MAX_COMPARE ? `Limit is ${MAX_COMPARE}` : "Add a ticker…"}
          </option>
          {available.map((row) => (
            <option key={row.symbol} value={row.symbol}>
              {row.symbol} — {tickers[row.symbol]?.name ?? "?"}
            </option>
          ))}
        </select>

        {selected.map((symbol) => (
          <button
            key={symbol}
            onClick={() => setSelected(selected.filter((entry) => entry !== symbol))}
            className="rounded border border-[var(--border)] px-2 py-1 text-xs hover:border-[var(--muted)]"
            aria-label={`Remove ${symbol}`}
          >
            {symbol} <span className="text-[var(--muted)]">×</span>
          </button>
        ))}
      </div>

      {chosen.length === 0 ? (
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
          Pick up to {MAX_COMPARE} tickers to compare. Every name evaluated in this scan is
          selectable, not just those that clear a preset — seeing why a candidate missed is half the
          comparison.
        </div>
      ) : (
        <>
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: `repeat(${chosen.length}, minmax(0, 1fr))` }}
          >
            {chosen.map((row) => (
              <div key={row.symbol} className="rounded-lg border border-[var(--border)] p-4">
                <Link href={`/t/${encodeURIComponent(row.symbol)}`} className="hover:underline">
                  <h3 className="font-semibold tracking-tight">{row.symbol}</h3>
                </Link>
                <p className="truncate text-xs text-[var(--muted)]">
                  {tickers[row.symbol]?.name ?? EM_DASH}
                </p>
                <p className="mt-2 font-mono text-xl tabular-nums">{fmtScore(row.score)}</p>
                <p className="text-[10px] uppercase tracking-wide text-[var(--muted)]">
                  {row.score === null ? "incomplete" : "composite"}
                </p>

                <div className="mt-4 space-y-2.5">
                  {FACTORS.map((factor) => (
                    <FactorRow
                      key={factor.key}
                      label={factor.label}
                      value={row[factor.key] as number | null}
                      weight={factor.weight}
                      title={factor.blurb}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] bg-[var(--surface)]">
                  <th className="px-3 py-2 text-left text-xs font-medium text-[var(--muted)]">
                    Metric
                  </th>
                  {chosen.map((row) => (
                    <th key={row.symbol} className="px-3 py-2 text-right text-xs font-semibold">
                      {row.symbol}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {METRICS.map((section) => (
                  <Fragment key={section.group}>
                    <tr className="border-b border-[var(--border)]">
                      <td
                        colSpan={chosen.length + 1}
                        className="bg-[var(--surface)] px-3 py-1.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]"
                      >
                        {section.group}
                      </td>
                    </tr>
                    {section.rows.map((metric) => (
                      <tr
                        key={`${section.group}-${metric.label}`}
                        className="border-b border-[var(--border)] last:border-0"
                      >
                        <td className="px-3 py-1.5 text-xs text-[var(--muted)]">{metric.label}</td>
                        {chosen.map((row) => (
                          <td
                            key={row.symbol}
                            className="px-3 py-1.5 text-right font-mono text-xs tabular-nums"
                          >
                            {metric.render(row, tickers[row.symbol] ?? null)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
