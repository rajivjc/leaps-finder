"use client";

/**
 * Position sizing (SPEC.md §7).
 *
 *     max_premium_dollars = 0.03 · equity
 *     max_contracts       = floor(max_premium_dollars / (mid · 100))
 *
 * §7 has the equity persisted per user, and as of M5 it is — in
 * `user_settings` (migration 0004). This page is public, though, so the
 * calculator has to work for a signed-out visitor too: it seeds from the saved
 * figure when there is a session, falls back to a local, unsaved value when
 * there is not, and says which of the two it is doing rather than leaving the
 * reader to guess whether their number was kept.
 *
 * The sleeve meters (15% exposure, 2 per sector, 5 positions) need open
 * positions to measure and live on /positions; the caps are named here so the
 * number this calculator gives is read in context.
 */

import { useState } from "react";

import { saveEquity } from "@/app/positions/actions";
import { ActionForm, SubmitButton } from "@/components/ActionForm";
import { fmtInteger, fmtPercent, fmtUsd } from "@/lib/format";
import {
  MAX_PER_SECTOR,
  MAX_POSITIONS,
  MAX_PREMIUM_FRACTION,
  SLEEVE_EXPOSURE_CAP,
  positionSize,
} from "@/lib/metrics";

export function SizeCalculator({
  mid,
  symbol,
  savedEquity = null,
  signedIn = false,
}: {
  mid: number | null;
  symbol: string;
  /** The owner's persisted equity, when there is a session. */
  savedEquity?: number | null;
  signedIn?: boolean;
}) {
  const [equity, setEquity] = useState(savedEquity === null ? "" : String(savedEquity));

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

      {signedIn && (
        <ActionForm action={saveEquity}>
          {(pending) => (
            <>
              <input type="hidden" name="account_equity" value={equity} />
              <SubmitButton pending={pending} variant="quiet">
                {parsed !== null && parsed === savedEquity ? "Saved" : "Save as my equity"}
              </SubmitButton>
            </>
          )}
        </ActionForm>
      )}

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
        {MAX_PER_SECTOR} positions per sector and {MAX_POSITIONS} positions overall — measured
        against your open positions on the{" "}
        <a href="/positions" className="underline">
          positions page
        </a>
        .{" "}
        {signedIn
          ? "Equity is saved to your account."
          : "Nothing entered here is saved; sign in to keep it."}
      </p>
    </div>
  );
}
