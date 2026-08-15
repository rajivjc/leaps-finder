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
│  fundamentals      │  write  │ iv_snapshots     │  read   │  /compare        │
│  options + BS delta│         │ positions (RLS)  │         │  /positions      │
│  scoring           │         │ alerts    (RLS)  │         │  /about          │
│  exit monitor      │         └──────────────────┘         └──────────────────┘
└────────────────────┘
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

Then create a Supabase project, apply the migrations in `supabase/migrations/` in order, and
fill in the two env files:

```bash
cp scanner/.env.example scanner/.env        # SUPABASE_URL, SUPABASE_SERVICE_KEY
cp apps/web/.env.example apps/web/.env.local # NEXT_PUBLIC_* pair
```

Neither file is tracked. This repo is public — no key of any kind belongs in a commit.

```bash
make web      # dev server on :3000
make scan     # the same full scan Actions runs on Saturdays
make lint     # ruff + eslint + tsc
make test     # pytest
```

## Status

Built milestone by milestone against [SPEC.md §11](SPEC.md).

- [x] **M1 Scaffold** — monorepo, migrations + RLS, Next.js shell with Supabase client, CI
- [ ] **M2 Scanner core** — universe, prices, indicators, `scan_results` writes
- [ ] **M3 Options + scoring** — chain selection, Black-Scholes delta, IV snapshots, presets
- [ ] **M4 Frontend** — screener, ticker detail, compare
- [ ] **M5 Risk** — auth, positions, size calculator, exit monitor and alerts
- [ ] **M6 Polish** — deploy, screenshots, badges

## Disclaimer

This project is educational. Yahoo Finance data is delayed and unofficial. Long-dated calls
regularly expire worthless and you can lose the entire premium. Nothing in this repository or
the application it builds constitutes financial advice. Do your own research.
