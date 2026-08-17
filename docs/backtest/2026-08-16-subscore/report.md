# §6 subscore test — run 2026-08-16

**Analysis, not a milestone.** SPEC-BACKTEST.md §0 Decision 2 excluded the §6 score
from v2 and this does not reverse it: no threshold is re-pinned, the engine and the
published run under `docs/backtest/2026-08-16/` are untouched. The question here is
narrow — does the Trend subscore, or Entry, order a trade's outcome?

**Market delta is the quantity that decides it**, not raw return. Trending names rose
over this window, so a high-Trend bucket earning more says nothing on its own; beating
SPY over the trade's own days is the only thing that can pay for the friction of buying
the option instead of the shares.

## The alignment gate

Each trade is scored at its **signal date**, not its entry date — P1 fills at the next
session's open, so the signal date is the completed weekly bar a Saturday scan would
have seen. The chain's frame is sliced to that date before `indicators.evaluate` is
called; an unsliced frame would score against the whole ten years without raising
anything. The check that catches it: the recomputed slowK must equal the
`entry_slow_k` the engine already recorded, for every trade.

| Trades in | Scored | Insufficient history | Unpriced chains | Market delta unpriced | Overlay matched |
|---|---|---|---|---|---|
| 5942 | 5942 | 0 | 0 | 0 | 5939 |

Variant `base`, 569 rename chains.
Mean `r_trade` +2.24%; mean market delta -0.45% (95% CI [-1.00%, +0.10%]), median -2.91%.

Confidence intervals are percentile bootstraps over 10,000 replicates resampling whole **entry month** blocks (seed 20260817). Trades overlap in time, so resampling individual trades would report an interval roughly √2 too narrow.

## Trend subscore (25% of the composite)

Distribution: mean 37.0, median 32.0, range 0.0–100.0.

