import Link from "next/link";

import { EarningsBadge, IvRankBadge } from "@/components/Badge";
import { FilterChecklist } from "@/components/FilterChecklist";
import { LeapEconomics } from "@/components/LeapEconomics";
import { Notice } from "@/components/Notice";
import { PriceChart } from "@/components/PriceChart";
import { CompositeScore, FactorBars } from "@/components/ScoreBar";
import { SizeCalculator } from "@/components/SizeCalculator";
import { StochasticPanel } from "@/components/StochasticPanel";
import { EM_DASH, fmtCompactUsd, fmtDate, fmtPercent, fmtUsd } from "@/lib/format";
import { loadTicker } from "@/lib/queries";

export const dynamic = "force-dynamic";

function Panel({
  title,
  children,
  note,
}: {
  title: string;
  children: React.ReactNode;
  note?: string;
}) {
  return (
    <section className="rounded-lg border border-[var(--border)] p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      {note && <p className="mt-1 text-xs leading-relaxed text-[var(--muted)]">{note}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

export default async function TickerPage({ params }: PageProps<"/t/[symbol]">) {
  const { symbol } = await params;
  const requested = decodeURIComponent(symbol).toUpperCase();
  const loaded = await loadTicker(requested);

  if (loaded.state === "unconfigured") {
    return <Notice title="Supabase is not configured">No scan data is readable.</Notice>;
  }
  if (loaded.state === "error") {
    return <Notice title="Could not read this symbol">{loaded.message}</Notice>;
  }
  if (!loaded.data) {
    return (
      <Notice title={`${requested} is not in the latest scan`}>
        Either no scan has completed successfully, or this symbol was not evaluated — it may have
        fallen below the $50B market-cap floor, or its price history never arrived.{" "}
        <Link href="/" className="underline">
          Back to the screener
        </Link>
        .
      </Notice>
    );
  }

  const { scan, row, ticker, bars } = loaded.data;

  return (
    <div className="space-y-6">
      <section>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{row.symbol}</h1>
            <p className="text-sm text-[var(--muted)]">{ticker?.name ?? EM_DASH}</p>
            <p className="mt-2 font-mono text-lg tabular-nums">
              {fmtUsd(row.spot)}
              <span className="ml-2 text-xs text-[var(--muted)]">
                {fmtPercent(row.pct_off_52w_high, 1, { signed: true })} off the 52-week high
              </span>
            </p>
            <p className="mt-1 text-xs text-[var(--muted)]">
              {ticker?.sector ?? EM_DASH} · {fmtCompactUsd(ticker?.market_cap ?? null)} cap
            </p>
          </div>
          <CompositeScore value={row.score} />
        </div>

        <div className="mt-3 flex flex-wrap gap-1.5">
          <IvRankBadge status={row.iv_rank_status} value={row.iv_rank} />
          <EarningsBadge dte={row.earnings_dte} date={row.next_earnings} />
        </div>

        <p className="mt-3 text-xs text-[var(--muted)]">
          As of {fmtDate(scan.as_of_date, true)} · data: Yahoo Finance, delayed
        </p>
      </section>

      <Panel
        title="Weekly price"
        note="Completed weekly candles with the daily SMA50 and SMA200 overlaid — the averages §4's trend filter is defined on. Signals never use the in-progress week."
      >
        <PriceChart bars={bars} symbol={row.symbol} />
      </Panel>

      <Panel
        title="Weekly slow stochastic (10, 3, 3)"
        note="The 20–70 band is the entry zone; Strict narrows the ceiling to 55, and a weekly close crossing below 20 is the exit signal."
      >
        <StochasticPanel bars={bars} />
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Five-filter checklist">
          <FilterChecklist row={row} />
        </Panel>

        <Panel title="LEAP economics">
          <LeapEconomics row={row} />
        </Panel>

        <Panel
          title="Score breakdown"
          note="Raw 0–100 subscores and the weights they enter the composite with. No curve, no rescaling — the bar is the number."
        >
          <FactorBars row={row} showWeights showBlurbs />
          <div className="mt-4 border-t border-[var(--border)] pt-3 text-xs leading-relaxed text-[var(--muted)]">
            {row.score === null
              ? "The composite is undefined because at least one subscore is missing. It is shown as an em dash rather than a number, because a weighted average over a missing term would be a different quantity wearing the same name."
              : "Composite = 0.25·Trend + 0.25·Quality + 0.20·Option econ. + 0.15·Valuation + 0.15·Entry."}
          </div>
        </Panel>

        <Panel title="Position size">
          <SizeCalculator mid={row.opt_mid} symbol={row.symbol} />
        </Panel>
      </div>
    </div>
  );
}
