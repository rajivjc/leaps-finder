"use client";

/**
 * Weekly slow stochastic (10,3,3) with the 20/70 band (SPEC.md §8.2, §4).
 *
 * The band is not decoration: 20–70 is the `in_zone` gate, 20 is where the exit
 * signal fires, and the Strict preset narrows the top of it to 55. Drawing %K
 * without those lines would leave the reader to guess the thresholds the whole
 * strategy turns on.
 *
 * The series is read from `weekly_bars`, written by the scanner — the same
 * numbers the row was scored from, not a second implementation of the formula.
 */

import { LineSeries, LineStyle } from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";

import { ChartEmpty, Legend, createSizedChart } from "@/components/PriceChart";
import type { WeeklyBar } from "@/lib/types";

const ZONE_LOW = 20;
const ZONE_HIGH = 70;

export function StochasticPanel({ bars }: { bars: WeeklyBar[] }) {
  const container = useRef<HTMLDivElement>(null);
  // Memoised so the effect below does not tear down and rebuild the chart on
  // every render just because `filter` returned a new array identity.
  const usable = useMemo(() => bars.filter((bar) => bar.slow_k !== null), [bars]);

  useEffect(() => {
    const element = container.current;
    if (!element || usable.length === 0) return;

    const { chart, fit, dispose } = createSizedChart(element);

    const slowK = chart.addSeries(LineSeries, {
      color: "#111827",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      // Pinned to the indicator's own 0–100 range rather than auto-scaled to
      // the visible data: the 20/70 band has to sit where it actually is, and a
      // %K oscillating between 45 and 50 must not fill the panel as if it had
      // swept the whole range.
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    slowK.setData(usable.map((bar) => ({ time: bar.week_ending, value: bar.slow_k as number })));

    const d = chart.addSeries(LineSeries, {
      color: "#9ca3af",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    d.setData(
      bars
        .filter((bar) => bar.d !== null)
        .map((bar) => ({ time: bar.week_ending, value: bar.d as number })),
    );

    for (const [level, title] of [
      [ZONE_LOW, "20 — zone floor / exit"],
      [ZONE_HIGH, "70 — zone ceiling"],
    ] as const) {
      slowK.createPriceLine({
        price: level,
        color: "#9ca3af",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title,
      });
    }

    fit();
    return dispose;
  }, [bars, usable]);

  if (usable.length === 0) {
    return <ChartEmpty>No stochastic history is stored for this symbol yet.</ChartEmpty>;
  }

  return (
    <div>
      <div ref={container} className="h-44 w-full" />
      <Legend
        items={[
          { color: "#111827", label: "slow %K" },
          { color: "#9ca3af", label: "%D (3-period average of %K)" },
        ]}
      />
    </div>
  );
}
