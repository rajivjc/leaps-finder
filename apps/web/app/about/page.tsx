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

/**
 * A boxed caveat inside a section, for a claim the reader needs before they
 * trust the formulas around it. `h3` rather than `h2`: it sits under a
 * section heading, and skipping a level would break the document outline.
 */
function Caveat({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4">
      <h3 className="text-sm font-semibold text-[var(--foreground)]">{title}</h3>
      <div className="mt-2 space-y-3">{children}</div>
    </div>
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

        <Caveat title="How much of this score has actually been tested">
          <p>
            The formulas below describe how the score is <em>built</em>. Whether it predicts
            anything is a separate question, and the honest answer is that most of it has never
            been measured.
          </p>
          <p>
            <strong className="text-[var(--foreground)]">
              Trend and Entry — 40% of the composite — have been tested and show nothing.
            </strong>{" "}
            Both were recomputed for every trade in the ten-year backtest, at the historical date
            the scanner would have scored it, and compared against how that trade went relative to
            the market over the same days. There is no detectable relationship. Sorting trades by
            Trend score does separate them by raw return, but not by return <em>versus the
            market</em> — which is the only kind that pays for the cost of buying an option
            instead of the stock.
          </p>
          <p>
            <strong className="text-[var(--foreground)]">
              The other 60% — Quality, Option economics and Valuation — has never been tested at
              all.
            </strong>{" "}
            Doing so needs point-in-time fundamentals and historical option chains as they stood
            on each past date, and this project has neither. That is the part of the score the
            strategy actually rests on, and it is unmeasured rather than measured and found
            wanting.
          </p>
          <p>
            One thing this does <em>not</em> mean: that a high score is worse than a low one. The
            preset filters run first, so the score only ever orders names that have already
            cleared every gate. Asking which of several uptrends is the strongest uptrend simply
            turns out not to be a useful question. Treat the composite as a consistent way to
            order a filtered list, not as evidence that the names at the top will do better.
          </p>
          <p>
            The timing signal underneath all of this was also evaluated over the same ten years,
            and did not beat a buy-and-hold of the same names. Those results, with every
            approximation behind them, are on the{" "}
            <a
              className="underline underline-offset-2 hover:text-[var(--foreground)]"
              href="/backtest"
            >
              backtest page
            </a>
            .
          </p>
        </Caveat>

        <p>
          Every subscore is built from one helper, <code className="font-mono">clip_map</code>, a
          piecewise-linear ramp: below <code className="font-mono">x0</code> it is 0, above{" "}
          <code className="font-mono">x1</code> it is 100, and in between it interpolates.
        </p>
        <Formula>{`clip_map(x, x0, x1) = 100 * clamp((x - x0) / (x1 - x0), 0, 1)`}</Formula>

        <p>
          <strong className="text-[var(--foreground)]">Trend (weight 0.25)</strong> — the mean of
          three terms, then multiplied by an extension penalty:
        </p>
        <Formula>{`mean of  clip_map(close / sma200 - 1, 0, 0.25)
         clip_map(sma50 / sma200 - 1, 0, 0.10)
         clip_map(share of last 60 sessions closing above SMA50, 0.5, 1.0)

penalty = min(1, clip_map(0.20 - (close / sma50 - 1), 0, 0.10) / 100)`}</Formula>
        <p>
          The penalty is why a runaway chart does not top this list: a stock more than 20% above
          its 50-day average scores zero on that gate outright. Parabolic is not better.
        </p>

        <p>
          <strong className="text-[var(--foreground)]">Quality (0.25)</strong> — the mean of five
          terms. A metric that is missing is excluded from the mean rather than being scored as
          average, and the omission is flagged rather than hidden:
        </p>
        <Formula>{`mean of  clip_map(operating margin, 0.05, 0.30)
         clip_map(return on equity, 0.08, 0.30)
         clip_map(3 - net debt / EBITDA, 0, 3)     <- net cash scores 100
         clip_map(revenue growth TTM YoY, 0, 0.20)
         clip_map(free cash flow margin, 0, 0.20)`}</Formula>

        <p>
          <strong className="text-[var(--foreground)]">Option economics (0.20)</strong> — cheap,
          liquid and calm scores well:
        </p>
        <Formula>{`mean of  clip_map(50 - iv_rank, 0, 50)
         clip_map(0.40 - iv30, 0, 0.25)
         clip_map(0.30 - cost_pct_spot, 0, 0.15)
         clip_map(0.10 - spread_pct, 0, 0.08)
         clip_map(open interest, 100, 2000)`}</Formula>

        <p>
          <strong className="text-[var(--foreground)]">Valuation and upside (0.15)</strong> — the
          analyst mean target is cut by 40% before it is believed at all:
        </p>
        <Formula>{`upside_adj = 0.6 * (analyst_target / spot - 1)

mean of  clip_map(upside_adj, 0, 0.25)
         inverted forward-P/E percentile within this scan's universe`}</Formula>

        <p>
          <strong className="text-[var(--foreground)]">Entry (0.15)</strong> — where in the zone
          the oscillator sits, and how fresh the turn is:
        </p>
        <Formula>{`zone position : peaks over slowK 25-45, falling linearly to 0 at 20 and at 70
freshness    : 100 if slowK crossed above D within the last 2 completed weeks
                60 if within 4
                30 otherwise`}</Formula>
        <p>
          A subscore that cannot be computed is null, and a composite missing any of its five
          terms is null too — shown as an em dash. It is never filled in with a zero, which would
          be a claim that the factor was measured and found to be as bad as possible.
        </p>
      </Section>

      <Section title="Implied volatility rank">
        <p>
          IV rank is computed from this project&rsquo;s own daily ATM snapshots, not bought from
          anyone:
        </p>
        <Formula>{`iv_rank = (iv30 - min(iv30 over 252 snapshots)) / (max - min)`}</Formula>
        <p>
          That range needs history this project has not accumulated yet. Until a symbol has at
          least 120 snapshots of its own, its status is <em>warming up</em> and a substitute
          stands in on the same 0–100 scale: the cross-sectional percentile of{" "}
          <code className="font-mono">iv30 / rv20</code> across the scanned universe, where{" "}
          <code className="font-mono">rv20</code> is 20-day realised volatility, annualised. The
          preset thresholds apply to whichever value is in force.
        </p>
        <p>
          Every symbol will read <em>warming up</em> for roughly the first six months of this
          project&rsquo;s life. The badge is always shown. A substitute labelled as the real thing
          would be the most quietly misleading number on the page.
        </p>
      </Section>

      <Section title="Presets">
        <p>
          The presets are hard filters applied before ranking — they decide which names appear at
          all, and the score only orders what survives. The tiers nest: everything that clears
          Strict also clears Balanced and Wide.
        </p>
        <div className="overflow-x-auto rounded-md border border-[var(--border)]">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--surface)] text-left">
                <th className="px-3 py-2 font-medium">Filter</th>
                <th className="px-3 py-2 font-medium">Strict</th>
                <th className="px-3 py-2 font-medium">Balanced</th>
                <th className="px-3 py-2 font-medium">Wide open</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {[
                ["Trend pass", "required", "required", "required"],
                ["slowK zone", "20–55 + turning up", "20–70 + turning up", "10–80"],
                ["IV rank", "≤ 30", "≤ 50", "—"],
                ["Earnings distance", "≥ 30d", "≥ 14d", "—"],
                ["Spread % of mid", "≤ 5%", "≤ 8%", "≤ 12%"],
                ["Open interest", "≥ 500", "≥ 200", "≥ 100"],
                ["Quality", "all metrics present, ≥ 60", "≥ 45", "—"],
              ].map(([filter, strict, balanced, wide]) => (
                <tr key={filter} className="border-b border-[var(--border)] last:border-0">
                  <td className="px-3 py-1.5 font-sans">{filter}</td>
                  <td className="px-3 py-1.5">{strict}</td>
                  <td className="px-3 py-1.5">{balanced}</td>
                  <td className="px-3 py-1.5">{wide}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          A gate whose input is unknown fails rather than passes. A symbol with no earnings date
          cannot clear &ldquo;earnings ≥ 30 days&rdquo;, and a symbol with no valid contract has no
          spread or open interest to clear any tier with.
        </p>
      </Section>

      <Section title="Risk discipline">
        <p>Position size is mechanical:</p>
        <Formula>{`max_premium_dollars = 0.03 * account_equity
max_contracts       = floor(max_premium_dollars / (mid * 100))`}</Formula>
        <p>
          Alongside that: at most 15% of equity in open premium across the whole sleeve, at most
          two positions per sector, and five open at a time. Those are warnings rather than hard
          blocks — they can be overridden deliberately, and the override is logged.
        </p>
        <p>Exits are mechanical too, and evaluated by the scanner rather than by judgement:</p>
        <Formula>{`trend break   : close < SMA200 or SMA50 < SMA200        (daily)
stochastic    : weekly slowK crosses below 20          (weekly)
premium stop  : current mid <= 50% of entry premium    (daily)
time exit     : DTE < 180                              (daily)
earnings      : earnings within 21d (informational)    (daily)
circuit break : sleeve loss >= 8% of entry equity
                -> no new entries for four weeks`}</Formula>
        <p>
          &ldquo;Crosses below 20&rdquo; means exactly that — a transition, not a state. A
          stochastic that has been sitting under 20 for six weeks is not firing an exit signal
          every week.
        </p>
        <p>
          Three details the formulas above leave open, resolved here rather than left to chance.
          The circuit breaker&rsquo;s denominator is the account equity recorded against the most
          recently opened position — a snapshot frozen at entry, so editing the current equity
          figure cannot defuse a breaker that has already tripped. Its numerator is realised P&amp;L
          on positions closed in the trailing four weeks plus unrealised P&amp;L on everything
          still open. And the four-week ban runs from the day it trips: it is not lifted early by
          the sleeve recovering.
        </p>
        <p>
          Alerts do not repeat themselves into uselessness. Four of the six rules describe a state
          that holds for as long as it holds, so a fresh alert is suppressed while an
          unacknowledged one of the same kind is open on the same position — acknowledging it
          re-arms the rule, because the condition is still true tomorrow. The earnings heads-up
          fires once per report, and the circuit breaker once per four-week ban.
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
        <p>
          Everything on this site is a stored result of the weekly scan, including the charts — no
          page fetches a quote when you open it. The date at the top of each page is the Friday the
          numbers describe, and between Saturday scans they do not move. Weekly candles, the
          stochastic panel and the card sparklines are all drawn from series the scanner wrote, so
          the chart and the checklist beside it are always computed from the same bars.
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
