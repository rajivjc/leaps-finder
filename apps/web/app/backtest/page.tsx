/**
 * `/backtest` — the committed backtest run (SPEC-BACKTEST.md §8).
 *
 * A server component reading the JSON at build time. No Supabase, no migration:
 * the numbers here were produced by a manual local run of the Python scanner and
 * committed under `docs/backtest/<run-date>/`.
 *
 * **The banner renders above any metric** (acceptance 6). That is a layout rule
 * with teeth behind it — `loadBacktest` throws when the banner is absent, so a
 * build that could render numbers without it does not complete. Nothing on this
 * page is rescaled, curve-fitted or rounded up: results are shown as computed,
 * including the ones that make the strategy look bad.
 */

import type { Metadata } from "next";

import { BacktestChart, type CurveSeries } from "@/components/BacktestChart";
import { Notice } from "@/components/Notice";
import { fmtNumber, fmtPercent, fmtUsd } from "@/lib/format";
import {
  type BacktestResults,
  type SleeveStats,
  type TrackStats,
  loadBacktest,
  orderedTrack,
  toLine,
} from "@/lib/backtest";

export const metadata: Metadata = {
  title: "Backtest · LEAPS Finder",
  description:
    "A ten-year historical evaluation of the entry/exit timing signal, with a synthetic LEAP overlay, a sleeve simulation and its full bias register.",
};

const SERIES_COLOURS = ["#2563eb", "#dc2626", "#059669", "#9333ea", "#ea580c"];

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {subtitle ? (
        <p className="max-w-3xl text-sm leading-relaxed text-[var(--muted)]">{subtitle}</p>
      ) : null}
      {children}
    </section>
  );
}

