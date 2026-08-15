/**
 * Server-side reads (SPEC.md §8: server components, anon key, RLS-limited).
 *
 * Two constraints shape everything here.
 *
 * **Nothing is computed at request time.** §10 requires the screener to load in
 * under a second, and it does that by reading rows the scanner already wrote —
 * no upstream fetching, no re-derivation of anything the scan settled.
 *
 * **PostgREST truncates pages silently.** A response shorter than the range
 * asked for is not proof the data ran out: Supabase's `max-rows` caps a page
 * with a plain HTTP 200. Every multi-row read here therefore pages until it
 * gets an genuinely empty page, and advances by the rows actually received.
 */

import type { PostgrestError } from "@supabase/supabase-js";

import { createReadClient } from "@/lib/supabase";
import type { Scan, ScanResult, Ticker, WeeklyBar } from "@/lib/types";

const PAGE_SIZE = 1000;

/**
 * A backstop against a paging loop that never terminates. It throws rather than
 * returning what it has: a screener quietly missing half its names is a worse
 * failure than one that says it broke.
 */
const MAX_PAGES = 200;

/** Weeks of stochastic history behind each screener card's sparkline. */
export const SPARKLINE_WEEKS = 26;

/** Weeks of candles on the ticker page — SPEC.md §8.2 asks for roughly a year. */
export const DETAIL_WEEKS = 52;

export type Loaded<T> =
  | { state: "unconfigured" }
  | { state: "error"; message: string }
  | { state: "ok"; data: T };

type Page<T> = { data: T[] | null; error: PostgrestError | null };

async function fetchAllPages<T>(
  fetchPage: (from: number, to: number) => PromiseLike<Page<T>>,
): Promise<T[]> {
  const rows: T[] = [];
  let offset = 0;

  for (let page = 0; page < MAX_PAGES; page += 1) {
    const { data, error } = await fetchPage(offset, offset + PAGE_SIZE - 1);
    if (error) throw new Error(error.message);

    const received = data ?? [];
    if (received.length === 0) return rows;

    rows.push(...received);
    offset += received.length;
  }

  throw new Error(`read exceeded ${MAX_PAGES} pages; refusing to return partial data`);
}

