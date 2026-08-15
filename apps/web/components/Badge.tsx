/**
 * The small status flags a card carries (SPEC.md §8.1).
 *
 * Each of these exists because a number on its own would mislead: an IV rank
 * that is really a substitute, an "annual" contract that is not, an earnings
 * date close enough to matter. They are labels on caveats, not decoration.
 */

import { EARNINGS_WARN_DAYS, isEarningsSoon, isShortDatedFallback } from "@/lib/metrics";
import type { IvRankStatus } from "@/lib/types";

type Tone = "neutral" | "warn";

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
}) {
  const styles =
    tone === "warn"
      ? "bg-[var(--warn-bg)] text-[var(--warn-fg)] border-[var(--warn-border)]"
      : "bg-[var(--surface)] text-[var(--muted)] border-[var(--border)]";

  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium whitespace-nowrap ${styles}`}
    >
      {children}
    </span>
  );
}

/**
 * SPEC.md §6 requires this one explicitly. Until a symbol has 120 IV snapshots
 * of its own, `iv_rank` is not an IV rank at all — it is the cross-sectional
 * percentile of iv30/rv20 standing in for one on the same 0–100 scale. Every
 * symbol will read `warming_up` for the first months of the project's life, so
 * the badge is the difference between a substitute and a claim.
 */
export function IvRankBadge({
  status,
  value,
}: {
  status: IvRankStatus | null;
  value: number | null;
}) {
  if (value === null) {
    return (
      <Badge title="No IV30 for this symbol in this scan, so no rank could be formed.">
        IV rank —
      </Badge>
    );
  }

  if (status === "warming_up") {
    return (
      <Badge title="Fewer than 120 IV snapshots exist for this symbol, so this is not a true IV rank: it is the cross-sectional percentile of IV30 / RV20 standing in on the same scale (SPEC §6).">
        IV pctl {Math.round(value)} · warming up
      </Badge>
    );
  }

  return <Badge title="True IV rank against this symbol's own 252-snapshot range.">IV rank {Math.round(value)}</Badge>;
}

/** §7's informational amber flag: earnings inside 21 days. */
export function EarningsBadge({ dte, date }: { dte: number | null; date: string | null }) {
  if (dte === null) {
    return (
      <Badge title="No next-earnings date was available. This fails the Strict and Balanced earnings gates, which require a known distance.">
        Earnings unknown
      </Badge>
    );
  }

  if (!isEarningsSoon(dte)) return <Badge title={date ?? undefined}>Earnings {dte}d</Badge>;

  return (
    <Badge tone="warn" title={`Earnings ${date ?? "soon"} — inside the ${EARNINGS_WARN_DAYS}-day window (SPEC §7).`}>
      Earnings {dte}d
    </Badge>
  );
}

/**
 * §5.1: when no expiry at least 350 days out exists, the scanner falls back to
 * the longest one available. That contract is not really a LEAP, and its
 * economics are not comparable with the rest — so it says so.
 */
export function ShortDatedBadge({ dte }: { dte: number | null }) {
  if (!isShortDatedFallback(dte)) return null;

  return (
    <Badge
      tone="warn"
      title="No expiry 350+ days out was listed, so this is the longest available contract (SPEC §5.1). Its economics are not comparable with a true one-year LEAP."
    >
      {dte}d — short-dated
    </Badge>
  );
}

/** A symbol whose option chain yielded no valid contract at all. */
export function NoContractBadge() {
  return (
    <Badge title="No call with a two-sided quote was found in the target expiry, so this row has no option economics and no option subscore.">
      No contract
    </Badge>
  );
}
