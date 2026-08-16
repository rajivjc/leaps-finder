"use client";

/**
 * Account equity, persisted per user (SPEC.md §7, migration 0004's
 * `user_settings`).
 *
 * This is the denominator for the 3% per-position budget and the 15% sleeve
 * cap. It is deliberately *not* the same number as
 * `positions.account_equity_at_entry`: that one is a frozen snapshot per trade
 * and is what §7's circuit breaker measures against, so editing this field can
 * never retroactively defuse a breaker that has already tripped.
 */

import { saveEquity } from "@/app/positions/actions";
import { ActionForm, SubmitButton } from "@/components/ActionForm";
import { fmtUsd } from "@/lib/format";
import { MAX_PREMIUM_FRACTION } from "@/lib/metrics";

export function EquityForm({ equity }: { equity: number | null }) {
  return (
    <ActionForm action={saveEquity} className="space-y-2">
      {(pending) => (
        <>
          <div className="flex flex-wrap items-end gap-2">
            <label className="space-y-1">
              <span className="block text-xs text-[var(--muted)]">Account equity ($)</span>
              <input
                type="number"
                name="account_equity"
                inputMode="decimal"
                min={0}
                step="any"
                required
                defaultValue={equity ?? ""}
                placeholder="100000"
                className="w-40 rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1.5 text-sm tabular-nums"
              />
            </label>
            <SubmitButton pending={pending}>Save</SubmitButton>
          </div>
          <p className="text-[10px] leading-relaxed text-[var(--muted)]">
            {equity === null
              ? "Not set yet — the sizing budget and the sleeve exposure meter stay blank until it is."
              : `Per-position premium budget: ${fmtUsd(MAX_PREMIUM_FRACTION * equity, 0)}. Saved here, and snapshotted onto each position as you open it.`}
          </p>
        </>
      )}
    </ActionForm>
  );
}
