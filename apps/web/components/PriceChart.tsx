"use client";

/**
 * Weekly candles with the daily SMA50/200 overlay (SPEC.md §8.2).
 *
 * The moving averages are read from the row, not recomputed here. §4 defines
 * the trend filter on the *daily* averages, and a 50-period average of these
 * weekly closes would be a roughly one-year line — a different indicator that
 * happened to share a name. Drawing it would put a chart on screen that
 * disagrees with the checklist beside it.
 */

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import type { WeeklyBar } from "@/lib/types";

export const CHART_BASE_OPTIONS = {
  layout: {
    background: { type: ColorType.Solid, color: "transparent" },
    textColor: "#6b7280",
    fontFamily: "var(--font-geist-sans), system-ui, sans-serif",
    attributionLogo: false,
  },
  grid: {
    vertLines: { color: "#f1f3f6" },
    horzLines: { color: "#f1f3f6" },
  },
  rightPriceScale: { borderColor: "#e5e7eb" },
  timeScale: { borderColor: "#e5e7eb" },
  crosshair: { mode: CrosshairMode.Normal },
  autoSize: true,
} as const;

type Candle = { time: string; open: number; high: number; low: number; close: number };

function toCandles(bars: WeeklyBar[]): Candle[] {
  const candles: Candle[] = [];
  for (const bar of bars) {
    if (bar.open === null || bar.high === null || bar.low === null || bar.close === null) continue;
    candles.push({
      time: bar.week_ending,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    });
  }
  return candles;
}

/** Line points for one nullable column, dropping the warm-up gap at the front. */
function toLine(bars: WeeklyBar[], key: "sma50" | "sma200") {
  return bars
    .filter((bar) => bar[key] !== null)
    .map((bar) => ({ time: bar.week_ending, value: bar[key] as number }));
}

export function PriceChart({ bars, symbol }: { bars: WeeklyBar[]; symbol: string }) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = container.current;
    if (!element || bars.length === 0) return;

    const chart = createChart(element, CHART_BASE_OPTIONS);

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#3f7d63",
      downColor: "#a84a4a",
      borderUpColor: "#3f7d63",
      borderDownColor: "#a84a4a",
      wickUpColor: "#3f7d63",
      wickDownColor: "#a84a4a",
    });
    candles.setData(toCandles(bars));

    const sma50 = chart.addSeries(LineSeries, {
      color: "#2563eb",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      title: "SMA50 (daily)",
    });
    sma50.setData(toLine(bars, "sma50"));

    const sma200 = chart.addSeries(LineSeries, {
      color: "#9333ea",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      title: "SMA200 (daily)",
    });
    sma200.setData(toLine(bars, "sma200"));

    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [bars]);

  if (bars.length === 0) {
    return (
      <ChartEmpty>
        No weekly bars are stored for {symbol} yet. The chart series is written by the weekly scan.
      </ChartEmpty>
    );
  }

  return (
    <div>
      <div ref={container} className="h-72 w-full" />
      <Legend
        items={[
          { color: "#2563eb", label: "SMA50 (daily)" },
          { color: "#9333ea", label: "SMA200 (daily)", dashed: true },
        ]}
      />
    </div>
  );
}

export function ChartEmpty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-72 items-center justify-center rounded border border-dashed border-[var(--border)] px-6 text-center text-sm text-[var(--muted)]">
      {children}
    </div>
  );
}

export function Legend({
  items,
}: {
  items: { color: string; label: string; dashed?: boolean }[];
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-4 text-[10px] text-[var(--muted)]">
      {items.map((item) => (
        <span key={item.label} className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-0 w-4"
            style={{
              borderTop: `2px ${item.dashed ? "dashed" : "solid"} ${item.color}`,
            }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}