| Score range | n | Win rate | Mean r | Median r | PF | Mkt delta | 95% CI | Median delta | Overlay | P(hold ≥ 186d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.0–7.6 | 595 | 34.6% | +0.81% | -1.10% | 1.45 | -0.51% | [-1.16%, +0.15%] | -1.04% | -5.37% | 6.6% |
| 7.6–13.1 | 594 | 29.8% | +0.91% | -2.28% | 1.34 | -0.38% | [-1.13%, +0.41%] | -2.14% | -6.41% | 9.9% |
| 13.1–18.7 | 594 | 33.2% | +1.69% | -2.74% | 1.53 | -0.38% | [-1.29%, +0.57%] | -2.99% | -3.31% | 14.0% |
| 18.7–25.1 | 594 | 36.5% | +2.61% | -3.21% | 1.76 | +0.03% | [-1.03%, +1.13%] | -2.98% | +0.64% | 19.2% |
| 25.1–32.0 | 594 | 33.8% | +0.99% | -3.67% | 1.26 | -1.34% | [-2.13%, -0.54%] | -3.93% | -6.56% | 17.7% |
| 32.1–40.4 | 594 | 36.9% | +3.27% | -3.66% | 1.80 | +0.32% | [-0.94%, +1.69%] | -3.26% | +0.36% | 23.4% |
| 40.4–50.3 | 594 | 38.0% | +2.30% | -3.48% | 1.56 | -0.62% | [-1.74%, +0.53%] | -3.83% | -1.40% | 22.2% |
| 50.3–60.9 | 594 | 38.7% | +3.21% | -3.58% | 1.68 | -0.61% | [-1.83%, +0.66%] | -4.69% | -0.04% | 26.1% |
| 60.9–74.5 | 594 | 40.2% | +3.56% | -3.28% | 1.68 | +0.10% | [-1.39%, +1.60%] | -4.09% | +2.34% | 29.0% |
| 74.5–100.0 | 595 | 40.8% | +3.05% | -3.29% | 1.53 | -1.16% | [-3.09%, +1.04%] | -5.25% | -3.25% | 27.1% |

| Against | n | Spearman | p (independence assumed) |
|---|---|---|---|
| market_delta | 5942 | -0.1383 | 9.18e-27 |
| r_trade | 5942 | -0.1073 | 1.08e-16 |
| holding_days | 5942 | 0.4005 | 7.02e-228 |
| r_overlay | 5939 | -0.1717 | 1.56e-40 |

Top-minus-bottom mean market delta, with the cut recomputed inside every replicate:

| Cut | Spread | 95% CI |
|---|---|---|
| top/bottom 25% | -0.28% | [-1.52%, +0.99%] |
| top/bottom 10% | -0.64% | [-2.66%, +1.77%] |

Held out at 2022 — 3262 trades in, 2680 held out:

| Period | Spearman vs market delta | Top/bottom 10% spread |
|---|---|---|
| < 2022 | -0.1154 | -1.17% |
| ≥ 2022 | -0.1696 | -0.48% |

## Entry subscore (15%)

Distribution: mean 81.4, median 82.9, range 15.3–100.0.

| Score range | n | Win rate | Mean r | Median r | PF | Mkt delta | 95% CI | Median delta | Overlay | P(hold ≥ 186d) |
|---|---|---|---|---|---|---|---|---|---|---|
| 15.3–55.4 | 595 | 35.8% | +2.99% | -2.72% | 1.70 | +0.13% | [-1.21%, +1.55%] | -2.70% | -0.52% | 22.7% |
| 55.4–63.1 | 594 | 35.2% | +2.09% | -3.06% | 1.49 | -1.21% | [-2.62%, +0.32%] | -3.47% | -4.97% | 21.0% |
| 63.1–71.1 | 594 | 38.7% | +2.79% | -2.17% | 1.73 | -0.20% | [-1.18%, +0.85%] | -2.72% | -1.64% | 20.7% |
| 71.1–79.6 | 594 | 35.4% | +1.98% | -2.84% | 1.51 | -0.61% | [-1.88%, +0.78%] | -3.38% | -4.70% | 18.2% |
| 79.6–82.9 | 594 | 35.9% | +1.64% | -2.24% | 1.45 | +0.04% | [-1.09%, +1.24%] | -2.29% | -3.48% | 15.3% |
| 82.9–92.6 | 594 | 37.7% | +2.87% | -2.61% | 1.71 | -0.04% | [-1.49%, +1.43%] | -2.97% | -1.07% | 21.7% |
| 92.6–100.0 | 459 | 34.9% | +2.14% | -2.91% | 1.53 | -0.39% | [-1.58%, +0.86%] | -2.54% | -1.94% | 20.9% |
| 100.0–100.0 | 1918 | 36.3% | +1.98% | -2.36% | 1.54 | -0.73% | [-1.38%, +0.01%] | -3.02% | -1.59% | 18.4% |

| Against | n | Spearman | p (independence assumed) |
|---|---|---|---|
| market_delta | 5942 | 0.0132 | 0.308 |
| r_trade | 5942 | 0.0170 | 0.189 |
| holding_days | 5942 | -0.0519 | 6.28e-05 |
| r_overlay | 5939 | 0.0346 | 0.00764 |

Top-minus-bottom mean market delta, with the cut recomputed inside every replicate:

| Cut | Spread | 95% CI |
|---|---|---|
| top/bottom 25% | -0.45% | [-1.59%, +0.64%] |
| top/bottom 10% | -0.86% | [-2.33%, +0.70%] |

Held out at 2022 — 3262 trades in, 2680 held out:

| Period | Spearman vs market delta | Top/bottom 10% spread |
|---|---|---|
| < 2022 | -0.0402 | -3.46% |
| ≥ 2022 | 0.0763 | +2.32% |

## Caveats

- Every threshold is exactly as SPEC.md §6 pins it. Nothing here proposes a change.
- The other 60% of the composite — Quality, Option economics, Valuation — is
  untested, because it needs point-in-time fundamentals and historical option
  chains this project does not have. That is unmeasured, not measured and found
  wanting.
- The backtest enters on §4's trend and stochastic signals alone, with none of the
  liquidity, IV, earnings-distance or quality gates the presets add, so this
  population is broader than the live screener's.
- Holding period is an *outcome*. Where a subscore ranks against it, that is not a
  selectable strategy.
- Every bias in SPEC-BACKTEST.md §7 applies unchanged; the trades are the same ones.

Educational and personal tooling on delayed, unofficial data. Not financial advice.
