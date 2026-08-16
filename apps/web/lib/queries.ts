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

import { createReadClient, createSessionClient } from "@/lib/supabase";
import type {
  Alert,
  Position,
  PositionMark,
  Scan,
  ScanResult,
  Ticker,
  WeeklyBar,
} from "@/lib/types";

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
async function load<T>(loader: (client: Client) => Promise<T>): Promise<Loaded<T>> {
  const client = createReadClient();
  if (!client) return { state: "unconfigured" };

  try {
    return { state: "ok", data: await loader(client) };
  } catch (error) {
    return { state: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

type Client = NonNullable<ReturnType<typeof createReadClient>>;

/**
 * The most recent scan of one kind that finished cleanly.
 *
 * Both filters are load-bearing.
 *
 * `status = 'ok'`: a scan that lost more than a fifth of the universe is
 * recorded `failed` precisely so it cannot surface here looking complete
 * (SPEC.md §9).
 *
 * `kind`: the daily refresh writes `scans` rows too, and it writes them five
 * times a week against the weekly scan's one. A refresh has no `scan_results`
 * behind it — it does not rescore anything — so without this filter the newest
 * "successful scan" would almost always be a refresh and every page that reads
 * rows for it would come back empty.
 */
async function latestScan(client: Client, kind: "full" | "refresh" = "full") {
  const { data, error } = await client
    .from("scans")
    .select("*")
    .eq("kind", kind)
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
      loadBarsFor(
        client,
        symbols,
        weeksBefore(scan.as_of_date, SPARKLINE_WEEKS),
        scan.as_of_date,
      ),
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
      loadBarsFor(
        client,
        [symbol],
        weeksBefore(scan.as_of_date, DETAIL_WEEKS),
        scan.as_of_date,
      ),
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

/**
 * How far back /positions reads its alert history. Bounded because the table
 * grows daily, generous because an alert left unacknowledged for months is
 * exactly the one the owner most needs to still see.
 */
export const ALERT_HISTORY_DAYS = 365;

/**
 * The signed-in owner's saved equity, for the size calculator on the public
 * ticker page (SPEC.md §7: "persisted per user").
 *
 * Returns nulls rather than a state union: this is a nicety on a page that
 * works fine without it, and a signed-out visitor is the normal case, not an
 * error worth rendering.
 */
export async function loadOwnerEquity(): Promise<{ signedIn: boolean; equity: number | null }> {
  const client = await createSessionClient();
  if (!client) return { signedIn: false, equity: null };

  try {
    const {
      data: { user },
    } = await client.auth.getUser();
    if (!user) return { signedIn: false, equity: null };

    const { data } = await client
      .from("user_settings")
      .select("account_equity")
      .eq("user_id", user.id)
      .maybeSingle();

    return { signedIn: true, equity: data?.account_equity ?? null };
  } catch {
    return { signedIn: false, equity: null };
  }
}

export type PositionsData = {
  user: { id: string; email: string | null };
  positions: Position[];
  /**
   * Marks keyed by position, all drawn from a single refresh run — see
   * `loadPositions`. A position absent from this map has no mark from that run.
   */
  marks: Record<number, PositionMark>;
  /** The refresh the marks came from; null before the daily job has ever run. */
  refresh: Scan | null;
  alerts: Alert[];
  tickers: Record<string, Ticker>;
  /** SPEC.md §7's current account equity, or null until the owner enters one. */
  equity: number | null;
};

/**
 * Everything /positions renders (SPEC.md §8.4). `null` data = not signed in.
 *
 * The marks are the part worth reading twice. They are selected by
 * `scan_id = <the latest successful refresh>`, not by "most recent mark per
 * position" — so every P&L, every exposure meter and every premium-stop
 * distance on the page describes the same trading day, and the page can name
 * that day in its header. Taking each position's newest mark independently
 * would quietly mix runs whenever one holding failed to price, which is the
 * same defect as a chart drawn from a scan the page is not showing.
 */
export async function loadPositions(): Promise<Loaded<PositionsData | null>> {
  const client = await createSessionClient();
  if (!client) return { state: "unconfigured" };

  try {
    const {
      data: { user },
    } = await client.auth.getUser();
    if (!user) return { state: "ok", data: null };

    const positions = await fetchAllPages<Position>((from, to) =>
      client
        .from("positions")
        .select("*")
        .order("opened_on", { ascending: false })
        .order("id", { ascending: false })
        .range(from, to),
    );

    const refresh = await latestScan(client, "refresh");
    const openIds = positions.filter((row) => row.status !== "closed").map((row) => row.id);

    const since = new Date(Date.now() - ALERT_HISTORY_DAYS * 86_400_000)
      .toISOString()
      .slice(0, 10);

    const [markRows, alerts, tickers, settings] = await Promise.all([
      refresh && openIds.length > 0
        ? fetchAllPages<PositionMark>((from, to) =>
            client
              .from("position_marks")
              .select("*")
              .eq("scan_id", refresh.id)
              .in("position_id", openIds)
              .order("position_id")
              .range(from, to),
          )
        : Promise.resolve([]),
      fetchAllPages<Alert>((from, to) =>
        client
          .from("alerts")
          .select("*")
          .gte("created_at", since)
          .order("created_at", { ascending: false })
          .order("id", { ascending: false })
          .range(from, to),
      ),
      loadTickersFor(
        client,
        positions.map((row) => row.symbol),
      ),
      client.from("user_settings").select("*").eq("user_id", user.id).maybeSingle(),
    ]);

    if (settings.error) throw new Error(settings.error.message);

    return {
      state: "ok",
      data: {
        user: { id: user.id, email: user.email ?? null },
        positions,
        marks: Object.fromEntries(markRows.map((mark) => [mark.position_id, mark])),
        refresh,
        alerts,
        tickers,
        equity: settings.data?.account_equity ?? null,
      },
    };
  } catch (error) {
    return { state: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

async function loadTickersFor(
  client: Client,
  symbols: string[],
): Promise<Record<string, Ticker>> {
  if (symbols.length === 0) return {};

  const rows = await fetchAllPages<Ticker>((from, to) =>
    client.from("tickers").select("*").in("symbol", symbols).order("symbol").range(from, to),
  );

  return Object.fromEntries(rows.map((row) => [row.symbol, row]));
}

/**
 * Bars for a set of symbols, bounded at both ends.
 *
 * The upper bound is the load-bearing one. `weekly_bars` is keyed by
 * (symbol, week_ending) rather than by scan, and the scanner writes it before
 * `finish_scan` decides whether the run counts — so a scan that then fails
 * §9's 20% ceiling still leaves its bars behind. These pages deliberately show
 * the latest *successful* scan, so without `until` the chart would draw a
 * candle for a week the header, checklist and economics beside it know nothing
 * about. Clamping to the displayed scan's `as_of_date` keeps the page one
 * self-consistent statement about one week.
 */
async function loadBarsFor(
  client: Client,
  symbols: string[],
  since: string,
  until: string,
): Promise<WeeklyBar[]> {
  if (symbols.length === 0) return [];

  return fetchAllPages<WeeklyBar>((from, to) =>
    client
      .from("weekly_bars")
      .select("*")
      .in("symbol", symbols)
      .gte("week_ending", since)
      .lte("week_ending", until)
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
