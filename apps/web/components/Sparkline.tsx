/**
 * Weekly stochastic sparkline for a screener card (SPEC.md §8.1).
 *
 * Plain inline SVG rather than a charting library: it renders on the server, it
 * costs nothing per card, and a 26-point line needs no interactivity. The
 * heavyweight `lightweight-charts` panels are reserved for the ticker page.
 *
 * The vertical scale is pinned to 0–100 — a stochastic's own fixed range — so
 * cards are comparable with each other. Auto-scaling each card to its own
 * min/max would make a line oscillating between 45 and 50 look identical to one
 * sweeping the full range.
 */

const WIDTH = 104;
const HEIGHT = 30;
const ZONE_LOW = 20;
const ZONE_HIGH = 70;

function y(value: number): number {
  // Value 0 sits at the bottom, 100 at the top.
  return HEIGHT - (Math.max(0, Math.min(100, value)) / 100) * HEIGHT;
}

export function Sparkline({ values, label }: { values: number[]; label?: string }) {
  if (values.length < 2) {
    return (
      <div
        className="flex items-center justify-center text-[10px] text-[var(--muted)]"
        style={{ width: WIDTH, height: HEIGHT }}
        title="Not enough weekly history to draw a stochastic line."
      >
        no history
      </div>
    );
  }

  const step = WIDTH / (values.length - 1);
  const points = values.map((value, index) => `${(index * step).toFixed(1)},${y(value).toFixed(1)}`);
  const last = values[values.length - 1];

  return (
    <svg
      width={WIDTH}
      height={HEIGHT}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={
        label ?? `weekly stochastic over the last ${values.length} completed weeks, now ${last.toFixed(0)}`
      }
      className="overflow-visible"
    >
      {/* The 20/70 entry zone, so the line is read against the thresholds that
          actually gate the presets rather than against an empty box. */}
      <rect
        x={0}
        y={y(ZONE_HIGH)}
        width={WIDTH}
        height={y(ZONE_LOW) - y(ZONE_HIGH)}
        fill="var(--track)"
      />
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke="var(--score-high)"
        strokeWidth={1.25}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={WIDTH} cy={y(last)} r={1.75} fill="var(--foreground)" />
    </svg>
  );
}
