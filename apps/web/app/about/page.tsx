import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "About · LEAPS Finder",
  description:
    "The strategy, the exact formulas, the data caveats, and the disclaimer behind LEAPS Finder.",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      <div className="space-y-3 text-sm leading-relaxed text-[var(--muted)]">{children}</div>
    </section>
  );
}

function Formula({ children }: { children: React.ReactNode }) {
  return (
    <pre className="overflow-x-auto rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 font-mono text-xs text-[var(--foreground)]">
      {children}
    </pre>
  );
}

export default function AboutPage() {
  return (
    <div className="space-y-10">
      <section className="space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">About</h1>
        <p className="max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          LEAPS Finder screens US large caps for long-dated call entries, prices a roughly
          one-year 0.70-delta call for each match, and ranks the results with a score whose
          every component is shown as computed.
        </p>
      </section>

      <Section title="The five filters">
        <p>
          A name has to clear all five. Trend: the stock is above both its 50- and 200-day
          moving averages, with the 50 above the 200. Weekly stochastic: the oscillator sits in
          its entry zone and is turning up. Quality: margins, returns, leverage, growth, and free
          cash flow. Valuation: meaningful upside to the analyst mean target, after a haircut.
          Implied volatility: options are not expensive relative to this name&rsquo;s own history.
        </p>
      </Section>

      <Section title="Signals (exact)">
        <p>
          Daily bars are resampled to weekly (Friday-ending), and the current in-progress week is
          dropped — signals only ever use completed weekly bars, so nothing repaints. The weekly
          slow stochastic is a (10, 3, 3):
        </p>
        <Formula>{`rawK_t = 100 * (C_t - min(L, 10w)) / (max(H, 10w) - min(L, 10w))
slowK  = SMA_3(rawK)
D      = SMA_3(slowK)

in_zone    : 20 <= slowK <= 70
turning_up : slowK > D  AND  slowK > slowK[-1]
exit       : slowK crosses below 20 on a weekly close`}</Formula>
        <p>Trend is evaluated on daily closes:</p>
        <Formula>{`trend_pass  : close > SMA50 AND close > SMA200 AND SMA50 > SMA200
trend_break : close < SMA200 OR SMA50 < SMA200`}</Formula>
      </Section>

      <Section title="The contract and its economics">
        <p>
          Among expiries at least 350 days out, the nearest one is preferred. Every call in it
          with a live two-sided quote gets a Black-Scholes delta, using the contract&rsquo;s own
          implied volatility, the 13-week T-bill as the risk-free rate, and the trailing dividend
          yield:
        </p>
        <Formula>{`d1    = (ln(S/K) + (r - q + sigma^2 / 2) * T) / (sigma * sqrt(T))
delta = e^(-qT) * N(d1)`}</Formula>
        <p>The strike whose delta is nearest 0.70 wins, and its economics are reported as:</p>
        <Formula>{`mid           = (bid + ask) / 2
breakeven     = strike + mid
breakeven_pct = breakeven / spot - 1
cost_pct_spot = mid / spot
spread_pct    = (ask - bid) / mid
cushion       = (target_adj - breakeven) / breakeven`}</Formula>
        <p>
          Cushion is measured against breakeven, not spot. That is the honest denominator: it is
          the move the stock actually has to make before the option is worth its premium.
        </p>
      </Section>

      <Section title="The score">
        <p>
          Five raw 0–100 subscores combine into the composite. Nothing is curved, rescaled, or
          rounded upward for presentation:
        </p>
        <Formula>{`composite = 0.25 * Trend
          + 0.25 * Quality
          + 0.20 * OptionEconomics
          + 0.15 * Valuation
          + 0.15 * Entry`}</Formula>
        <p>
          The trend subscore carries an extension penalty: a stock more than 20% above its 50-day
          average scores zero on that gate. Parabolic is not better. A quality metric that is
          missing is excluded from its mean and flagged in the interface rather than being
          quietly treated as average.
        </p>
        <p>
          IV rank is computed from this project&rsquo;s own daily snapshots. Until a symbol has
          accumulated at least 120 of them, it is labelled <em>warming up</em> and a
          cross-sectional proxy stands in — the badge is shown, never hidden.
        </p>
      </Section>

      <Section title="Risk discipline">
        <p>
          Position size is capped at 3% of account equity in premium per trade, with a 15% cap
          across the whole sleeve, at most two positions per sector, and five open at a time.
          Exits are mechanical: a trend break, a weekly stochastic cross below 20, premium down
          50% from entry, or fewer than 180 days to expiry. A sleeve drawdown of 8% or more stops
          new entries for four weeks.
        </p>
      </Section>

      <Section title="Data caveats">
        <p>
          Prices, fundamentals, option chains, and analyst targets come from Yahoo Finance
          through the yfinance client. That data is delayed, unofficial, occasionally wrong, and
          can disappear without notice. Option quotes in particular may be stale outside market
          hours, and a mid-price is not a fill. Scans that lose more than a fifth of the universe
          to fetch failures are recorded as failed rather than presented as complete.
        </p>
      </Section>

      <Section title="Disclaimer">
        <p>
          This is an educational and personal tool. Nothing in this application is financial
          advice, a recommendation, or an offer to trade anything. Long-dated call options can
          and regularly do expire worthless — you can lose the entire premium. Do your own
          research and verify every number with your broker before acting on it.
        </p>
      </Section>
    </div>
  );
}
