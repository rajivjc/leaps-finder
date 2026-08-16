# LEAPS Finder — Backtest Specification (v2)

A historical evaluation of the stock-level entry/exit signal over 10 years of daily data,
with an approximate synthetic-LEAP P&L overlay and a simulation of §7's sleeve discipline.
Reports CAGR, max drawdown, win rate, and comparisons against buy-and-hold.

This document is the source of truth for v2, with the same authority CLAUDE.md grants
SPEC.md: formulas are pinned exactly as written; anything ambiguous comes back to RC
rather than being improvised. Where this spec cites SPEC.md (§4 signals, §5 economics,
§7 risk rules), those definitions apply verbatim — the backtest must reuse the same code,
not reimplement it.

**Honesty rule, inherited and extended:** every approximation in this backtest is listed
in the bias register (§7 below) with its expected direction. No result is displayed —
in the report or the web page — without its caveats. A smaller honest result beats a
larger fake one.

---

## 0. Decisions locked (RC, 2026-08-16)

| # | Question | Decision |
|---|---|---|
| 1 | Name & scope | **v2 = backtesting only.** Resend email alerts and IV-rank graduation remain parked (SPEC.md §12); they share nothing with this work. |
| 2 | Entry signal | **Trend + stochastic only** (§4 definitions). Quality, valuation, IV, and earnings-distance filters are *not* backtested — historical fundamentals cannot be reconstructed from yfinance without look-ahead bias. The report says this plainly. |
| 3 | Survivorship | **Point-in-time S&P 500 membership** from a public change-history dataset checked into the repo. Unfetchable delisted names are logged and counted; the coverage gap is reported as residual bias. |
| 4 | Option layer | **Stock-level signal is the headline result.** The synthetic-LEAP overlay is a clearly-labeled approximation layer — synthesized 0.70Δ strike, no liquidity gates, not comparable to the live screener. |
| 5 | Repricing σ | **σ = trailing 252-session realized vol at entry × 1.1**, held flat for the trade's life. Sensitivity runs at 1.0× and 1.2×. |
| 6 | Sleeve | **Per-trade statistics primary; sleeve simulation secondary**, with §7's full discipline (3% sizing, caps, circuit breaker) and a pinned tie-break rule. |
| 7 | Benchmark | **Both**: SPY total-return buy-and-hold, and equal-weight buy-and-hold of the same entered names over the same windows. |
| 8 | Spec home | **This document** (`SPEC-BACKTEST.md`, repo root). SPEC.md §12 points here; the v1 spec otherwise stays frozen. |

### 0.1 Decisions proposed in this draft (not covered by the eight questions — approve or amend)

