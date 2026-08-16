# LEAPS Finder — Build Specification v1.0

A web app that screens US large-cap stocks for LEAPS call entries using a five-filter
strategy (trend, weekly stochastic, quality, valuation/upside, low IV), computes the
economics of a ~1-year 0.70-delta call for each match, ranks candidates with a
transparent score, and enforces position-sizing / exit discipline.

Personal project for RC. Public portfolio repo. Not financial advice; the app must say so.

---

## 0. Final decisions (locked)

| Item | Decision |
|---|---|
| Repo name | `leaps-finder` (public, GitHub) |
| Frontend | Next.js 14+ (App Router) + Tailwind, deployed on Vercel |
| DB | Supabase Postgres (free tier) |
| Scanner | Python 3.11+, runs in GitHub Actions cron; identical script runnable locally |
| Data source | yfinance (free). No scraping workarounds beyond yfinance's own client |
| Universe | US-listed common stocks, market cap ≥ $50B, optionable (~250 names) |
| Auth | Supabase Auth (magic link). Single allow-listed email (owner) can write positions; scan results publicly readable |
| Scope v1 | Screener + LEAP economics + score + positions/exit monitor + size calculator |
| Deferred | Backtesting (v1.5), email alerts, multi-user |

---

## 1. Repo layout (monorepo)

```
leaps-finder/
├── apps/web/                  # Next.js app (Vercel root)
│   ├── app/                   # App Router pages
│   ├── components/
│   ├── lib/supabase.ts        # client factory (anon key)
│   └── ...
├── scanner/                   # Python package
│   ├── pyproject.toml
│   ├── leaps_scanner/
│   │   ├── universe.py        # build/refresh ticker universe
│   │   ├── prices.py          # OHLCV fetch + weekly resample
│   │   ├── indicators.py      # SMA, stochastic
│   │   ├── fundamentals.py    # quality/valuation inputs
│   │   ├── options.py         # chain fetch, BS delta, contract selection
│   │   ├── scoring.py         # filters, subscores, composite
│   │   ├── risk.py            # exit-signal evaluation for positions
│   │   ├── db.py              # Supabase writes (supabase-py, service key)
│   │   └── run_scan.py        # entrypoints: full_scan / daily_refresh
│   └── tests/
├── supabase/
│   └── migrations/            # SQL schema, RLS policies
├── .github/workflows/
│   ├── scan-weekly.yml        # Sat 02:00 UTC (10:00 SGT)
│   ├── refresh-daily.yml      # Tue–Sat 22:30 UTC (after US close + settle)
│   └── ci.yml                 # lint + tests on PR (python + web)
├── README.md                  # portfolio-grade: architecture diagram, screenshots, disclaimers
└── SPEC.md                    # this file
```

---

## 2. Database schema (Supabase)

All timestamps UTC. Migrations in `supabase/migrations/`.

```sql
-- universe, refreshed weekly
create table tickers (
  symbol text primary key,
  name text, sector text, industry text,
  market_cap numeric, avg_volume_30d numeric,
  updated_at timestamptz default now()
);

-- one row per scan run
create table scans (
  id bigint generated always as identity primary key,
  kind text check (kind in ('full','refresh')),
  as_of_date date not null,            -- Friday of the completed week
  started_at timestamptz, finished_at timestamptz,
  universe_count int, matches_count int,
  status text default 'running',       -- running|ok|failed
  notes text
);

-- one row per ticker per full scan (all evaluated names, pass or fail)
create table scan_results (
  scan_id bigint references scans(id),
  symbol text references tickers(symbol),
  spot numeric, pct_off_52w_high numeric,
  sma50 numeric, sma200 numeric,
  stoch_k numeric, stoch_d numeric, stoch_k_prev numeric,
  turning_up boolean, in_zone boolean, trend_pass boolean,
  quality_pass boolean, valuation_pass boolean, iv_pass boolean,
  passes_strict boolean, passes_balanced boolean, passes_wide boolean,
  -- factor subscores (raw 0–100) + composite
  s_trend numeric, s_quality numeric, s_option numeric,
  s_valuation numeric, s_entry numeric, score numeric,
  -- fundamentals snapshot
  op_margin numeric, roe numeric, net_debt_ebitda numeric,
  rev_growth numeric, fcf_margin numeric, fwd_pe numeric,
  analyst_target numeric, upside_adj numeric,
  -- option economics (best contract found)
  opt_expiry date, opt_strike numeric, opt_dte int, opt_delta numeric,
  opt_mid numeric, opt_bid numeric, opt_ask numeric,
  opt_spread_pct numeric, opt_oi int, opt_iv numeric,
  breakeven numeric, breakeven_pct numeric, cost_pct_spot numeric,
  iv30 numeric, iv_rank numeric, iv_rank_status text,  -- ok|warming_up
  next_earnings date, earnings_dte int,
  primary key (scan_id, symbol)
);

-- daily ATM IV snapshots (the IV-rank bootstrap)
create table iv_snapshots (
  symbol text, snap_date date,
  iv30 numeric,                 -- interpolated ~30d ATM IV
  rv20 numeric,                 -- 20d realized vol (annualized)
  primary key (symbol, snap_date)
);

-- owner's open/closed positions
create table positions (
  id bigint generated always as identity primary key,
  user_id uuid references auth.users not null,
  symbol text not null,
  opened_on date not null,
  expiry date not null, strike numeric not null,
  contracts int not null check (contracts > 0),
  entry_premium numeric not null,       -- per share
  account_equity_at_entry numeric,
  status text default 'open',           -- open|closed
  closed_on date, exit_premium numeric, exit_reason text,
  created_at timestamptz default now()
);

create table alerts (
  id bigint generated always as identity primary key,
  position_id bigint references positions(id),
  created_at timestamptz default now(),
  kind text,      -- trend_break|stoch_below_20|premium_stop|time_exit|earnings_soon
  message text,
  acknowledged boolean default false
);
```

