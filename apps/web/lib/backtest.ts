/**
 * Loads the committed backtest results at build time (SPEC-BACKTEST.md §8).
 *
 * §8 puts the report under `docs/backtest/<run-date>/` and has this page render
 * the latest one *at build time* — there is no Supabase table and no migration
 * behind `/backtest`, just a JSON file the scanner wrote and a human committed.
 *
 * **The banner gate is the point of this module.** §8: "The §1 banner text is
 * the JSON's required top-level `banner` field; the build **fails** if it is
 * absent or empty, so the page cannot render metrics without it." That is
 * enforced here by throwing, which fails `next build` — not by rendering a
 * fallback, because a fallback is exactly the failure mode the rule exists to
 * prevent. A page that shows a 15% CAGR without the caveat saying what it does
 * and does not measure is the thing the spec is trying to make impossible.
 *
 * Not having *any* committed run is a different case and is not an error: the
 * site should not become undeployable because an analysis has not been run yet.
 * `loadBacktest` returns null there and the page says so. The gate is about a
 * run that exists and is missing its caveat.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/** Where §8 puts committed runs, relative to this Next.js app. */
export const RESULTS_ROOT = join(process.cwd(), "..", "..", "docs", "backtest");

export const SUPPORTED_SCHEMA_VERSION = 1;

export type TrackStats = {
  track: string;
  variant: string;
  parameters: { zone?: string; sigma_multiplier?: number; friction?: number } | null;
  trades: number;
  chains: number;
  win_rate: number | null;
  mean_return: number | null;
  median_return: number | null;
  profit_factor: number | null;
  return_percentiles: Record<string, number | null>;
  exit_reasons: Record<string, number>;
  market_delta: Record<string, number | null>;
  market_delta_unpriced: number;
  vehicle_alpha: Record<string, number | null> | null;
  coverage_ratio: number | null;
  low_coverage_warning: boolean | null;
};

export type SleeveStats = {
  name: string;
  start_equity: number;
  final_equity: number;
  total_return: number | null;
  cagr: number | null;
  max_drawdown: number | null;
  max_drawdown_date: string | null;
  calendar_year_returns: Record<string, number | null>;
  positions: number;
  chains: number;
  win_rate: number | null;
  average_open_positions: number | null;
  average_premium_exposure: number | null;
  skipped_entries: Record<string, number>;
  breaker_activations: number;
  breaker_dates: string[];
  unlabelled_symbols: number;
  coverage_ratio: number | null;
  low_coverage_warning: boolean | null;
};

export type BenchmarkStats = {
  name: string;
  label: string;
  total_return: number | null;
  cagr: number | null;
  max_drawdown: number | null;
  max_drawdown_date: string | null;
  constituents: number | null;
  unpriced: number | null;
  caveat: string | null;
  coverage_ratio: number | null;
  low_coverage_warning: boolean | null;
};

export type BiasRow = { row: string; assumption: string; direction: string; note: string };

export type BacktestResults = {
  schema_version: number;
  banner: string;
  run_date: string;
  window: { start: string; end: string; fetch_start: string };
  coverage: {
    coverage_ratio: number;
    status: string;
    low_coverage_warning: boolean;
    members: number;
    no_data_members: number;
    total_member_weeks: number;
    covered_member_weeks: number;
    snapshot_date: string | null;
  };
  track_a: { stock: Record<string, TrackStats>; overlay: Record<string, TrackStats> };
  /** Presentation order per track — the JSON's keys are sorted, §5.6's grid is not. */
  track_a_order: { stock: string[]; overlay: string[] };
  sleeves: SleeveStats[];
  curve_dates: string[];
  sleeve_curves: Record<string, (number | null)[]>;
  benchmarks: BenchmarkStats[];
  benchmark_curves: Record<string, (number | null)[]>;
  bias_register: BiasRow[];
  notes: Record<string, string | number>;
};

/**
 * The newest committed run directory, or null when none exists.
 *
 * Directories are named by ISO run date, so lexicographic order is
 * chronological order and no parsing is needed to pick the latest.
 */
export function latestRunDate(root: string = RESULTS_ROOT): string | null {
  let entries: string[];
  try {
    entries = readdirSync(root);
  } catch {
    return null;
  }
  const runs = entries
    .filter((name) => /^\d{4}-\d{2}-\d{2}$/.test(name))
    .filter((name) => {
      try {
        return statSync(join(root, name)).isDirectory();
      } catch {
        return false;
      }
    })
    .sort();
  return runs.length > 0 ? runs[runs.length - 1] : null;
}

/**
 * Parse and validate one run's `results.json`.
 *
 * Throws — and so fails the build — when the file exists but cannot be trusted:
 * unparseable, wrong schema version, or missing the §8 banner. `NaN` is rejected
 * explicitly because Python's `json.dumps` will happily write a bare `NaN` token
 * that `JSON.parse` refuses anyway; catching it here names the problem instead of
 * surfacing a bare syntax error.
 */
export function parseResults(raw: string, source: string): BacktestResults {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`${source}: not valid JSON (${(error as Error).message})`);
  }

  if (typeof parsed !== "object" || parsed === null) {
    throw new Error(`${source}: expected a JSON object`);
  }
  const results = parsed as Partial<BacktestResults>;

  // §8: absent or empty banner fails the build. Checked before anything else, so
  // no other validation error can mask the one rule that must never be skipped.
  if (typeof results.banner !== "string" || results.banner.trim() === "") {
    throw new Error(
      `${source}: missing the required non-empty top-level "banner" field. ` +
        "SPEC-BACKTEST.md §8 fails the build rather than render metrics without " +
        "the §1 caveat that says what they do and do not measure.",
    );
  }

  if (results.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `${source}: schema_version ${String(results.schema_version)} is not supported ` +
        `(this build reads version ${SUPPORTED_SCHEMA_VERSION})`,
    );
  }

  for (const field of ["run_date", "window", "coverage", "track_a", "sleeves"] as const) {
    if (results[field] === undefined) {
      throw new Error(`${source}: missing required field "${field}"`);
    }
  }

  return results as BacktestResults;
}

export type LoadedBacktest = { runDate: string; results: BacktestResults };

/** The latest committed run, or null when nothing has been published yet. */
export function loadBacktest(root: string = RESULTS_ROOT): LoadedBacktest | null {
  const runDate = latestRunDate(root);
  if (runDate === null) return null;

  const path = join(root, runDate, "results.json");
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    // A run directory with no results.json is a half-committed artefact, not an
    // absent one — worth failing on rather than silently showing "no run yet".
    throw new Error(`${path}: run directory ${runDate} has no results.json`);
  }
  return { runDate, results: parseResults(raw, path) };
}

/** One track's variants in §5.6's declared order, falling back to key order. */
export function orderedTrack(
  variants: Record<string, TrackStats>,
  order: string[] | undefined,
): TrackStats[] {
  if (!order) return Object.values(variants);
  const listed = order.map((name) => variants[name]).filter((item): item is TrackStats => !!item);
  // Anything the order list does not mention still gets rendered — a variant
  // silently dropped from a results table is worse than one shown out of order.
  const seen = new Set(order);
  return [...listed, ...Object.entries(variants).filter(([k]) => !seen.has(k)).map(([, v]) => v)];
}

/** Points for a lightweight-charts line, dropping samples the curve has no value for. */
export function toLine(dates: string[], values: (number | null)[]) {
  const points: { time: string; value: number }[] = [];
  for (let index = 0; index < dates.length && index < values.length; index += 1) {
    const value = values[index];
    if (value === null || !Number.isFinite(value)) continue;
    points.push({ time: dates[index], value });
  }
  return points;
}
