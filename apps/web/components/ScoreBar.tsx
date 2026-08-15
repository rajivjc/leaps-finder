/**
 * Score bars (SPEC.md §6, §8).
 *
 * This is the anti-TradeformIQ component, so it is worth being explicit about
 * what it refuses to do:
 *
 *   * **No curve.** Bar length is the raw 0–100 subscore, linearly. Nothing is
 *     rescaled to fill the track, and nothing is normalised against the rest of
 *     the universe to make a mediocre name look better among mediocre company.
 *   * **No green at 70.** The fill is neutral below 60 and a muted slate above
 *     it — the one threshold the spec names, in a colour that does not read as
 *     "buy". A red-amber-green ramp would do the inflating that §8 forbids.
 *   * **The number is always printed.** The bar is an aid to scanning; the
 *     digits are the actual claim.
 *
 * A null subscore renders as an empty track and an em dash, never a zero-length
 * bar that looks like a real score of nothing.
 */

import { fmtScore } from "@/lib/format";
import { FACTORS } from "@/lib/metrics";
import type { ScanResult } from "@/lib/types";

/** The spec's only score threshold: neutral below, muted slate at or above. */
const NEUTRAL_BELOW = 60;

export function ScoreBar({
  value,
  className = "",
}: {
  value: number | null;
  className?: string;
}) {
  const known = value !== null && Number.isFinite(value);
  const width = known ? Math.max(0, Math.min(100, value)) : 0;
  const fill = known && value >= NEUTRAL_BELOW ? "var(--score-high)" : "var(--score-low)";

  return (
    <div
      className={`h-1.5 w-full overflow-hidden rounded-full bg-[var(--track)] ${className}`}
      role="img"
      aria-label={known ? `score ${fmtScore(value)} of 100` : "score not available"}
    >
      {known && (
        <div className="h-full rounded-full" style={{ width: `${width}%`, background: fill }} />
      )}
    </div>
  );
}

/** One labelled factor row: name, raw value, bar. */
export function FactorRow({
  label,
  value,
  weight,
  title,
}: {
  label: string;
  value: number | null;
  weight?: number;
  title?: string;
}) {
  return (
    <div className="space-y-1" title={title}>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-[var(--muted)]">
          {label}
          {weight !== undefined && (
            <span className="ml-1 text-[10px] tabular-nums opacity-60">
              {Math.round(weight * 100)}%
            </span>
          )}
        </span>
        <span className="font-mono tabular-nums">{fmtScore(value)}</span>
      </div>
      <ScoreBar value={value} />
    </div>
  );
}

/** The five §6 factors for one row, in composite-weight order. */
export function FactorBars({
  row,
  showWeights = false,
  showBlurbs = false,
  className = "space-y-2.5",
}: {
  row: ScanResult;
  showWeights?: boolean;
  showBlurbs?: boolean;
  /** Layout is the caller's business — cards lay the five out in two columns. */
  className?: string;
}) {
  return (
    <div className={className}>
      {FACTORS.map((factor) => (
        <FactorRow
          key={factor.key}
          label={factor.label}
          value={row[factor.key] as number | null}
          weight={showWeights ? factor.weight : undefined}
          title={showBlurbs ? factor.blurb : undefined}
        />
      ))}
    </div>
  );
}

/**
 * The composite, large. Null whenever any subscore is null (§6 weights every
 * factor, so a missing one has no defined composite) — shown as an em dash with
 * the reason spelled out, never as a zero that would sort last as if it were a
 * real, bad score.
 */
export function CompositeScore({ value }: { value: number | null }) {
  return (
    <div className="text-right">
      <div className="font-mono text-2xl leading-none tabular-nums">{fmtScore(value)}</div>
      <div className="mt-1 text-[10px] uppercase tracking-wide text-[var(--muted)]">
        {value === null ? "incomplete" : "composite"}
      </div>
    </div>
  );
}