**RLS:** `tickers`, `scans`, `scan_results`, `iv_snapshots`: public `select`, no public writes
(scanner uses service key, bypasses RLS). `positions`, `alerts`: owner-only (`auth.uid() = user_id`;
alerts via join). Signup restricted: allow-list the owner's email via auth hook or simply
disable public signups in Supabase settings.

---

## 3. Scanner pipeline (`run_scan.py full_scan`)

Runs Saturday 02:00 UTC in GitHub Actions; also runnable locally (`python -m leaps_scanner.run_scan full`).

1. **Universe.** Start from a bundled seed list of US large caps (S&P 500 constituents CSV
   checked into repo as fallback). For each, pull `market_cap`, `avg_volume_30d` via yfinance;
   keep `market_cap ≥ 50e9`. Upsert `tickers`.
2. **Prices & indicators.** Fetch ~2y daily OHLCV per symbol (batched, throttled: ≤5 req/s,
   retry with exponential backoff, `curl_cffi` session). Compute daily SMA50/SMA200 and
   weekly bars (resample `W-FRI`, completed weeks only) → stochastic per §4.
3. **Fundamentals.** From `yf.Ticker.info` / `financials`: operating margin, ROE,
   net debt / EBITDA, revenue growth (TTM YoY), FCF margin, forward P/E, analyst mean target,
   next earnings date.
4. **Options.** Per §5: select ~1-year 0.70Δ call, record economics + liquidity.
5. **IV snapshot.** Compute IV30 (nearest-to-30d ATM call/put IV, interpolated) + RV20;
   append to `iv_snapshots`. IV rank per §6.
6. **Score & filters.** Per §6–7. Write every evaluated symbol to `scan_results`.
7. **Exit monitor.** For each open position, evaluate exit rules (§8); insert `alerts`.
8. **Bookkeeping.** Update `scans` row; fail loudly (non-zero exit) so Actions shows red.

`run_scan.py refresh` (Tue–Sat 22:30 UTC): updates `tickers` quotes, appends `iv_snapshots`,
evaluates **daily** exit rules for open positions (premium stop needs daily marks), and keeps
the Supabase project from pausing. No rescoring.

---

## 4. Signal definitions (exact)

**Weekly bars:** resample daily OHLC to `W-FRI`; drop the current in-progress week —
signals only ever use completed weekly bars (no repaint).

**Weekly slow stochastic (10, 3, 3):**
```
rawK_t = 100 * (C_t − min(L, 10w)) / (max(H, 10w) − min(L, 10w))
slowK  = SMA_3(rawK)
D      = SMA_3(slowK)
```
- `in_zone`     : 20 ≤ slowK ≤ 70
- `turning_up`  : slowK > D  AND  slowK > slowK[−1]
- Exit signal   : slowK crosses below 20 (weekly close)

**Trend (daily, evaluated at Friday close):**
- `trend_pass` : close > SMA50 AND close > SMA200 AND SMA50 > SMA200
- Trend break (exit): close < SMA200 OR SMA50 < SMA200 (daily close confirmed)

---

## 5. LEAP contract selection & economics

1. Expiries: all with DTE ≥ 350; prefer the nearest expiry ≥ 350d (Jan LEAPS tolerance);
   if none, use the longest available and flag `opt_dte < 350`.
2. For each call in that expiry with `bid > 0` and `ask > 0`: compute Black-Scholes delta
   using the contract's yfinance `impliedVolatility`, `r` = 13-week T-bill (^IRX/100),
   `q` = trailing dividend yield:
   `d1 = (ln(S/K) + (r − q + σ²/2)T) / (σ√T)`, `delta = e^(−qT) · N(d1)`.
