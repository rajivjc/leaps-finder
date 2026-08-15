/**
 * Derived numbers the UI shows but the scanner does not store.
 *
 * Everything here is a pure function of one `scan_results` row, so it can be
 * computed on the client without a round trip — and, more importantly, so it
 * stays checkable against SPEC.md line by line.
 */

import type { ScanResult } from "@/lib/types";

/** SPEC.md §6: the composite's factor weights, and what each one measures. */
export const FACTORS = [
  {
    key: "s_trend",
    label: "Trend",
    weight: 0.25,
    blurb: "Distance above the 200-day, 50-vs-200 spread, and persistence — penalised for extension above the 50-day.",
  },
  {
    key: "s_quality",
    label: "Quality",
    weight: 0.25,
    blurb: "Operating margin, ROE, net debt / EBITDA, revenue growth, FCF margin.",
  },
  {
    key: "s_option",
    label: "Option econ.",
    weight: 0.2,
    blurb: "IV rank, absolute IV30, cost as a share of spot, bid-ask spread, open interest.",
  },
  {
    key: "s_valuation",
    label: "Valuation",
    weight: 0.15,
    blurb: "Haircut analyst upside, plus forward P/E percentile within this scan's universe.",
  },
  {
    key: "s_entry",
    label: "Entry",
    weight: 0.15,
    blurb: "Position inside the stochastic zone (peaks at 25–45) and how recently %K crossed above D.",
  },
] as const satisfies readonly {
  key: keyof ScanResult;
  label: string;
  weight: number;
  blurb: string;
}[];

export type Factor = (typeof FACTORS)[number];

/**
 * SPEC.md §5.5 — cushion between the haircut target and the breakeven.
 *
 *     target_adj = spot · (1 + upside_adj)
 *     cushion    = (target_adj − breakeven) / breakeven
 *
 * The denominator is the breakeven, not the spot; the spec is explicit about
 * that, and the two differ by the premium. Negative results are returned as
 * they are: a cushion below zero means the haircut target does not even reach
 * the breakeven, which is the single most useful thing this number can say.
 */
export function cushion(row: Pick<ScanResult, "spot" | "upside_adj" | "breakeven">): number | null {
  const { spot, upside_adj, breakeven } = row;
  if (spot === null || upside_adj === null || breakeven === null) return null;
  if (!(breakeven > 0)) return null;
  return (spot * (1 + upside_adj) - breakeven) / breakeven;
}

/** The haircut analyst target itself (§6 Valuation), for display beside the cushion. */
export function targetAdjusted(
  row: Pick<ScanResult, "spot" | "upside_adj">,
): number | null {
  if (row.spot === null || row.upside_adj === null) return null;
  return row.spot * (1 + row.upside_adj);
}

/** SPEC.md §7 position sizing: 3% of equity per position, whole contracts only. */
export const MAX_PREMIUM_FRACTION = 0.03;
/** The sleeve caps §7 warns against breaching (warnings, never hard blocks). */
export const SLEEVE_EXPOSURE_CAP = 0.15;
export const MAX_POSITIONS = 5;
export const MAX_PER_SECTOR = 2;

export type SizeResult = {
  maxPremiumDollars: number;
  maxContracts: number;
  /** What buying `maxContracts` actually costs — always ≤ the budget. */
  deployedDollars: number;
  /** Share of equity actually deployed, which whole-contract rounding leaves below 3%. */
  deployedFraction: number;
};

/**
 * §7: `max_premium_dollars = 0.03 · equity`, `max_contracts = floor(that / (mid · 100))`.
 *
 * Returns null rather than a zero-contract result when there is nothing to size
 * against — no equity entered yet, or a symbol with no valid contract to price.
 */
export function positionSize(equity: number | null, mid: number | null): SizeResult | null {
  if (equity === null || !Number.isFinite(equity) || equity <= 0) return null;
  if (mid === null || !Number.isFinite(mid) || mid <= 0) return null;

  const maxPremiumDollars = MAX_PREMIUM_FRACTION * equity;
  const contractCost = mid * 100;
  const maxContracts = Math.floor(maxPremiumDollars / contractCost);
  const deployedDollars = maxContracts * contractCost;

  return {
    maxPremiumDollars,
    maxContracts,
    deployedDollars,
    deployedFraction: deployedDollars / equity,
  };
}

/**
 * SPEC.md §5.1: the LEAP window is DTE ≥ 350. A shorter contract means no
 * expiry that far out existed, and the scanner fell back to the longest one —
 * a fact the UI has to surface rather than quietly present as a LEAP.
 */
export const LEAP_MIN_DTE = 350;

export function isShortDatedFallback(dte: number | null): boolean {
  return dte !== null && dte < LEAP_MIN_DTE;
}

/**
 * The standalone checklist thresholds (the M3 addenda recorded in the scanner's
 * `scoring.py` docstring, not values SPEC.md pins).
 *
 * `scoring.py` is the source of truth: it computes the `quality_pass` / `iv_pass`
 * booleans the UI renders as ticks and crosses. These copies exist only so the
 * checklist can say *how close* a name was — and if they ever drift, the page
 * would explain a red cross with a comparison that reads as passing. A test
 * (`metrics.test.ts`) parses `scoring.py` and fails on divergence, so the copy
 * cannot rot silently.
 */
export const QUALITY_PASS_MIN = 45;
export const IV_PASS_MAX = 50;

/** SPEC.md §7: earnings inside 21 days is the informational amber flag. */
export const EARNINGS_WARN_DAYS = 21;

export function isEarningsSoon(dte: number | null): boolean {
  return dte !== null && dte <= EARNINGS_WARN_DAYS;
}
