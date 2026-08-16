# LEAPS Finder

[![CI](https://github.com/rajivjc/leaps-finder/actions/workflows/ci.yml/badge.svg)](https://github.com/rajivjc/leaps-finder/actions/workflows/ci.yml)

A screener for long-dated call options (LEAPS) on US large caps. It applies five filters —
trend, weekly stochastic, quality, valuation upside, and low implied volatility — prices a
roughly one-year 0.70-delta call for every match, and ranks candidates with a composite score
that is displayed exactly as computed.

No curves, no rescaling, no green-at-70 thresholds. If a name scores 43, it shows 43.

> Educational and personal tooling on delayed, unofficial data. Nothing here is financial
> advice. See [SPEC.md](SPEC.md) for every formula and [/about](apps/web/app/about/page.tsx)
> for the caveats the app itself displays.

## Architecture

```
GitHub Actions (cron)          Supabase Postgres            Vercel
┌────────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│ scanner (Python)   │ service │ tickers          │  anon   │ Next.js App      │
│  universe          │  key    │ scans            │  key    │  / screener      │
│  prices/indicators ├────────▶│ scan_results     │◀────────┤  /t/[symbol]     │
│  fundamentals      │  write  │ weekly_bars      │  read   │  /compare        │
│  options + BS delta│         │ iv_snapshots     │         │  /positions      │
│  scoring           │         │ positions (RLS)  │         │  /about          │
│  exit monitor      │         │ alerts    (RLS)  │         └──────────────────┘
└────────────────────┘         └──────────────────┘
   weekly: full scan (Sat)                                  server components,
   daily:  refresh + exits                                  precomputed rows only
```

The scanner is the only writer, and it writes with the service key from Actions secrets. The
web app only ever reads, with the anon key, through row-level security: scan data is public,
positions and alerts are owner-only.

## Repo layout

| Path | What lives there |
|---|---|
| `apps/web/` | Next.js App Router frontend (Vercel root directory) |
| `scanner/` | Python package `leaps_scanner` — the whole pipeline |
| `supabase/migrations/` | Schema and RLS policies |
| `.github/workflows/` | CI, plus the weekly and daily scan crons |
| `SPEC.md` | Source of truth for every formula, threshold, and preset |

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

## How a scan decides

The scanner reads the bundled S&P 500 seed list, keeps names at $50B or more of market cap,
pulls two years of daily bars for each, and evaluates them at the close of the **last completed
week**. The in-progress week is dropped before any signal is computed, so a Tuesday rerun
reproduces Saturday's numbers exactly — nothing repaints.

A symbol without enough history to define every signal is excluded rather than approximated,
and a 10-week window with no range yields no stochastic rather than an invented midpoint. If
more than 20% of the universe ends up missing for any reason, the scan is recorded as `failed`
and the screener will not display it. Partial data never looks complete.

## Status

Built milestone by milestone against [SPEC.md §11](SPEC.md).

- [x] **M1 Scaffold** — monorepo, migrations + RLS, Next.js shell with Supabase client, CI
- [x] **M2 Scanner core** — universe, prices, indicators, `scan_results` writes, weekly cron
- [x] **M3 Options + scoring** — chain selection, Black-Scholes delta, IV snapshots, presets
- [x] **M4 Frontend** — screener, ticker detail, compare, about; `weekly_bars` chart series
- [x] **M5 Risk** — magic-link auth, positions CRUD, persisted size calculator, §7's exit
      monitor and alerts, daily refresh job
- [ ] **M6 Polish** — deploy, screenshots, badges

## Disclaimer

This project is educational. Yahoo Finance data is delayed and unofficial. Long-dated calls
regularly expire worthless and you can lose the entire premium. Nothing in this repository or
the application it builds constitutes financial advice. Do your own research.
