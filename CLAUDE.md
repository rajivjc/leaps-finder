# leaps-finder

A LEAPS options screener: Next.js (Vercel) + Supabase + Python scanner (GitHub Actions).

## The one rule

**SPEC.md is the source of truth.** All formulas (stochastic, Black-Scholes delta, scoring,
presets, exit rules) are pinned there exactly — implement them as written, do not improvise
the math. If something in the spec seems wrong or ambiguous, stop and ask; don't silently
deviate. **SPEC-BACKTEST.md carries the same authority for v2 (backtesting)**; if the two
documents ever conflict, that too is a stop-and-ask, not a judgment call.

## Workflow

- Build milestone by milestone (SPEC.md §11: M1 → M6). One milestone per session; do not
  start the next milestone unless asked.
- Tests are part of every milestone, not a later phase. The unit tests in SPEC.md §10
  (stochastic fixture, BS delta reference values, crossing semantics) are required.
- A partially-failed scan must report status `failed` — partial data must never look complete.

## Hard constraints

- This is a PUBLIC repo. No secrets in code, ever — Supabase service key only in GitHub
  Actions secrets, anon key only in Vercel env vars.
- Scores are displayed raw. No display curves, no rescaling, no score inflation of any kind.
- Signals use completed weekly bars only (no repaint).
- yfinance calls: batched, throttled (≤5 req/s), retried with backoff.

## Stack conventions

- Python 3.11+, `ruff` + `pytest`, package lives in `scanner/leaps_scanner/`.
- Next.js App Router + TypeScript + Tailwind in `apps/web/`; server components read
  Supabase with anon key.
- SQL migrations in `supabase/migrations/`; RLS per SPEC.md §2.
- Charts: lightweight-charts.

## Disclaimer

Educational/personal tooling on delayed data. The app must display "not financial advice"
(SPEC.md §8, /about page).