3. Pick strike with delta nearest **0.70**.
4. Economics: `mid=(bid+ask)/2`, `breakeven = K + mid`, `breakeven_pct = breakeven/S − 1`,
   `cost_pct_spot = mid/S`, `spread_pct = (ask−bid)/mid`, record `oi`, contract `iv`.
5. Cushion vs target (shown honestly): `(target_adj − breakeven)/breakeven` where
   `target_adj` uses the §6 haircut. Denominator is breakeven, not spot.

---

## 6. Scoring engine

All subscores raw 0–100. **Composite = 0.25·Trend + 0.25·Quality + 0.20·OptionEcon +
0.15·Valuation + 0.15·Entry.** Displayed as-is — no curves, no rescaling. Helper
`clip_map(x, x0, x1) → 0..100` is piecewise-linear.

- **Trend (25):** mean of: `clip_map(close/sma200 − 1, 0, 0.25)`;
  `clip_map(sma50/sma200 − 1, 0, 0.10)`; % of last 60 sessions closing above SMA50
  (`clip_map(share, 0.5, 1.0)`); **extension penalty**: multiply by
  `clip_map(0.20 − (close/sma50 − 1), 0, 0.10)/100` capped at 1 (i.e. >20% above the
  50-day scores 0 on this gate; parabolic ≠ better).
- **Quality (25):** mean of `clip_map(op_margin, 0.05, 0.30)`, `clip_map(roe, 0.08, 0.30)`,
  `clip_map(3 − net_debt_ebitda, 0, 3)` (net cash ⇒ 100), `clip_map(rev_growth, 0, 0.20)`,
  `clip_map(fcf_margin, 0, 0.20)`. Missing metric ⇒ excluded from mean, flagged in UI.
- **Option economics (20):** mean of `clip_map(50 − iv_rank, 0, 50)`,
  `clip_map(0.40 − iv30, 0, 0.25)`, `clip_map(0.30 − cost_pct_spot, 0, 0.15)`,
  `clip_map(0.10 − spread_pct, 0, 0.08)`, `clip_map(oi, 100, 2000)`.
- **Valuation & upside (15):** `upside_adj = 0.6 · (analyst_target/S − 1)` (40% haircut);
  mean of `clip_map(upside_adj, 0, 0.25)` and cross-sectional forward-P/E percentile within
  the scan universe inverted (`cheaper ⇒ higher`).
- **Entry (15):** mean of zone position — peak at slowK 25–45, linear to 0 at 20 and 70 —
  and freshness: 100 if slowK crossed above D within last 2 completed weeks, 60 within 4, else 30.

**IV rank:** `(iv30 − min(iv30, 252d)) / (max − min)` from our own `iv_snapshots`. Until a
symbol has ≥ 120 snapshots: `iv_rank_status = 'warming_up'` and substitute the cross-sectional
percentile of `iv30/rv20` for the IV-rank subscore. UI must show the warming-up badge.

**Presets (hard filters, applied before ranking):**

| Filter | Strict | Balanced | Wide open |
|---|---|---|---|
| Trend pass | required | required | required |
| slowK zone | 20–55 + turning up | 20–70 + turning up | 10–80 |
| IV rank | ≤ 30 | ≤ 50 | — |
| Earnings distance | ≥ 30d | ≥ 14d | — |
| Spread % of mid | ≤ 5% | ≤ 8% | ≤ 12% |
| Open interest | ≥ 500 | ≥ 200 | ≥ 100 |
| Quality | all metrics present & Quality ≥ 60 | Quality ≥ 45 | — |

---

## 7. Risk engine & exit monitor

**Position size calculator** (ticker page + positions page): inputs `account_equity`
(persisted per user) → `max_premium_dollars = 0.03 · equity`;
`max_contracts = floor(max_premium_dollars / (mid · 100))`. Also show current exposure:
open premium / equity vs the 15% cap, per-sector counts vs the 2-per-sector cap, and count vs
5-position cap. Block-level warnings, not hard blocks (user may override; log it).

**Exit rules evaluated by scanner** (weekly full scan + daily refresh where noted):

| Rule | Check | Cadence |
|---|---|---|
| Trend break | close < SMA200 or SMA50 < SMA200 | daily |
| Stochastic | weekly slowK crosses < 20 | weekly |
| Premium stop | current mid ≤ 50% of entry premium | daily |
| Time exit | DTE < 180 | daily |
| Earnings heads-up | earnings within 21d (informational) | daily |
| Circuit breaker | realized+unrealized sleeve loss ≥ 8% of entry equity → banner "no new entries 4 weeks" | daily |

