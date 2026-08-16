"use client";

/**
 * The backtest's equity curves (SPEC-BACKTEST.md §8), on the repo's chart stack.
 *
 * Series are read from props and never recomputed here, exactly as `PriceChart`
 * reads its moving averages from the row: every number on this chart was
 * produced by the Python run that wrote `results.json`, so the page cannot
 * disagree with the committed report about what the run found.
 */

import { LineSeries, createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { CHART_BASE_OPTIONS, createSizedChart } from "@/components/PriceChart";

export type CurveSeries = {
  label: string;
  colour: string;
  points: { time: string; value: number }[];
};

export function BacktestChart({
  series,
  format,
}: {
  series: CurveSeries[];
  /** Dollars for the sleeve's own equity, multiples when curves are normalized. */
  format: "usd" | "multiple";
}) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = container.current;
    if (!element || series.length === 0) return;

    const { chart, fit, dispose } = createSizedChart(element, {
      ...CHART_BASE_OPTIONS,
      localization: {
        priceFormatter: (value: number) =>
          format === "usd"
            ? `$${Math.round(value).toLocaleString("en-US")}`
            : `${value.toFixed(2)}×`,
      },
    } as Parameters<typeof createChart>[1]);

    for (const item of series) {
      const line = chart.addSeries(LineSeries, {
        color: item.colour,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: item.label,
      });
      line.setData(item.points);
    }
    fit();

    return dispose;
  }, [series, format]);

  return (
    <div className="space-y-2">
      <div ref={container} className="h-[320px] w-full" />
      <ul className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-[var(--muted)]">
        {series.map((item) => (
          <li key={item.label} className="flex items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-[3px] w-5 rounded"
              style={{ backgroundColor: item.colour }}
            />
            {item.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