| # | Choice | Pinned as |
|---|---|---|
| P1 | Execution timing | Signals at close → fills at **next session's open** (§4.3). |
| P2 | Price basis | `auto_adjust=False` raw Close/Open — **identical to the live scanner** (`prices.py`), split-adjusted, dividend-unadjusted. Benchmarks use Adj Close (total return) where stated. |
| P3 | Synthetic tenor | Fixed **T₀ = 365 calendar days** at entry; time exit at DTE < 180 ⇒ max hold 185 days. |
| P4 | Strike | Exact closed-form 0.70Δ strike (§5.2), no strike grid. |
| P5 | Friction | Fill haircut **h = 0.04** of model value each way (half the Balanced preset's 8% spread cap). Sensitivity at 0 and 0.06. |
| P6 | Dividend yield q | Trailing 365-day cash dividends ÷ entry spot, held flat (mirrors §5's q). |
| P7 | Rate r | ^IRX/100 on the entry date, held flat for the trade. |
| P8 | Trade granularity | One open simulated trade per symbol per track; re-entry only after exit. |
| P9 | Sleeve notional | E₀ = $100,000; integer contracts; cash earns 0%. Sensitivity at E₀ = $250,000. |
| P10 | Tie-break | When entry signals exceed open sleeve slots: ascending `|slowK − 35|` (the §6 Entry-subscore sweet spot), then alphabetical. Deterministic. |
| P11 | Circuit breaker semantics | Mirror the v1 implementation in `risk.py`: trailing-28-day realized + current unrealized P&L, denominator = equity snapshot at the most recent entry, trips at ≥ 8%, latches 28 days. |
| P12 | Where results live | Committed report under `docs/backtest/<run-date>/` (`report.md`, `results.json`, SVG equity curves) + a `/backtest` web page rendering the JSON. Manual local runs only — no cron. |
| P13 | Entry zone | Primary entry uses the §4 base stochastic (20–70 + turning up). The Strict zone (20–55) is reported as a variant, not the headline. |

---

## 1. What this backtest can and cannot claim

**It evaluates:** the §4 timing signal — daily trend template plus weekly slow stochastic —
as an entry/exit discipline on S&P 500 members, and approximately what that timing is worth
when expressed through a ~1-year 0.70Δ call under stated assumptions.

**It does not evaluate:**
- the quality, valuation, IV-rank, or earnings-distance filters (no point-in-time data);
- the preset liquidity gates (spread %, OI — no historical chains);
- the live screener's ranking/score (§6 needs fundamentals);
- real option fills, IV dynamics, or early-exercise/assignment effects.

Therefore: **a good backtest result validates the timing component only.** It is evidence
about two of the five filters. The live strategy could still underperform (bad fundamental
filters, unmodeled IV crush) or outperform (the untested filters may add value). The report
and the `/backtest` page must carry this paragraph's substance verbatim as a banner.

---

## 2. Universe (point-in-time)

1. **Membership dataset.** `scanner/leaps_scanner/backtest/data/sp500_membership.csv`,
   columns `symbol, added, removed` (`removed` empty = still a member), compiled from the
   change history on Wikipedia's *List of S&P 500 companies* (which tracks S&P press
   releases). The CSV header comment records the retrieval date and source URL. Known
   imperfection: the change table is community-maintained; errors are possible and are
   listed in the bias register.
2. **Membership rule.** A symbol is in the universe on date *d* iff `added ≤ d` and
   (`removed` empty or `d < removed`). Inclusive of the addition date, exclusive of the
   removal date.
3. **Symbol normalization.** Class shares map to yfinance form (`BRK.B → BRK-B`,
   `BF.B → BF-B`). Renames/re-tickers are handled by rows in the CSV (old symbol removed,
   new symbol added on the effective date).
4. **Coverage accounting.** For every member, attempt the full price history. A
   member-week is *covered* if OHLCV exists for that week while the symbol was a member.
   The report must state: coverage ratio (covered member-weeks ÷ total member-weeks),
   count of members with no data at all, and the list of uncovered symbols with their
   membership spans. **No coverage threshold is faked**: if coverage < 85%, every headline
   table carries an explicit low-coverage warning.
5. **Delisting mid-trade.** If a simulated position's price history ends (acquisition,
   bankruptcy, delisting), the trade is force-exited at the last available close,
   exit reason `delisted`. These trades stay in the statistics and their count is reported.
6. **No market-cap filter.** The live $50B floor is dropped — historical caps are not
   reliably reconstructable, and S&P 500 membership is already a large-cap proxy. Stated
   in the bias register.

---

## 3. Data

1. **Window.** Evaluation window = the 10 years ending at the last completed `W-FRI` week
   before the run date. Fetch extends ~18 months earlier so SMA200, RV252, and the weekly
   stochastic are fully warmed up at the first evaluation date. A symbol becomes eligible
   on the first date all three are defined.
2. **Prices.** Daily OHLCV via yfinance with `auto_adjust=False` — the same basis as the
   live scanner (`prices.py`). `Close`/`Open` are split-adjusted, dividend-unadjusted.
   `Adj Close` is also retained, used *only* for total-return benchmarks (§6.3).
3. **Fetch discipline.** Identical to SPEC.md §9: batched downloads, ≤ 5 req/s,
   exponential backoff with jitter, 3 retries, empty-DataFrame treated as failure
   (yfinance fails silently). Reuses the `prices.py` downloader machinery.
4. **Cache.** Fetched data is cached locally (Parquet, in a git-ignored directory).
   All computation runs from the cache; a run records the cache snapshot date. Raw price
   data is never committed (size; Yahoo terms).
5. **Auxiliary series.** ^IRX daily history (for r); per-symbol dividend history
   (for q). Both cached alongside prices.
6. **Sector labels.** Current GICS sector per symbol from the v1 `tickers` table /
   yfinance, applied historically (2-per-sector cap only). Minor look-ahead; in the
   bias register.

---

## 4. Signal reconstruction (exact)

### 4.1 Indicators — reuse, don't reimplement

SMA50/SMA200, `W-FRI` weekly resampling with the in-progress week dropped, and the
(10, 3, 3) weekly slow stochastic are computed by importing the existing
`leaps_scanner.indicators` functions. The backtest must not contain a second
implementation of any §4 formula. (Acceptance §10.7 asserts parity against live
`scan_results` rows.)

### 4.2 Entry

Evaluated at each completed-week Friday close *t*, per §4:

```
entry_signal(t) = trend_pass(t) AND in_zone(t) AND turning_up(t)

trend_pass : close > SMA50 AND close > SMA200 AND SMA50 > SMA200   (daily, at t)
in_zone    : 20 ≤ slowK ≤ 70                                        (completed weekly bars)
turning_up : slowK > D AND slowK > slowK[−1]
```

Variant reported alongside (not headline): Strict zone `20 ≤ slowK ≤ 55`.
The IV-rank, earnings-distance, quality, and liquidity preset gates are **not** applied
(§1). One open trade per symbol per track (P8); signals while in a trade are ignored.

### 4.3 Execution

Signal at close of *t* → fill at the **open of the next session** with data for that
symbol. If no bar appears within 5 sessions, the entry is skipped and logged. The same
next-open rule applies to every exit. No same-close fills (that would use the closing
price that produced the signal).

### 4.4 Exits

Evaluated per SPEC.md §7, restricted to what is reconstructable:

| Rule | Check | Cadence | Applies to |
|---|---|---|---|
| Trend break | daily close < SMA200 OR SMA50 < SMA200 | daily | both tracks |
| Stochastic | weekly slowK **crosses** below 20: `slowK[−1] ≥ 20 AND slowK < 20` (completed bar) | weekly | both tracks |
| Premium stop | model mid ≤ 50% of entry premium paid (§5.4) | daily | LEAP overlay only |
| Time exit | DTE < 180, i.e. calendar days held ≥ 185 (P3) | daily | both tracks |
| Delisted | price history ends mid-trade | — | both tracks (§2.5) |

Not simulated (stated, bias register): earnings heads-up (informational only in v1, and no
historical earnings calendar exists). If multiple rules trigger at the same evaluation,
the recorded reason follows the priority `trend_break > stoch_below_20 > premium_stop >
time_exit`; the fill (next open) is identical regardless. Crossing semantics are exactly
§10's: *crosses below 20 ≠ is below 20*.

Trades still open at the end of the window are marked at the final close, flagged
`end_of_window`, included in the statistics, and their count disclosed.

### 4.5 Stock-track P&L

`r_trade = exit_open / entry_open − 1` on the P2 price basis (dividends excluded — the
strategy's actual vehicle is a call, which collects no dividends; direction stated in
the bias register).

---

## 5. Synthetic LEAP overlay (exact)

All Black-Scholes terms as in SPEC.md §5: `d1 = (ln(S/K) + (r − q + σ²/2)T) / (σ√T)`,
`d2 = d1 − σ√T`, call price `C = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)`,
`delta = e^(−qT)·N(d1)`.

1. **Inputs at entry** (fill date, §4.3, S = fill open):
   - `σ = m · RV252`, base multiplier `m = 1.1`; `RV252 = stdev(ln(C_i/C_{i−1}),
     last 252 sessions, sample stdev) · √252` on the P2 close series.
   - `r` = ^IRX/100 on the entry date (last available print), held flat (P7).
   - `q` = trailing 365-day cash dividends per share ÷ S, held flat (P6). Correct on this
     price basis: dividends are *not* embedded in the P2 series, matching live §5.
   - `T₀ = 365/365` years (P3).
2. **Strike (closed form).** The 0.70Δ strike solves `e^(−qT₀)·N(d1) = 0.70`:
   ```
   d1* = N⁻¹(0.70 · e^(qT₀))
   K   = S · exp(−(d1*·σ√T₀ − (r − q + σ²/2)·T₀))
   ```
   No rounding to a strike grid (P4).