Alerts are rows in `alerts`, surfaced as a banner + list in the UI. (Email via Resend = v1.5.)

---

## 8. Frontend (Next.js on Vercel)

Server components read Supabase with the anon key. Pages:

1. **/ (Screener).** Preset tabs (Strict / Balanced / Wide open) + advanced-filter drawer
   (stoch bounds, IV rank, cap, spread, OI, sector, earnings distance). Results as cards:
   symbol, name, price, % off 52w high, composite score with factor mini-bars, weekly-stoch
   sparkline, earnings badge (≤21d, amber), IV-rank badge (`warming up` where applicable),
   LEAP one-liner (strike/DTE/breakeven%/cost%). "As of <Friday date> · data: Yahoo Finance,
   delayed" always visible.
2. **/t/[symbol] (Ticker detail).** Price chart (~1y weekly candles + SMA50/200 overlay,
   lightweight-charts), stochastic panel with 20/70 band, filter checklist (5 green/red rows
   with reasons), LEAP economics card (all §5 numbers incl. honest cushion), score breakdown
   (raw factor bars — show the raw number, this is the anti-TradeformIQ feature),
   size calculator.
3. **/compare.** Up to 4 tickers side-by-side: factor bars + underlying data table
   (mirrors the video's compare, with raw scores).
4. **/positions** (auth-required). CRUD open positions, sleeve exposure meters
   (15% cap, sector counts, position count), alerts list with acknowledge, circuit-breaker banner.
5. **/about.** Strategy explanation, every formula in this spec, data caveats,
   full disclaimer ("educational tool, not financial advice; delayed data; do your own research").

Charts: `lightweight-charts` (TradingView OSS). Styling: Tailwind; clean light theme;
no score-inflating visual tricks (no green-at-70 thresholds; neutral color below 60).

---

## 9. GitHub Actions

- **scan-weekly.yml:** `cron: '0 2 * * SAT'` → `full_scan`. Timeout 45 min. On failure:
  Actions badge red (badge in README).
- **refresh-daily.yml:** `cron: '30 22 * * 2-6'` → `refresh` (Tue–Sat UTC = Mon–Fri US close).
  Doubles as Supabase keep-alive.
- **ci.yml:** on PR/push — `ruff` + `pytest` for scanner; `eslint` + `tsc --noEmit` +
  `next build` for web.
- Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (Actions); `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY` (Vercel). Never in code — repo is public.
- yfinance resilience: batched downloads, ≤5 req/s, exponential backoff w/ jitter, 3 retries;
  if >20% of universe fails, mark scan `failed` (partial data must not look like a full scan).
  Document local fallback: `make scan` runs the identical job from a laptop.

---

## 10. Testing & acceptance

- **Unit (must):** stochastic (10,3,3) against hand-computed fixture; BS delta vs known values
  (e.g. σ=0.30, T=1, r=0.04: verify against reference); scoring `clip_map` edge cases;
  contract selection picks nearest-0.70Δ with valid bid/ask; preset filter logic;
  exit-rule evaluation incl. crossing semantics (crosses below 20 ≠ is below 20).
- **Integration:** scanner E2E against recorded yfinance fixtures (vcr-style JSON), asserting
  a full `scan_results` row matches a golden file.
- **Acceptance for v1:** weekly scan completes < 30 min for ~250 names; screener loads < 1s
  (reads precomputed rows only); TSM-like validation — for a known ticker, breakeven, cost %,
  spread math reproduce hand calculations to the cent; positions flow: create → alert fires on
  simulated trend break → acknowledge.

---

## 11. Milestones (suggested Claude Code sessions)

1. **M1 Scaffold:** monorepo, Supabase migrations + RLS, Next.js shell w/ Supabase client,
   CI green, README skeleton.
2. **M2 Scanner core:** universe, prices, indicators, unit tests; write `scan_results`
   (no options yet); first Actions run green.
3. **M3 Options + scoring:** chain selection, BS delta, IV snapshots, scoring, presets;
   golden-file test.
4. **M4 Frontend:** screener, ticker detail, compare, about.
5. **M5 Risk:** auth, positions CRUD, size calculator, exit monitor + alerts, daily refresh job.
6. **M6 Polish:** README with architecture diagram + screenshots, Vercel deploy, disclaimers,
   Actions badges.

## 12. Post-v1 scope

Backtesting is specified as **v2** in [SPEC-BACKTEST.md](SPEC-BACKTEST.md) (its own
source-of-truth document; decisions locked 2026-08-16). Still parked: email alerts via
Resend; IV-rank graduation once 252 snapshots accumulate.

---

*Disclaimer: educational/personal tooling. Yahoo Finance data is delayed and unofficial.
Nothing in this application constitutes financial advice.*
