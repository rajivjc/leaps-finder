/**
 * One screener result (SPEC.md §8.1): symbol, name, price, % off the 52-week
 * high, the composite with its factor mini-bars, a weekly-stochastic sparkline,
 * the earnings and IV-rank badges, and a one-line summary of the LEAP.
 */

import Link from "next/link";

import { EarningsBadge, IvRankBadge, NoContractBadge, ShortDatedBadge } from "@/components/Badge";
import { CompositeScore, FactorBars } from "@/components/ScoreBar";
import { Sparkline } from "@/components/Sparkline";
import { EM_DASH, fmtPercent, fmtUsd } from "@/lib/format";
import type { ScanResult, Ticker } from "@/lib/types";

/** The §8.1 one-liner: strike, DTE, breakeven %, cost as a share of spot. */
function LeapLine({ row }: { row: ScanResult }) {
  if (row.opt_strike === null || row.opt_expiry === null) {
    return (
      <p className="text-xs text-[var(--muted)]">
        No LEAP economics — the chain yielded no call with a two-sided quote.
      </p>
    );
  }

  return (
    <p className="font-mono text-xs text-[var(--muted)]">
      {fmtUsd(row.opt_strike, 0)} call · {row.opt_dte ?? EM_DASH}d · breakeven{" "}
      {fmtPercent(row.breakeven_pct, 1, { signed: true })} · costs {fmtPercent(row.cost_pct_spot)} of
      spot
    </p>
  );
}

export function ResultCard({
  row,
  ticker,
  sparkline,
}: {
  row: ScanResult;
  ticker: Ticker | null;
  sparkline: number[];
}) {
  return (
    <article className="rounded-lg border border-[var(--border)] p-4 transition-colors hover:border-[var(--muted)]">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Link href={`/t/${encodeURIComponent(row.symbol)}`} className="hover:underline">
            <h3 className="font-semibold tracking-tight">{row.symbol}</h3>
          </Link>
          <p className="truncate text-xs text-[var(--muted)]" title={ticker?.name ?? undefined}>
            {ticker?.name ?? EM_DASH}
          </p>
          <p className="mt-1.5 font-mono text-sm tabular-nums">
            {fmtUsd(row.spot)}
            <span className="ml-2 text-xs text-[var(--muted)]">
              {fmtPercent(row.pct_off_52w_high, 1, { signed: true })} off 52w high
            </span>
          </p>
        </div>

        <div className="flex shrink-0 items-start gap-3">
          <Sparkline
            values={sparkline}
            label={`${row.symbol} weekly stochastic over the last ${sparkline.length} completed weeks`}
          />
          <CompositeScore value={row.score} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <IvRankBadge status={row.iv_rank_status} value={row.iv_rank} />
        <EarningsBadge dte={row.earnings_dte} date={row.next_earnings} />
        <ShortDatedBadge dte={row.opt_dte} />
        {row.opt_strike === null && <NoContractBadge />}
      </div>

      <div className="mt-3 border-t border-[var(--border)] pt-3">
        <LeapLine row={row} />
      </div>

      <FactorBars
        row={row}
        showBlurbs
        className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2"
      />
    </article>
  );
}
