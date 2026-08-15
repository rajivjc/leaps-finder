/**
 * Hand-written mirror of supabase/migrations (SPEC.md §2).
 *
 * Keep this in sync when a migration changes a table. Once the project exists,
 * `supabase gen types typescript` can replace it wholesale.
 */

export type ScanKind = "full" | "refresh";
export type ScanStatus = "running" | "ok" | "failed";
export type IvRankStatus = "ok" | "warming_up";
export type PositionStatus = "open" | "closed";
export type AlertKind =
  | "trend_break"
  | "stoch_below_20"
  | "premium_stop"
  | "time_exit"
  | "earnings_soon";

export type Ticker = {
  symbol: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  market_cap: number | null;
  avg_volume_30d: number | null;
  updated_at: string | null;
};

export type Scan = {
  id: number;
  kind: ScanKind | null;
  as_of_date: string;
  started_at: string | null;
  finished_at: string | null;
  universe_count: number | null;
  matches_count: number | null;
  status: ScanStatus | null;
  notes: string | null;
};

export type ScanResult = {
  scan_id: number;
  symbol: string;
  spot: number | null;
  pct_off_52w_high: number | null;
  sma50: number | null;
  sma200: number | null;
  stoch_k: number | null;
  stoch_d: number | null;
  stoch_k_prev: number | null;
  turning_up: boolean | null;
  in_zone: boolean | null;
  trend_pass: boolean | null;
  quality_pass: boolean | null;
  valuation_pass: boolean | null;
  iv_pass: boolean | null;
  passes_strict: boolean | null;
  passes_balanced: boolean | null;
  passes_wide: boolean | null;
  s_trend: number | null;
  s_quality: number | null;
  s_option: number | null;
  s_valuation: number | null;
  s_entry: number | null;
  score: number | null;
  op_margin: number | null;
  roe: number | null;
  net_debt_ebitda: number | null;
  rev_growth: number | null;
  fcf_margin: number | null;
  fwd_pe: number | null;
  analyst_target: number | null;
  upside_adj: number | null;
  opt_expiry: string | null;
  opt_strike: number | null;
  opt_dte: number | null;
  opt_delta: number | null;
  opt_mid: number | null;
  opt_bid: number | null;
  opt_ask: number | null;
  opt_spread_pct: number | null;
  opt_oi: number | null;
  opt_iv: number | null;
  breakeven: number | null;
  breakeven_pct: number | null;
  cost_pct_spot: number | null;
  iv30: number | null;
  iv_rank: number | null;
  iv_rank_status: IvRankStatus | null;
  next_earnings: string | null;
  earnings_dte: number | null;
};

export type IvSnapshot = {
  symbol: string;
  snap_date: string;
  iv30: number | null;
  rv20: number | null;
};

export type Position = {
  id: number;
  user_id: string;
  symbol: string;
  opened_on: string;
  expiry: string;
  strike: number;
  contracts: number;
  entry_premium: number;
  account_equity_at_entry: number | null;
  status: PositionStatus | null;
  closed_on: string | null;
  exit_premium: number | null;
  exit_reason: string | null;
  created_at: string | null;
};

export type Alert = {
  id: number;
  position_id: number | null;
  created_at: string | null;
  kind: AlertKind | null;
  message: string | null;
  acknowledged: boolean | null;
};

/** Columns that accept null, and so may be omitted from an insert. */
type NullableKeys<Row> = {
  [K in keyof Row]-?: null extends Row[K] ? K : never;
}[keyof Row];

/**
 * `Generated` names the non-null columns the database fills in itself (identity
 * primary keys). Everything else non-null is required on insert — otherwise a
 * missing NOT NULL column typechecks and fails at runtime as a PostgREST 400.
 */
type TableFor<Row, Generated extends keyof Row = never> = {
  Row: Row;
  Insert: Omit<Row, NullableKeys<Row> | Generated> &
    Partial<Pick<Row, NullableKeys<Row> | Generated>>;
  Update: Partial<Row>;
  Relationships: [];
};

export type Database = {
  public: {
    Tables: {
      tickers: TableFor<Ticker>;
      scans: TableFor<Scan, "id">;
      scan_results: TableFor<ScanResult>;
      iv_snapshots: TableFor<IvSnapshot>;
      positions: TableFor<Position, "id">;
      alerts: TableFor<Alert, "id">;
    };
    Views: Record<never, never>;
    Functions: Record<never, never>;
    Enums: Record<never, never>;
    CompositeTypes: Record<never, never>;
  };
};