/** Run a loader, mapping "no Supabase configured" and thrown errors into states. */
async function load<T>(loader: (client: NonNullable<ReturnType<typeof createReadClient>>) => Promise<T>): Promise<Loaded<T>> {
  const client = createReadClient();
  if (!client) return { state: "unconfigured" };

  try {
    return { state: "ok", data: await loader(client) };
  } catch (error) {
    return { state: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * The most recent scan that finished cleanly.
 *
 * `status = 'ok'` is load-bearing, not decoration: a scan that lost more than a
 * fifth of the universe is recorded `failed` precisely so it cannot surface
 * here looking complete (SPEC.md §9).
 */
async function latestScan(client: NonNullable<ReturnType<typeof createReadClient>>) {
  const { data, error } = await client
    .from("scans")
    .select("*")
    .eq("status", "ok")
    .order("as_of_date", { ascending: false })
    .order("id", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) throw new Error(error.message);
  return data;
}

export type ScreenerData = {
  scan: Scan;
  /** Every name that clears the Wide preset — see below on why that is the whole set. */
  rows: ScanResult[];
  tickers: Record<string, Ticker>;
  /** `symbol -> weekly slow %K, oldest first`, for the card sparklines. */
  sparklines: Record<string, number[]>;
};

/** null data = a configured project with no successful scan yet. */
export async function loadScreener(): Promise<Loaded<ScreenerData | null>> {
  return load(async (client) => {
    const scan = await latestScan(client);
    if (!scan) return null;

    // Wide is the loosest preset, and the tiers nest — every Strict pass is a
    // Balanced pass is a Wide pass (§6: each Wide gate is implied by the
    // stricter tiers'). So the Wide set is the whole screener dataset, and all
    // three tabs filter it client-side without another round trip.
    const rows = await fetchAllPages<ScanResult>((from, to) =>
      client
        .from("scan_results")
        .select("*")
        .eq("scan_id", scan.id)
        .eq("passes_wide", true)
        .order("score", { ascending: false, nullsFirst: false })
        .order("symbol")
        .range(from, to),
    );

    const symbols = rows.map((row) => row.symbol);
    const [tickers, bars] = await Promise.all([
      loadTickersFor(client, symbols),
      loadBarsFor(client, symbols, weeksBefore(scan.as_of_date, SPARKLINE_WEEKS)),
    ]);

    const sparklines: Record<string, number[]> = {};
    for (const bar of bars) {
      // A week whose stochastic is undefined breaks the line rather than
      // pretending to a value; the sparkline draws what it has.
      if (bar.slow_k === null) continue;
      (sparklines[bar.symbol] ??= []).push(bar.slow_k);
    }

    return { scan, rows, tickers, sparklines };
  });
}

export type TickerData = {
  scan: Scan;
  row: ScanResult;
  ticker: Ticker | null;
  bars: WeeklyBar[];
};

/** null data = the symbol was not part of the latest successful scan. */
export async function loadTicker(symbol: string): Promise<Loaded<TickerData | null>> {
  return load(async (client) => {
    const scan = await latestScan(client);
    if (!scan) return null;

    // Not restricted to Wide passes: a name can be reached from /compare or a
    // bookmarked URL, and "why did this one fail?" is exactly what the filter
    // checklist on this page is for.
    const { data: row, error } = await client
      .from("scan_results")
      .select("*")
      .eq("scan_id", scan.id)
      .eq("symbol", symbol)
      .maybeSingle();

    if (error) throw new Error(error.message);
    if (!row) return null;

    const [tickers, bars] = await Promise.all([
      loadTickersFor(client, [symbol]),
      loadBarsFor(client, [symbol], weeksBefore(scan.as_of_date, DETAIL_WEEKS)),
    ]);

    return { scan, row, ticker: tickers[symbol] ?? null, bars };
  });
}

export type CompareData = {
  scan: Scan;
  rows: ScanResult[];
  tickers: Record<string, Ticker>;
};

/**
 * Every evaluated name in the latest scan, for /compare's picker.
 *
 * Unlike the screener this is not restricted to Wide passes — comparing a
 * candidate against a name that just missed a gate is the point of the page.
 */
export async function loadCompare(): Promise<Loaded<CompareData | null>> {
  return load(async (client) => {
    const scan = await latestScan(client);
    if (!scan) return null;

    const rows = await fetchAllPages<ScanResult>((from, to) =>
      client
        .from("scan_results")
        .select("*")
        .eq("scan_id", scan.id)
        .order("score", { ascending: false, nullsFirst: false })
        .order("symbol")
        .range(from, to),
    );

    const tickers = await loadTickersFor(
      client,
      rows.map((row) => row.symbol),
    );
    return { scan, rows, tickers };
  });
}

async function loadTickersFor(
  client: NonNullable<ReturnType<typeof createReadClient>>,
  symbols: string[],
): Promise<Record<string, Ticker>> {
  if (symbols.length === 0) return {};

  const rows = await fetchAllPages<Ticker>((from, to) =>
    client.from("tickers").select("*").in("symbol", symbols).order("symbol").range(from, to),
  );

  return Object.fromEntries(rows.map((row) => [row.symbol, row]));
}

async function loadBarsFor(
  client: NonNullable<ReturnType<typeof createReadClient>>,
  symbols: string[],
  since: string,
): Promise<WeeklyBar[]> {
  if (symbols.length === 0) return [];

  return fetchAllPages<WeeklyBar>((from, to) =>
    client
      .from("weekly_bars")
      .select("*")
      .in("symbol", symbols)
      .gte("week_ending", since)
      .order("symbol")
      .order("week_ending")
      .range(from, to),
  );
}

/**
 * The Friday `weeks` weekly bars before `isoDate`, as a date string.
 *
 * Bars are labelled by their `W-FRI` Friday whether or not that Friday traded,
 * so stepping back in exact 7-day multiples lands on real labels. UTC at both
 * ends: local-time parsing of a date-only column shifts the day west of London.
 */
export function weeksBefore(isoDate: string, weeks: number): string {
  const start = new Date(`${isoDate}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - 7 * (weeks - 1));
  return start.toISOString().slice(0, 10);
}
