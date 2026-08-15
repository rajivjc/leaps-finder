-- Weekly bar history for the charts (SPEC.md §8.1 sparkline, §8.2 price + stochastic panel).
--
-- The scanner already computes these series on every full scan and reads only the
-- last value out of each; this table keeps the rest. Two reasons it is persisted
-- rather than fetched on demand:
--
--   * §10 requires the screener to load in under a second from precomputed rows,
--     which rules out fetching a time series per result card at request time; and
--   * the (10,3,3) stochastic is pinned in §4, so it gets exactly one
--     implementation. Recomputing it in the web app to draw the same line is how
--     the two quietly disagree.
--
-- Keyed by (symbol, week_ending) rather than by scan: a bar is a fact about the
-- market, not about a scan run, so re-keying per scan would multiply the table by
-- the number of scans for no gain. Each full scan upserts the window it fetched.

create table weekly_bars (
  symbol text references tickers(symbol),
  week_ending date not null,           -- the Friday the completed bar is labelled with
  open numeric, high numeric, low numeric, close numeric, volume numeric,
  slow_k numeric, d numeric,           -- weekly slow stochastic (10,3,3), SPEC.md §4
  -- Daily SMA50/SMA200 sampled at that week's last session. §8.2 overlays the
  -- *daily* averages the §4 trend filter is defined on; a 50-period average of
  -- weekly bars would be a ~1-year line instead, so the daily value is carried
  -- here rather than re-derived from the weekly closes. Null until the average
  -- is defined (the first 200 sessions of the fetched window have no SMA200).
  sma50 numeric, sma200 numeric,
  primary key (symbol, week_ending)
);

-- The primary key already indexes (symbol, week_ending); "latest N weeks for one
-- symbol" is a backward scan of it, so no additional index is needed.

-- RLS, matching the other scan-data tables (SPEC.md §2): readable by anyone with
-- the anon key, writable only by the service key, which bypasses RLS.
alter table weekly_bars enable row level security;

drop policy if exists weekly_bars_public_read on weekly_bars;
create policy weekly_bars_public_read on weekly_bars
  for select using (true);
