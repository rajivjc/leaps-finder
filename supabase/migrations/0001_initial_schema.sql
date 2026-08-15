-- LEAPS Finder initial schema (SPEC.md §2).
-- All timestamps UTC.

-- Universe of screenable names, refreshed weekly by the full scan.
create table if not exists tickers (
  symbol text primary key,
  name text,
  sector text,
  industry text,
  market_cap numeric,
  avg_volume_30d numeric,
  updated_at timestamptz default now()
);

-- One row per scan run.
create table if not exists scans (
  id bigint generated always as identity primary key,
  kind text check (kind in ('full', 'refresh')),
  as_of_date date not null,            -- Friday of the completed week
  started_at timestamptz,
  finished_at timestamptz,
  universe_count int,
  matches_count int,
  status text default 'running',       -- running|ok|failed
  notes text
);

-- One row per ticker per full scan: every evaluated name, pass or fail.
create table if not exists scan_results (
  scan_id bigint references scans(id),
  symbol text references tickers(symbol),
  spot numeric,
  pct_off_52w_high numeric,
  sma50 numeric,
  sma200 numeric,
  stoch_k numeric,
  stoch_d numeric,
  stoch_k_prev numeric,
  turning_up boolean,
  in_zone boolean,
  trend_pass boolean,
  quality_pass boolean,
  valuation_pass boolean,
  iv_pass boolean,
  passes_strict boolean,
  passes_balanced boolean,
  passes_wide boolean,
  -- factor subscores (raw 0-100) + composite
  s_trend numeric,
  s_quality numeric,
  s_option numeric,
  s_valuation numeric,
  s_entry numeric,
  score numeric,
  -- fundamentals snapshot
  op_margin numeric,
  roe numeric,
  net_debt_ebitda numeric,
  rev_growth numeric,
  fcf_margin numeric,
  fwd_pe numeric,
  analyst_target numeric,
  upside_adj numeric,
  -- option economics (best contract found)
  opt_expiry date,
  opt_strike numeric,
  opt_dte int,
  opt_delta numeric,
  opt_mid numeric,
  opt_bid numeric,
  opt_ask numeric,
  opt_spread_pct numeric,
  opt_oi int,
  opt_iv numeric,
  breakeven numeric,
  breakeven_pct numeric,
  cost_pct_spot numeric,
  iv30 numeric,
  iv_rank numeric,
  iv_rank_status text,                 -- ok|warming_up
  next_earnings date,
  earnings_dte int,
  primary key (scan_id, symbol)
);

-- Daily ATM IV snapshots: the IV-rank bootstrap (SPEC.md §6).
create table if not exists iv_snapshots (
  symbol text,
  snap_date date,
  iv30 numeric,                        -- interpolated ~30d ATM IV
  rv20 numeric,                        -- 20d realized vol (annualized)
  primary key (symbol, snap_date)
);

-- Owner's open/closed positions.
create table if not exists positions (
  id bigint generated always as identity primary key,
  user_id uuid references auth.users not null,
  symbol text not null,
  opened_on date not null,
  expiry date not null,
  strike numeric not null,
  contracts int not null check (contracts > 0),
  entry_premium numeric not null,      -- per share
  account_equity_at_entry numeric,
  status text default 'open',          -- open|closed
  closed_on date,
  exit_premium numeric,
  exit_reason text,
  created_at timestamptz default now()
);

create table if not exists alerts (
  id bigint generated always as identity primary key,
  position_id bigint references positions(id),
  created_at timestamptz default now(),
  kind text,     -- trend_break|stoch_below_20|premium_stop|time_exit|earnings_soon
  message text,
  acknowledged boolean default false
);

-- Access paths the app actually uses: latest scan first, then its rows by score.
create index if not exists scans_as_of_date_idx on scans (as_of_date desc, id desc);
create index if not exists scan_results_scan_score_idx on scan_results (scan_id, score desc);
create index if not exists scan_results_symbol_idx on scan_results (symbol);
create index if not exists iv_snapshots_symbol_date_idx on iv_snapshots (symbol, snap_date desc);
create index if not exists positions_user_status_idx on positions (user_id, status);
create index if not exists alerts_position_idx on alerts (position_id, created_at desc);
