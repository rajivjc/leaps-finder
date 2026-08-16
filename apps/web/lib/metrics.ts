/**
 * Derived numbers the UI shows but the scanner does not store.
 *
 * Everything here is a pure function of one `scan_results` row, so it can be
 * computed on the client without a round trip — and, more importantly, so it
 * stays checkable against SPEC.md line by line.
 */

import type { Alert, ScanResult } from "@/lib/types";

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

/** One option contract covers 100 shares; every premium here is per share. */
export const SHARES_PER_CONTRACT = 100;

/**
 * SPEC.md §7's exit thresholds, mirrored for display only.
 *
 * The scanner's `risk.py` is the source of truth — it is what actually fires
 * alerts. These copies exist so the page can say *how far* a position is from a
 * stop rather than only whether one has fired. A test parses `risk.py` and
 * fails on divergence, so a page cannot end up drawing a stop line the scanner
 * does not use.
 */
export const PREMIUM_STOP_FRACTION = 0.5;
export const TIME_EXIT_DTE = 180;
export const CIRCUIT_BREAKER_LOSS_FRACTION = 0.08;
export const CIRCUIT_BREAKER_WEEKS = 4;
export const CIRCUIT_BREAKER_DAYS = CIRCUIT_BREAKER_WEEKS * 7;

type Holding = { contracts: number; entry_premium: number };

/** Premium paid, in dollars. */
export function costBasis(position: Holding): number {
  return position.entry_premium * position.contracts * SHARES_PER_CONTRACT;
}

export type Pnl = { value: number; cost: number; dollars: number; fraction: number };

/**
 * Mark-to-market P&L against the entry premium.
 *
 * Null when there is no mark — the daily job has not run, or could not price
 * this contract. An unmarked position shows an em dash, never a zero: "we do
 * not know what this is worth" and "this is worth exactly what you paid" are
 * very different statements to put in front of someone holding it.
 */
export function positionPnl(position: Holding, mid: number | null | undefined): Pnl | null {
  if (mid === null || mid === undefined || !Number.isFinite(mid)) return null;

  const cost = costBasis(position);
  const value = mid * position.contracts * SHARES_PER_CONTRACT;
  if (!(cost > 0)) return null;

  return { value, cost, dollars: value - cost, fraction: (value - cost) / cost };
}

/** The mark at which §7's premium stop fires for a given entry. */
export function premiumStopLevel(entryPremium: number): number {
  return PREMIUM_STOP_FRACTION * entryPremium;
}

/** Calendar days from `from` to `to`, both ISO date strings, in UTC. */
export function daysBetween(from: string, to: string): number {
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  return Math.round((end - start) / 86_400_000);
}

/**
 * The circuit-breaker alert currently in force, if any (SPEC.md §7).
 *
 * §7's ban runs four weeks from the day the breaker trips and is not lifted by
 * the sleeve recovering, so "in force" is a question about the alert's age and
 * nothing else.
 *
 * This lives here rather than beside the banner it drives because the banner is
 * a client component: every export of a `"use client"` module becomes an opaque
 * client reference, and a Server Component calling one throws at runtime.
 */
export function activeCircuitBreaker<T extends Pick<Alert, "kind" | "created_at">>(
  alerts: T[],
  now: Date,
): T | null {
  const floor = now.getTime() - CIRCUIT_BREAKER_DAYS * 86_400_000;
  return (
    alerts.find(
      (alert) =>
        alert.kind === "circuit_breaker" &&
        alert.created_at !== null &&
        Date.parse(alert.created_at) > floor,
    ) ?? null
  );
}

export type SectorMeter = { sector: string; count: number; over: boolean };

export type SleeveMeters = {
  /** Open premium at cost, which is what the caps are written against. */
  openPremium: number;
  /** Share of current equity; null until an equity figure exists. */
  exposureFraction: number | null;
  overExposureCap: boolean;
  positionCount: number;
  overPositionCap: boolean;
  sectors: SectorMeter[];
};

/**
 * SPEC.md §7's sleeve meters: 15% of equity in open premium, 2 per sector, 5
 * positions.
 *
 * Exposure is measured at **cost**, not at current mark. The cap governs how
 * much was committed, and a sleeve that has halved in value has not thereby
 * earned room for another trade — marking to market would loosen the limit
 * exactly when it should bite. (Current value is shown separately, as P&L.)
 *
 * These are warnings, never blocks (§7 is explicit); nothing here refuses
 * anything, it only reports.
 */
export function sleeveMeters(
  open: (Holding & { symbol: string })[],
  sectorOf: (symbol: string) => string | null,
  equity: number | null,
): SleeveMeters {
  const openPremium = open.reduce((total, position) => total + costBasis(position), 0);
  const exposureFraction =
    equity !== null && Number.isFinite(equity) && equity > 0 ? openPremium / equity : null;

  const counts = new Map<string, number>();
  for (const position of open) {
    const sector = sectorOf(position.symbol) ?? "Unknown";
    counts.set(sector, (counts.get(sector) ?? 0) + 1);
  }

  return {
    openPremium,
    exposureFraction,
    overExposureCap: exposureFraction !== null && exposureFraction > SLEEVE_EXPOSURE_CAP,
    positionCount: open.length,
    overPositionCap: open.length > MAX_POSITIONS,
    sectors: [...counts.entries()]
      .map(([sector, count]) => ({ sector, count, over: count > MAX_PER_SECTOR }))
      .sort((a, b) => b.count - a.count || a.sector.localeCompare(b.sector)),
  };
}

/**
 * Which of §7's caps a prospective entry would breach.
 *
 * Shared deliberately: the entry form shows these as warnings *before* the
 * owner commits, and the server action recomputes them to write the override
 * log §7 asks for. Recomputing server-side rather than trusting what the form
 * submitted is the point — otherwise the log would record whatever the client
 * chose to admit to.
 *
 * An empty list means the entry breaches nothing.
 */
export function capBreaches(
  candidate: Holding & { symbol: string; sector: string | null },
  open: (Holding & { symbol: string })[],
  sectorOf: (symbol: string) => string | null,
  equity: number | null,
): string[] {
  const breaches: string[] = [];
  const cost = costBasis(candidate);

  if (equity !== null && equity > 0) {
    const perPosition = MAX_PREMIUM_FRACTION * equity;
    if (cost > perPosition) {
      breaches.push(
        `Position premium $${Math.round(cost).toLocaleString("en-US")} exceeds the ` +
          `${Math.round(MAX_PREMIUM_FRACTION * 100)}% per-position budget of ` +
          `$${Math.round(perPosition).toLocaleString("en-US")}.`,
      );
    }

    const after = open.reduce((total, position) => total + costBasis(position), 0) + cost;
    if (after / equity > SLEEVE_EXPOSURE_CAP) {
      breaches.push(
        `Sleeve exposure would reach ${((after / equity) * 100).toFixed(1)}% of equity, over the ` +
          `${Math.round(SLEEVE_EXPOSURE_CAP * 100)}% cap.`,
      );
    }
  }

  if (open.length + 1 > MAX_POSITIONS) {
    breaches.push(`This would be open position ${open.length + 1}, over the ${MAX_POSITIONS} cap.`);
  }

  const sector = candidate.sector;
  if (sector) {
    const existing = open.filter((position) => sectorOf(position.symbol) === sector).length;
    if (existing + 1 > MAX_PER_SECTOR) {
      breaches.push(
        `This would be ${existing + 1} positions in ${sector}, over the ${MAX_PER_SECTOR} cap.`,
      );
    }
  }

  return breaches;
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
