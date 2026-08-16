"use client";

/**
 * Open a position (SPEC.md §8.4 CRUD).
 *
 * The form previews §7's cap breaches as you type, so the warning arrives
 * before the commitment rather than after it. That preview is advisory only:
 * the server action recomputes the same breaches from fresh data and writes
 * them to `sizing_override`, because a log assembled from whatever the client
 * chose to send is not a log.
 */

import { useState } from "react";

import { createPosition } from "@/app/positions/actions";
import { ActionForm, SubmitButton } from "@/components/ActionForm";
import { capBreaches } from "@/lib/metrics";

type OpenPosition = { symbol: string; contracts: number; entry_premium: number };

function Field({
  label,
  name,
  type = "text",
  hint,
  ...rest
}: {
  label: string;
  name: string;
  type?: string;
  hint?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="space-y-1">
      <span className="block text-xs text-[var(--muted)]">{label}</span>
      <input
        name={name}
        type={type}
        className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1.5 text-sm tabular-nums"
        {...rest}
      />
      {hint && <span className="block text-[10px] text-[var(--muted)]">{hint}</span>}
    </label>
  );
}

export function NewPositionForm({
  open,
  sectors,
  equity,
  today,
}: {
  open: OpenPosition[];
  /** symbol → sector, from the scanner's universe table. */
  sectors: Record<string, string | null>;
  equity: number | null;
  /** Today in UTC, resolved on the server so the default does not depend on the viewer's clock. */
  today: string;
}) {
  const [symbol, setSymbol] = useState("");
  const [contracts, setContracts] = useState("");
  const [premium, setPremium] = useState("");

  const parsedContracts = Number(contracts);
  const parsedPremium = Number(premium);
  const previewable =
    symbol.trim() !== "" &&
    Number.isFinite(parsedContracts) &&
    parsedContracts > 0 &&
    Number.isFinite(parsedPremium) &&
    parsedPremium > 0;

  const upper = symbol.trim().toUpperCase();
  const breaches = previewable
    ? capBreaches(
        {
          symbol: upper,
          sector: sectors[upper] ?? null,
          contracts: parsedContracts,
          entry_premium: parsedPremium,
        },
        open,
        (value) => sectors[value] ?? null,
        equity,
      )
    : [];

  return (
    <ActionForm action={createPosition} className="space-y-3">
      {(pending) => (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field
              label="Symbol"
              name="symbol"
              required
              value={symbol}
              onChange={(event) => setSymbol(event.target.value)}
              placeholder="AAPL"
              autoComplete="off"
            />
            <Field label="Opened on" name="opened_on" type="date" required defaultValue={today} />
            <Field label="Expiry" name="expiry" type="date" required />
            <Field label="Strike" name="strike" type="number" step="any" min={0} required />
            <Field
              label="Contracts"
              name="contracts"
              type="number"
              step={1}
              min={1}
              required
              value={contracts}
              onChange={(event) => setContracts(event.target.value)}
            />
            <Field
              label="Entry premium"
              name="entry_premium"
              type="number"
              step="any"
              min={0}
              required
              hint="Per share, not per contract."
              value={premium}
              onChange={(event) => setPremium(event.target.value)}
            />
          </div>

          {breaches.length > 0 && (
            <div className="rounded border border-[var(--warn-border)] bg-[var(--warn-bg)] p-3 text-xs leading-relaxed text-[var(--warn-fg)]">
              <p className="font-semibold">
                This entry breaches {breaches.length === 1 ? "a sizing cap" : "sizing caps"}:
              </p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {breaches.map((breach) => (
                  <li key={breach}>{breach}</li>
                ))}
              </ul>
              <p className="mt-2">
                §7 makes these warnings, not blocks — you can still open it, and the reasons are
                logged against the position.
              </p>
              <label className="mt-2 block space-y-1">
                <span className="block text-[10px]">Why override? (optional, logged)</span>
                <input
                  name="override_note"
                  className="w-full rounded border border-[var(--warn-border)] bg-[var(--background)] px-2 py-1 text-xs text-[var(--foreground)]"
                  placeholder="Sized up deliberately; conviction trade."
                />
              </label>
            </div>
          )}

          <SubmitButton pending={pending}>Add position</SubmitButton>
        </>
      )}
    </ActionForm>
  );
}