3. **Entry premium.** `entry_cost = C(S, K, σ, r, q, T₀) · (1 + h)`, `h = 0.04` (P5).
4. **Daily repricing.** At each daily close: `mid_t = C(S_t, K, σ, r, q, T_t)` with the
   entry σ, r, q held flat and `T_t = (expiry − t)/365`. Premium stop: `mid_t ≤ 0.5 ·
   entry_cost` (model mid vs. premium paid, mirroring §7's live comparison).
5. **Exit value.** `exit_value = C(S_exit, K, σ, r, q, T_exit) · (1 − h)` at the exit
   fill (next open). Trade return `= exit_value / entry_cost − 1`.
6. **Sensitivities.** One-at-a-time around the base case (m = 1.1, h = 0.04):
   `m ∈ {1.0, 1.1, 1.2}` and `h ∈ {0, 0.04, 0.06}`. All five runs appear in every report.

---

## 6. Tracks, metrics, benchmarks

### 6.1 Track A — per-trade (primary)

Every §4.2 signal taken independently (subject to P8). Reported for both the stock track
and the LEAP overlay: trade count, win rate, mean and median return, profit factor,
return percentiles (5/25/50/75/95), holding-period stats, exit-reason breakdown
(including `delisted` and `end_of_window` counts), and per-year trade counts.

Per-trade benchmark deltas, same window and same price basis as the trade:
- **Timing alpha:** `r_trade − r_hold`, where `r_hold` is the same symbol held
  entry-fill → exit-fill.
- **Market delta:** `r_trade − r_SPY` over the same window (SPY on the P2 basis for
  consistency at trade level).

### 6.2 Track B — sleeve simulation (secondary)

Applies §7's discipline to Track A's LEAP-overlay signals, processed weekly in signal
order (P10 tie-break):

- Start equity E₀ = $100,000 (P9); equity marks daily from model mids.
- Per-position premium budget `0.03 · current equity`;
  `contracts = floor(budget / (entry_cost · 100))`; zero contracts ⇒ entry skipped,
  logged, and counted (granularity is a real cost at small equity — reported, and probed
  by the E₀ = $250k sensitivity).
- Caps, checked at entry: ≤ 5 open positions; ≤ 2 per sector (§3.6 labels);
  open premium at cost ≤ 15% of current equity.
- **Circuit breaker (P11, mirroring `risk.py`):** realized P&L over the trailing 28 days
  plus unrealized on open positions, divided by the `account_equity_at_entry` snapshot of
  the most recently opened position; at ≥ 8% loss, no new entries for 28 days (latched).
- Cash earns 0% (conservative; understates the sleeve — bias register).

Reported: CAGR, max drawdown (daily equity), calendar-year returns, win rate, average
open-position count and premium exposure, skipped-entry counts (by cause: slots, sector,
exposure, granularity, breaker), breaker activation dates.

### 6.3 Benchmarks

1. **SPY buy-and-hold**, total return (Adj Close), over the full window: CAGR, max DD.
   The report must state the exposure caveat: the sleeve risks ≤ 15% of equity by design
   while SPY is 100% invested — headline CAGRs are not like-for-like, and the comparison
   table says so in its caption.
2. **Equal-weight matched names**: buy-and-hold of the distinct entered symbols,
   equal-weighted, each over the window it was actually traded (aggregate of §6.1's
   `r_hold`). Answers "does the timing add anything beyond the names themselves?"

---

## 7. Bias register

Every report embeds this table (values updated per run where applicable).

| # | Assumption | Direction | Mitigation / note |
|---|---|---|---|
| 1 | Residual survivorship: delisted members with no yfinance data are unpriceable | **Flatters** (missing names skew toward failures) | Coverage ratio + uncovered list reported (§2.4) |
| 2 | Flat IV over each trade | **Likely flatters** (entries follow pullbacks, when true IV is typically elevated; mean reversion would hurt a long call) | Stated; sensitivity on σ level only — path dynamics are out of scope |
| 3 | σ = 1.1 × RV252 | Unknown | Sensitivity m ∈ {1.0, 1.2} (§5.6) |
| 4 | No liquidity gates; fills at model value ± h | **Flatters** (some trades untradeable at modeled prices) | h haircut + sensitivity; non-comparability banner |
| 5 | Continuous strike (no grid) | Neutral/minor | — |
| 6 | r and q flat per trade | Minor | — |
| 7 | Current sector labels applied historically | Minor | Affects only the 2-per-sector cap |
| 8 | Membership CSV errors possible | Unknown | Source + retrieval date recorded |
| 9 | Earnings-distance gate not simulated | Unknown (backtest enters where live Strict/Balanced would wait) | Stated |
| 10 | Stock-track returns exclude dividends | **Hurts** the stock track slightly vs. total-return intuition | Vehicle is a call; benchmarks labeled |
| 11 | Cash earns 0% in the sleeve | **Hurts** the sleeve | Conservative by construction |
| 12 | Quality/valuation/IV filters untested | Unknown — live strategy may differ in either direction | §1 banner, verbatim |

---

## 8. Deliverables & where things live

```
scanner/leaps_scanner/backtest/
├── data/sp500_membership.csv   # PIT membership (committed)
├── membership.py               # §2 loader + normalization
├── data.py                     # cached 10y fetch (reuses prices.py machinery)
├── engine.py                   # §4 event loop (stock track)
├── synthetic.py                # §5 pricing/strike (reuses options.py BS helpers)
├── sleeve.py                   # §6.2 (mirrors risk.py breaker semantics)
└── report.py                   # report.md + results.json + SVG curves
```

- **Run:** manual only — `make backtest` / `python -m leaps_scanner.backtest`. No cron,
  no GitHub Actions job; it is an analysis, not a pipeline. Deterministic: no randomness,
  no wall-clock dependence beyond the recorded run date.
- **Results:** committed via PR under `docs/backtest/<run-date>/` — `report.md` (human
  report: §1 banner, all §6 tables, §7 register, §5.6 sensitivities), `results.json`
  (machine-readable, schema versioned), equity-curve SVGs.
- **Web:** `/backtest` page (server component) renders the latest committed
  `results.json` at build time. The §1 banner is part of the JSON and the page must not
  render metrics without it. No new Supabase tables, no migrations.
- **Public repo:** unchanged rules — no secrets; no raw price data committed.

---

## 9. Testing (per-milestone, SPEC.md §10 style)

- **Unit (must):**
  - Membership boundary semantics (inclusive `added`, exclusive `removed`); symbol
    normalization; rename handling.
  - Strike inversion round-trip: `delta(K) = 0.70 ± 1e−9` for the §5.2 closed form.
  - Repricing against §10's BS reference values; RV252 against a hand-computed fixture.
  - Friction arithmetic (entry ×1.04, exit ×0.96) to the cent.
  - Exit crossing semantics reuse the existing §10 tests (crosses-below ≠ is-below);
    exit priority ordering; forced `delisted` exit; `end_of_window` marking.
  - Tie-break determinism (fixture where three signals compete for one slot).
  - Sleeve breaker: fixture mirroring `risk.py` semantics (28-day realized window,
    newest-entry denominator, latch).
- **Golden trade:** one fully hand-computed trade (synthetic price series) — entry date,
  strike, entry premium, daily mids, exit, both tracks' P&L — reproduced to the cent.
- **Integration:** engine over a small recorded fixture universe → golden
  `results.json`.
- **No-look-ahead property:** truncating the input data at date *d* must not change any
  decision (entries, exits, sizing) made at or before *d*. Asserted by running the
  engine on full vs. truncated fixtures.

## 10. Acceptance criteria (v2)

1. **Determinism:** two runs from the same cache produce byte-identical `results.json`.
2. **Coverage honesty:** the report states the member-week coverage ratio and lists every
   uncovered symbol with its membership span; coverage < 85% adds a visible warning to
   every headline table. No silent truncation of the universe.
3. **Golden trade:** the §9 hand-computed trade reproduces to the cent.
4. **No-look-ahead:** the §9 property test passes.
5. **Runtime:** full 10y run from a warm cache < 15 min on a laptop; the initial fetch
   respects the §3.3 throttle (≤ 5 req/s) end to end.
6. **Report completeness:** every generated report contains the §1 banner, the §7 bias
   register, and the §5.6 sensitivity table; the `/backtest` page renders the banner
   above any metric.
7. **Live parity:** for a recent scan date, the backtest's `trend_pass`, `slowK`, `D`,
   and `turning_up` for a sampled symbol equal the live `scan_results` row (same
   `indicators.py` code path, proven, not assumed).

## 11. Milestones (one Claude Code session each; branch → PR → /code-review → fix → merge)

1. **B1 Universe & data:** membership CSV + loader + normalization, cached 10y fetch
   reusing `prices.py` machinery, ^IRX + dividend series, coverage report, warm-up
   eligibility; unit tests (membership boundaries, cache determinism).
2. **B2 Stock-signal engine:** §4 event loop reusing `indicators.py`, Track A stock-track
   stats + per-trade benchmarks, Strict-zone variant, no-look-ahead property test,
   integration golden file. Acceptance 1, 2, 4, 7.
3. **B3 Synthetic LEAP overlay:** §5 pricing, strike closed form, friction, premium stop,
   sensitivity grid, golden trade to the cent. Acceptance 3.
4. **B4 Sleeve & publication:** §6.2 simulation with `risk.py`-mirrored breaker,
   `report.md`/`results.json`/SVG generation, `/backtest` page with banner, README
   section, first committed run under `docs/backtest/`. Acceptance 5, 6.

## 12. Out of scope for v2

Resend email alerts and IV-rank graduation (still parked — SPEC.md §12); point-in-time
fundamentals from paid or filings-based sources; purchased historical option chains;
intraday execution; taxes and commissions beyond the h haircut; multi-user anything.

---

*Disclaimer: educational/personal tooling. This backtest is a simulation built on stated
approximations over delayed, unofficial data. Past performance — simulated or otherwise —
does not predict future results. Nothing here is financial advice.*
