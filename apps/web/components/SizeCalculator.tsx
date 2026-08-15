"use client";

/**
 * Position sizing (SPEC.md §7), client-side only.
 *
 *     max_premium_dollars = 0.03 · equity
 *     max_contracts       = floor(max_premium_dollars / (mid · 100))
 *
 * Nothing here is persisted. §7 has the equity saved per user, but that needs
 * auth and the `positions` table, which are M5 — so this milestone does the
 * arithmetic and stops there rather than half-implementing storage. The sleeve
 * meters (15% exposure cap, 2-per-sector, 5 positions) need open positions to
 * measure, so they land with M5 too; the caps are stated here so the number
 * this calculator gives is read in context.
 */

import { useState } from "react";

import { fmtInteger, fmtPercent, fmtUsd } from "@/lib/format";
import {
  MAX_PER_SECTOR,
  MAX_POSITIONS,
  MAX_PREMIUM_FRACTION,
  SLEEVE_EXPOSURE_CAP,
  positionSize,
} from "@/lib/metrics";

export function SizeCalculator({ mid, symbol }: { mid: number | null; symbol: string }) {
  const [equity, setEquity] = useState("");

  const parsed = equity.trim() === "" ? null : Number(equity);
  const size = positionSize(parsed, mid);

  return (
    <div className="space-y-3">
      <label className="block space-y-1">
        <span className="text-xs text-[var(--muted)]">Account equity ($)</span>
        <input
          type="number"
          inputMode="decimal"
          min={0}
          value={equity}
          onChange={(event) => setEquity(event.target.value)}
          placeholder="100000"
          className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1.5 text-sm tabular-nums"
        />
      </label>

      {mid === null ? (
        <p className="text-xs leading-relaxed text-[var(--muted)]">
          {symbol} has no contract with a two-sided quote in this scan, so there is no premium to
          size against.
        </p>
      ) : size === null ? (
        <p className="text-xs leading-relaxed text-[var(--muted)]">
          Enter your account equity. The rule is {fmtPercent(MAX_PREMIUM_FRACTION, 0)} of equity as
          premium per position, in whole contracts.
        </p>
      ) : (
        <div className="space-y-2">
          <dl className="divide-y divide-[var(--border)] text-sm">
            <div className="flex justify-between py-1.5">
              <dt className="text-xs text-[var(--muted)]">
                Budget ({fmtPercent(MAX_PREMIUM_FRACTION, 0)} of equity)
              </dt>
              <dd className="font-mono tabular-nums">{fmtUsd(size.maxPremiumDollars, 0)}</dd>
            </div>
            <div className="flex justify-between py-1.5">
              <dt className="text-xs text-[var(--muted)]">Cost per contract (mid × 100)</dt>
              <dd className="font-mono tabular-nums">{fmtUsd(mid * 100, 0)}</dd>
            </div>
            <div className="flex justify-between py-1.5">
              <dt className="text-xs font-medium">Max contracts</dt>
              <dd className="font-mono text-base tabular-nums">{fmtInteger(size.maxContracts)}</dd>
            </div>
            <div className="flex justify-between py-1.5">
              <dt className="text-xs text-[var(--muted)]">Actually deployed</dt>
              <dd className="font-mono tabular-nums">
                {fmtUsd(size.deployedDollars, 0)} ({fmtPercent(size.deployedFraction)} of equity)
              </dd>
            </div>
          </dl>

          {size.maxContracts === 0 && (
            <p className="rounded border border-[var(--warn-border)] bg-[var(--warn-bg)] p-2 text-xs leading-relaxed text-[var(--warn-fg)]">
              One contract costs {fmtUsd(mid * 100, 0)}, more than the{" "}
              {fmtPercent(MAX_PREMIUM_FRACTION, 0)} budget of{" "}
              {fmtUsd(size.maxPremiumDollars, 0)}. Taking this trade at all would breach the sizing
              rule.
            </p>
          )}
        </div>
      )}

      <p className="text-[10px] leading-relaxed text-[var(--muted)]">
        §7 also caps the sleeve at {fmtPercent(SLEEVE_EXPOSURE_CAP, 0)} of equity in open premium,{" "}
        {MAX_PER_SECTOR} positions per sector and {MAX_POSITIONS} positions overall. Those are
        measured against open positions, which arrive with the positions page. Nothing entered here
        is saved.
      </p>
    </div>
  );
}