function Table({ headers, rows }: { headers: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
      <table className="w-full min-w-[38rem] text-sm">
        <thead className="bg-[var(--surface)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
          <tr>
            {headers.map((header) => (
              <th key={header} className="whitespace-nowrap px-3 py-2 font-medium">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border)]">
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className="whitespace-nowrap px-3 py-2 font-mono text-xs tabular-nums"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Acceptance 2: below §2.4's 85% floor every headline table carries a warning.
 * Rendered from the flag the Python statistics carry, not recomputed here.
 */
function CoverageWarning({ stats }: { stats: { low_coverage_warning: boolean | null; coverage_ratio: number | null } }) {
  if (stats.low_coverage_warning !== true) return null;
  return (
    <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
      Low coverage: {fmtPercent(stats.coverage_ratio, 2)} of member-weeks. Every figure in this
      table is computed over a thinned universe.
    </p>
  );
}

/**
 * Render one line of the banner's `**bold**` markup.
 *
 * The banner travels from the spec as Markdown and must arrive word for word,
 * so it is rendered rather than rewritten. Only bold is supported because bold
 * is all §1 uses — an unrecognized marker would show as literal asterisks,
 * which is the right failure: visible, and never a silent edit of the caveat.
 */
function Emphasized({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
        part.startsWith("**") && part.endsWith("**") ? (
          <strong key={index} className="font-semibold text-[var(--foreground)]">
            {part.slice(2, -2)}
          </strong>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

/**
 * The §1 banner as prose.
 *
 * The source is hard-wrapped at the spec's column width, so lines within a
 * paragraph are rejoined — a mid-sentence break rendered literally is how a
 * caveat starts looking like boilerplate nobody reads. Bullet blocks keep their
 * line structure because there the breaks are meaningful.
 */
function Banner({ text }: { text: string }) {
  return (
    <div className="mt-3 space-y-3 text-sm leading-relaxed text-[var(--muted)]">
      {text.split("\n\n").map((block, index) => {
        const lines = block.split("\n");
        if (lines.every((line) => line.startsWith("- "))) {
          return (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {lines.map((line, lineIndex) => (
                <li key={lineIndex}>
                  <Emphasized text={line.slice(2)} />
                </li>
              ))}
            </ul>
          );
        }
        const [lead, ...rest] = lines;
        const isLabelledList = lead.endsWith(":**") && rest.every((line) => line.startsWith("- "));
        if (isLabelledList) {
          return (
            <div key={index} className="space-y-1">
              <p>
                <Emphasized text={lead} />
              </p>
              <ul className="list-disc space-y-1 pl-5">
                {rest.map((line, lineIndex) => (
                  <li key={lineIndex}>
                    <Emphasized text={line.slice(2)} />
                  </li>
                ))}
              </ul>
            </div>
          );
        }
        return (
          <p key={index}>
            <Emphasized text={lines.join(" ")} />
          </p>
        );
      })}
    </div>
  );
}

function trackRow(stats: TrackStats): React.ReactNode[] {
  return [
    stats.variant,
    fmtNumber(stats.trades, 0),
    fmtNumber(stats.chains, 0),
    fmtPercent(stats.win_rate, 1),
    fmtPercent(stats.mean_return, 2, { signed: true }),
    fmtPercent(stats.median_return, 2, { signed: true }),
    fmtNumber(stats.profit_factor, 2),
    stats.vehicle_alpha
      ? fmtPercent(stats.vehicle_alpha.mean, 2, { signed: true })
      : "—",
  ];
}

function sleeveRow(sleeve: SleeveStats): React.ReactNode[] {
  return [
    sleeve.name,
    fmtUsd(sleeve.start_equity, 0),
    fmtUsd(sleeve.final_equity, 0),
    fmtPercent(sleeve.total_return, 2, { signed: true }),
    fmtPercent(sleeve.cagr, 2, { signed: true }),
    fmtPercent(sleeve.max_drawdown, 2),
    fmtNumber(sleeve.positions, 0),
    fmtPercent(sleeve.win_rate, 1),
    fmtNumber(sleeve.breaker_activations, 0),
  ];
}

function buildSeries(results: BacktestResults): { equity: CurveSeries[]; relative: CurveSeries[] } {
  const dates = results.curve_dates;
  const equity: CurveSeries[] = results.sleeves.map((sleeve, index) => ({
    label: `Sleeve ${sleeve.name}`,
    colour: SERIES_COLOURS[index % SERIES_COLOURS.length],
    points: toLine(dates, results.sleeve_curves[sleeve.name] ?? []),
  }));

  // Normalized so the sleeve and the benchmarks share one axis. The sleeve's own
  // start equity is the divisor, which is what makes "1.0" mean the same thing
  // on every line here.
  const relative: CurveSeries[] = [
    ...results.sleeves.map((sleeve, index) => ({
      label: `Sleeve ${sleeve.name}`,
      colour: SERIES_COLOURS[index % SERIES_COLOURS.length],
      points: toLine(dates, results.sleeve_curves[sleeve.name] ?? []).map((point) => ({
        time: point.time,
        value: point.value / sleeve.start_equity,
      })),
    })),
    ...results.benchmarks.map((benchmark, index) => ({
      label: benchmark.label,
      colour: SERIES_COLOURS[(results.sleeves.length + index) % SERIES_COLOURS.length],
      points: toLine(dates, results.benchmark_curves[benchmark.name] ?? []),
    })),
  ];

  return { equity, relative };
}

export default function BacktestPage() {
  const loaded = loadBacktest();

  if (loaded === null) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold tracking-tight">Backtest</h1>
        <Notice title="No run published yet">
          The backtest is a manual, local analysis. Its results are committed under
          <code className="mx-1 font-mono text-xs">docs/backtest/</code>; this page renders the
          most recent one at build time.
        </Notice>
      </div>
    );
  }

  const { runDate, results } = loaded;
  const stock = orderedTrack(results.track_a.stock, results.track_a_order?.stock);
  const overlay = orderedTrack(results.track_a.overlay, results.track_a_order?.overlay);
  const headline = results.track_a.overlay.base ?? overlay[0];
  const { equity, relative } = buildSeries(results);
  const exposureCaveat = results.benchmarks.find((item) => item.caveat)?.caveat;

  return (
    <div className="space-y-10">
      <section className="space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">Backtest</h1>
        <p className="max-w-3xl text-sm leading-relaxed text-[var(--muted)]">
          A ten-year replay of the entry/exit timing signal over point-in-time S&amp;P 500
          membership, {results.window.start} to {results.window.end}. Run {runDate}; cache
          snapshot {results.coverage.snapshot_date ?? "unrecorded"}.
        </p>
      </section>

      {/*
        Acceptance 6: the banner renders above any metric. Do not move a table,
        a chart or a headline number above this block.
      */}
      <section className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-5">
        <h2 className="text-sm font-semibold">What this backtest can and cannot claim</h2>
        <Banner text={results.banner} />
      </section>

      <Section
        title="Coverage"
        subtitle="How much of the point-in-time universe the run could actually price. No silent truncation: names with no data are counted, not dropped."
      >
        <Table
          headers={["Member-weeks", "Covered", "Ratio", "Members", "No data", "Status"]}
          rows={[
            [
              fmtNumber(results.coverage.total_member_weeks, 0),
              fmtNumber(results.coverage.covered_member_weeks, 0),
              fmtPercent(results.coverage.coverage_ratio, 2),
              fmtNumber(results.coverage.members, 0),
              fmtNumber(results.coverage.no_data_members, 0),
              results.coverage.status,
            ],
          ]}
        />
      </Section>

      <Section
        title="Track A — per-trade"
        subtitle="Every signal counted independently, one open trade per name per track. This is the primary result. The LEAP overlay is an approximation layer — a synthesized 0.70Δ strike, no liquidity gates, flat IV — and is not comparable to the live screener."
      >
        {headline ? <CoverageWarning stats={headline} /> : null}
        <h3 className="pt-2 text-sm font-medium">Stock track</h3>
        <Table
          headers={[
            "Variant",
            "Trades",
            "Names",
            "Win rate",
            "Mean",
            "Median",
            "Profit factor",
            "Vehicle alpha",
          ]}
          rows={stock.map(trackRow)}
        />
        <h3 className="pt-2 text-sm font-medium">LEAP overlay (§5.6 sensitivity grid)</h3>
        <Table
          headers={[
            "Config",
            "Trades",
            "Names",
            "Win rate",
            "Mean",
            "Median",
            "Profit factor",
            "Vehicle alpha",
          ]}
          rows={overlay.map(trackRow)}
        />
      </Section>

      <Section
        title="Track B — sleeve simulation"
        subtitle="The risk discipline applied to the overlay's signals: 3% premium per position, integer contracts, at most 5 open and 2 per sector, 15% open premium cap, cash at 0%, and the 8% circuit breaker enforced as a hard 28-day entry ban. Secondary to Track A — with five slots against thousands of signals, which trades get funded is decided largely by arrival order."
      >
        {results.sleeves[0] ? <CoverageWarning stats={results.sleeves[0]} /> : null}
        <Table
          headers={[
            "Sleeve",
            "Start",
            "Final",
            "Total",
            "CAGR",
            "Max DD",
            "Positions",
            "Win rate",
            "Breaker trips",
          ]}
          rows={results.sleeves.map(sleeveRow)}
        />
        <div className="pt-2">
          <BacktestChart series={equity} format="usd" />
        </div>
      </Section>

      <Section
        title="Benchmarks"
        subtitle="Buy-and-hold comparisons over the same window. The equal-weight benchmark holds the distinct entered names for the full window — it answers whether the timing added anything beyond the names themselves."
      >
        <Table
          headers={["Benchmark", "Total return", "CAGR", "Max drawdown", "Constituents"]}
          rows={results.benchmarks.map((item) => [
            item.label,
            fmtPercent(item.total_return, 2, { signed: true }),
            fmtPercent(item.cagr, 2, { signed: true }),
            fmtPercent(item.max_drawdown, 2),
            item.constituents === null ? "—" : fmtNumber(item.constituents, 0),
          ])}
        />
        {exposureCaveat ? (
          <p className="rounded-md border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-xs leading-relaxed text-[var(--muted)]">
            {exposureCaveat}
          </p>
        ) : null}
        <div className="pt-2">
          <BacktestChart series={relative} format="multiple" />
        </div>
      </Section>

      <Section
        title="Bias register"
        subtitle="Every approximation in the backtest, with the direction it is expected to push the result. This travels with the run; it is not a footnote added afterwards."
      >
        <Table
          headers={["#", "Assumption", "Direction", "Note"]}
          rows={results.bias_register.map((row) => [
            row.row,
            <span key="a" className="whitespace-normal font-sans">
              {row.assumption}
            </span>,
            <span key="d" className="whitespace-normal font-sans">
              {row.direction}
            </span>,
            <span key="n" className="whitespace-normal font-sans">
              {row.note}
            </span>,
          ])}
        />
      </Section>

      <section className="border-t border-[var(--border)] pt-6 text-xs leading-relaxed text-[var(--muted)]">
        Educational and personal tooling on delayed, unofficial data. This is a simulation built
        on the approximations listed above. Past performance — simulated or otherwise — does not
        predict future results. <strong>Nothing here is financial advice.</strong>
      </section>
    </div>
  );
}
