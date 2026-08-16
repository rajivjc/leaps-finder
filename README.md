# LEAPS Finder

[![CI](https://github.com/rajivjc/leaps-finder/actions/workflows/ci.yml/badge.svg)](https://github.com/rajivjc/leaps-finder/actions/workflows/ci.yml)
[![Weekly scan](https://github.com/rajivjc/leaps-finder/actions/workflows/scan-weekly.yml/badge.svg)](https://github.com/rajivjc/leaps-finder/actions/workflows/scan-weekly.yml)
[![Daily refresh](https://github.com/rajivjc/leaps-finder/actions/workflows/refresh-daily.yml/badge.svg)](https://github.com/rajivjc/leaps-finder/actions/workflows/refresh-daily.yml)

A screener for long-dated call options (LEAPS) on US large caps. It applies five filters —
trend, weekly stochastic, quality, valuation upside, and low implied volatility — prices a
roughly one-year 0.70-delta call for every match, and ranks candidates with a composite score
that is displayed exactly as computed.

No curves, no rescaling, no green-at-70 thresholds. If a name scores 43, it shows 43.

> Educational and personal tooling on delayed, unofficial data. Nothing here is financial
> advice. See [SPEC.md](SPEC.md) for every formula and [/about](apps/web/app/about/page.tsx)
> for the caveats the app itself displays.

## Screens

Captured from the live app against a real weekly scan — 238 names in the universe, 237
evaluated, as of Friday 14 August 2026.

### Screener

![The screener, Wide open preset](docs/images/screener.png)

The preset tabs report their own counts, and two of the three are showing zero. That is the real
result for this scan, not a loading state: 29 names clear trend plus zone plus turning-up, but
only 9 of them also clear Strict's and Balanced's `OI ≥ 200` liquidity gate, and the intersection
with the remaining gates is empty. Whether the preset thresholds are calibrated correctly is a
strategy question this repo deliberately leaves open — the screener's job is to report the
filters as specified, not to find something to show.

Every IV-rank badge reads *warming up* for the same honest reason: IV rank is computed from this
project's own `iv_snapshots`, and §6 requires 120 daily observations before the number means
anything. Until then the card says so and the score substitutes a cross-sectional percentile.

### Ticker detail

![MU ticker detail with chart, checklist, and score breakdown](docs/images/ticker-detail.png)

Weekly candles with the SMA50/200 overlay, the stochastic panel with its 20/70 band, the
five-filter checklist with a reason on every row, and the score breakdown showing raw factor
numbers. A composite of 73.0 is drawn as 73.0.

### Compare

![Three names compared on raw factor scores, with the underlying data table](docs/images/compare.png)

Up to four names, opening on the top three. The factor bars share one fixed 0–100 axis rather
than being normalised to the best of the group, so three mediocre candidates look like three
mediocre candidates. Underneath, the numbers the scores were computed from — including the
`Strict / Balanced / Wide open` row that shows two of the three presets rejecting all of them.

## Architecture

```
GitHub Actions (cron)          Supabase Postgres            Vercel
┌────────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│ scanner (Python)   │ service │ tickers          │  anon   │ Next.js App      │
│  universe          │  key    │ scans            │  key    │  / screener      │
│  prices/indicators │  write  │ scan_results     │  read   │  /t/[symbol]     │
│  fundamentals      ├────────▶│ weekly_bars      │◀────────┤  /compare        │
│  options + BS delta│         │ iv_snapshots     │         │  /positions      │
│  scoring           │         ├─ owner only ─────┤         │  /about          │
│  exit monitor      │         │ positions        │         └──────────────────┘
│  daily marks       │         │ alerts           │
└────────────────────┘         │ position_marks   │         server components,
                               │ user_settings    │         precomputed rows only
   weekly: full scan (Sat)     └──────────────────┘
   daily:  refresh + exits
```

Everything above the `owner only` rule is public: the scanner writes it with the service key from
Actions secrets, and anyone may read it. Nothing else can write it at all, which is enforced by
the absence of an insert policy rather than by trust.

Below that rule, ownership decides. The owner writes their own `positions` and `user_settings`
straight from the browser under `auth.uid() = user_id`; the scanner writes `alerts` and
`position_marks` on their behalf and the owner only reads them back — acknowledging an alert is
the single exception. The anon key the browser ships with is powerless outside those policies,
which is the whole reason the secret key never leaves Actions.

## Repo layout

| Path | What lives there |
|---|---|
| `apps/web/` | Next.js App Router frontend (Vercel root directory) |
| `scanner/` | Python package `leaps_scanner` — the whole pipeline |
| `supabase/migrations/` | Schema and RLS policies |
| `.github/workflows/` | CI, plus the weekly and daily scan crons |
| `docs/` | Supabase setup runbook, the screenshots above, and committed backtest runs |
| `SPEC.md` | Source of truth for every formula, threshold, and preset |
| `SPEC-BACKTEST.md` | Source of truth for v2, the backtest — same authority as SPEC.md |

## Getting started

Requires Python 3.11+, Node 22+, and [uv](https://docs.astral.sh/uv/).

```bash
make setup
```

Then follow **[docs/SETUP.md](docs/SETUP.md)** to create a Supabase project, apply the
migrations, verify the security policies, and get the keys to the four places they belong. It
takes about fifteen minutes and covers what to do if a key ever leaks.

The short version: apply `supabase/migrations/` in order, then fill in the two env files.

```bash
cp scanner/.env.example scanner/.env        # SUPABASE_URL, SUPABASE_SERVICE_KEY
cp apps/web/.env.example apps/web/.env.local # NEXT_PUBLIC_* pair
```

Neither file is tracked. This repo is public — no key of any kind belongs in a commit, and the
service key belongs only in those two places and GitHub Actions secrets.

```bash
make web      # dev server on :3000
make scan     # the same full scan Actions runs on Saturdays
make lint     # ruff + eslint + tsc
make test     # pytest
```

Dev must be on port 3000. Sign-in links are built from the browser's own origin and Supabase
rejects any origin missing from its redirect allow-list, so a server on :3001 can read the
screener but cannot complete a magic link.

## Deploying

The web app deploys to Vercel from `apps/web`.

> **Forking this?** `apps/web/vercel.json` pins the serverless region to `sin1` (Singapore)
> because that is where *this* project's Supabase lives. **Change it to match your own database's
> region before you deploy** — a mismatch is the difference between a screener that renders in
> under a second and one that takes three, and it fails slowly and silently rather than loudly.
> Vercel's region codes are in [their docs](https://vercel.com/docs/edge-network/regions).

1. **New Project** → import this repository.
2. Set **Root Directory** to `apps/web`. Next.js, the build command, and the output directory are
   then detected automatically. Skipping this step is the one way to get a build that fails at
   "no framework detected".
3. Set the region in `apps/web/vercel.json` to your Supabase project's region, per the note above.
4. Add the two public environment variables from [docs/SETUP.md §6](docs/SETUP.md) —
   `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`. The publishable key only; the
   secret key never touches Vercel.
5. Add `https://<your-domain>/auth/callback` to Supabase's **Authentication → URL Configuration →
   Redirect URLs**, alongside the localhost entry. Until it is there, sign-in works locally and
   fails in production.

### Why the region is pinned

Vercel defaults new projects to `iad1` (US East) regardless of where the database is, and every
page here except `/about` has to reach Supabase before it can render. Deployed at that default
against a Singapore database, the screener missed
[SPEC.md §10](SPEC.md)'s "screener loads < 1s" by roughly threefold.

Measured on the deployed site, screener TTFB:

| | `iad1` (default) | `sin1` (pinned) |
|---|---|---|
| Warm, median | ~2.7s | **0.39s** |
| Warm, range | 2.1 – 4.7s | 0.27 – 0.55s |
| Cold start | — | 1.01s |

Two measurements identified the distance as the cause rather than the code. The response header
read `x-vercel-id: sin1::iad1::…` — the request entering at the Singapore edge and the function
executing in Virginia — while Supabase answered a query in **39ms** from Singapore over a reused
connection. `loadScreener` issues three sequential round trips (`scans`, then `scan_results`, then
`tickers` and `weekly_bars` together), so at the default region each one paid a cross-Pacific
crossing, plus a fresh TLS handshake on cold connections, instead of tens of milliseconds. Pinned,
the header reads `sin1::sin1` and `/t/[symbol]` and `/compare` land around 0.30s too.

Three caveats worth knowing before treating this as a total fix. The **first** request after an
idle period still measures ~1.0s — right on §10's line — because a serverless cold start pays for
a fresh connection to Supabase before it can answer; every warm request has roughly 2× headroom.
`regions` governs the serverless functions, so it does not necessarily cover `proxy.ts`, which
refreshes the session on **every** matched request and awaits one `getUser()` call against
Supabase before the page function runs. And the region only co-locates the function with the
database — a visitor far from that region still pays their own distance to it. What the pin
removes is the round trip that was paid per query on every data-backed page.

Keeping it in a committed file rather than a dashboard setting means the constraint is visible to
anyone reading the repo, and shows up in review when it changes.

The build runs without Supabase configured — CI proves this on every pull request — so a missing
variable surfaces as an "unconfigured" notice on the page rather than a failed deploy. That is
friendlier to debug and also means the first deploy cannot fail for that reason alone.

## Operations

Two crons keep the data current, and both report through the badges at the top of this file.

| Workflow | When | What it does |
|---|---|---|
| **Weekly scan** | Saturdays 02:00 UTC | Full pipeline: universe, prices, fundamentals, chains, scoring, weekly exit rules |
| **Daily refresh** | Weekdays 22:30 UTC | Quotes, IV snapshots, and the daily exit rules; doubles as the Supabase keep-alive |

The badge going red is the alerting mechanism — there is no inbox to check. It means the run
exited non-zero, which happens in two distinct cases that need different responses.

**The scan row says `failed`.** The screener data is incomplete: a run that loses more than a
fifth of the universe is recorded as failed rather than publishing thinned-out results, and the
app will not display it. The site keeps serving the last good scan.

**The scan row says `ok` and the run still exited non-zero.** The screener data is fine; §7's
exit monitor is what fell short, because a rule could not be evaluated for an open position. This
case leaves `status = 'ok'` deliberately — the app only ever displays an `ok` scan, so marking it
failed would blank the public site over one held position — and puts the detail in the scan's
`notes` and the job's stderr instead. Do not read a red badge over an `ok` row as spurious: it
means an exit signal may have been missed, which is the one thing that must never be inferred
from silence.

The daily cron runs `1-5` rather than SPEC.md §9's literal `2-6`. At 22:30 UTC there is no date
rollover, so `2-6` would cover Tuesday through Friday's US closes plus a Saturday with no session
and miss Monday's entirely; `1-5` is what §9's own annotation ("Mon–Fri US close") describes. The
reasoning is repeated in the workflow file.

## How a scan decides

The scanner reads the bundled S&P 500 seed list, keeps names at $50B or more of market cap,
pulls two years of daily bars for each, and evaluates them at the close of the **last completed
week**. The in-progress week is dropped before any signal is computed, so a Tuesday rerun
reproduces Saturday's numbers exactly — nothing repaints.

A symbol without enough history to define every signal is excluded rather than approximated,
and a 10-week window with no range yields no stochastic rather than an invented midpoint. If
more than 20% of the universe ends up missing for any reason, the scan is recorded as `failed`
and the screener will not display it. Partial data never looks complete.

## Backtest

Ten years of the entry/exit **timing** signal, replayed over point-in-time S&P 500 membership.
It is a separate piece of work with its own source of truth, [SPEC-BACKTEST.md](SPEC-BACKTEST.md),
and it deliberately does **not** evaluate the whole strategy: the quality, valuation, IV-rank
and earnings-distance filters need point-in-time fundamentals that cannot be reconstructed
without look-ahead bias, and the preset liquidity gates need historical option chains nobody
has. A good result here validates two of the five filters, and the report says so in a banner
above every number.

Runs are manual and local — an analysis, not a pipeline, so there is no cron and no Actions
job. Each one is committed under `docs/backtest/<run-date>/` as a human report, a
machine-readable `results.json`, and equity-curve SVGs; the `/backtest` page renders the most
recent one at build time. The build fails outright if `results.json` has no banner, so the
page cannot show a metric without the caveat that says what it means.

```bash
make backtest        # or: python -m leaps_scanner.backtest --report-dir docs/backtest
```

The first fetch is network-bound and takes roughly 30–45 minutes at the throttle the live
scanner uses; from a warm cache a full run — six sensitivity configurations, both sleeves and
both benchmarks — takes about 16 seconds and is byte-for-byte reproducible. The price cache is
git-ignored: no raw Yahoo data is ever committed.

**What the first run found**, and it is not flattering: the stock signal is mildly positive
(5,942 trades, 36.3% win rate, mean +2.24%, profit factor 1.58), but it does not survive the
option vehicle at the pinned friction — through a synthetic one-year 0.70Δ call the same
signal returns a mean of −2.26% with a profit factor of 0.89, and it is profitable only at
h = 0, i.e. assuming fills at model mid with no spread at all. SPY returned 15.32% a year over
the same window. The full result, every sensitivity, and the bias register listing each
approximation with the direction it pushes the number are in
[`docs/backtest/2026-08-16/report.md`](docs/backtest/2026-08-16/report.md).

## Status

Built milestone by milestone against [SPEC.md §11](SPEC.md).

- [x] **M1 Scaffold** — monorepo, migrations + RLS, Next.js shell with Supabase client, CI
- [x] **M2 Scanner core** — universe, prices, indicators, `scan_results` writes, weekly cron
- [x] **M3 Options + scoring** — chain selection, Black-Scholes delta, IV snapshots, presets
- [x] **M4 Frontend** — screener, ticker detail, compare, about; `weekly_bars` chart series
- [x] **M5 Risk** — magic-link auth, positions CRUD, persisted size calculator, §7's exit
      monitor and alerts, daily refresh job
- [x] **M6 Polish** — screenshots, scan and refresh badges, deploy runbook, disclaimer audit

That is v1. [SPEC.md §12](SPEC.md) parks the rest: a backtest of the stock-level signal, email
alerts through Resend, and IV-rank graduation once 252 snapshots have accumulated — roughly a
year of the daily refresh, with the *warming up* badges clearing at 120 around six months in.

## Disclaimer

This project is educational. Yahoo Finance data is delayed and unofficial. Long-dated calls
regularly expire worthless and you can lose the entire premium. Nothing in this repository or
the application it builds constitutes financial advice. Do your own research.
