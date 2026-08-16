"use client";

/**
 * Open and closed positions (SPEC.md §8.4).
 *
 * Two display rules run through this component.
 *
 * **No mark is an em dash, never a zero.** A position the daily refresh could
 * not price shows `—` for value and P&L, and says so. "We do not know what this
 * is worth" and "it is worth what you paid" are different facts, and only one
 * of them is safe to imply to someone holding the contract.
 *
 * **Every mark on the page comes from one run.** The loader selects marks by
 * the refresh's `scan_id`, so the date in the section header applies to every
 * row beneath it — no row is quietly a day fresher than its neighbour.
 */

import Link from "next/link";

import { closePosition, deletePosition } from "@/app/positions/actions";
import { ActionForm, SubmitButton } from "@/components/ActionForm";
import { EM_DASH, fmtDate, fmtInteger, fmtPercent, fmtUsd } from "@/lib/format";
import {
  TIME_EXIT_DTE,
  costBasis,
  daysBetween,
  positionPnl,
  premiumStopLevel,
} from "@/lib/metrics";
import type { Position, PositionMark } from "@/lib/types";

function Pnl({ position, mark }: { position: Position; mark: PositionMark | undefined }) {
  const pnl = positionPnl(position, mark?.mid);
  if (!pnl) {
    return <span className="text-[var(--muted)]">{EM_DASH}</span>;
  }

  const negative = pnl.dollars < 0;
  return (
    <span className={negative ? "text-[var(--fail)]" : "text-[var(--pass)]"}>
      {negative ? "−" : "+"}
      {fmtUsd(Math.abs(pnl.dollars), 0)}{" "}
      <span className="text-[var(--muted)]">({fmtPercent(pnl.fraction, 1, { signed: true })})</span>
    </span>
  );
}

function Cell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wide text-[var(--muted)]">{label}</dt>
      <dd className="font-mono text-sm tabular-nums">{children}</dd>
    </div>
  );
}

function OpenRow({
  position,
  mark,
  today,
}: {
  position: Position;
  mark: PositionMark | undefined;
  today: string;
}) {
  const dte = daysBetween(today, position.expiry);
  const stop = premiumStopLevel(position.entry_premium);

  return (
    <li className="rounded-lg border border-[var(--border)] p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">
          <Link href={`/t/${position.symbol}`} className="underline-offset-2 hover:underline">
            {position.symbol}
          </Link>{" "}
          <span className="font-mono font-normal text-[var(--muted)]">
            {fmtDate(position.expiry)} · {position.strike}C ×{fmtInteger(position.contracts)}
          </span>
        </h3>
        <span className="text-xs text-[var(--muted)]">
          opened {fmtDate(position.opened_on)}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Cell label="Entry">{fmtUsd(position.entry_premium)}</Cell>
        <Cell label="Cost">{fmtUsd(costBasis(position), 0)}</Cell>
        <Cell label="Mark">{mark?.mid != null ? fmtUsd(mark.mid) : EM_DASH}</Cell>
        <Cell label="P&L">
          <Pnl position={position} mark={mark} />
        </Cell>
        <Cell label="DTE">
          <span className={dte < TIME_EXIT_DTE ? "text-[var(--warn-fg)] font-semibold" : ""}>
            {fmtInteger(dte)}
          </span>
        </Cell>
      </dl>

      <p className="mt-2 text-[10px] leading-relaxed text-[var(--muted)]">
        {mark?.mid == null ? (
          <>
            No mark from the latest refresh — the premium stop could not be checked for this
            contract. §7&rsquo;s stop would fire at {fmtUsd(stop)}.
          </>
        ) : (
          <>
            §7 stop at {fmtUsd(stop)} (half the entry premium); currently {fmtUsd(mark.mid)}
            {mark.bid != null && mark.ask != null && (
              <> — bid {fmtUsd(mark.bid)} / ask {fmtUsd(mark.ask)}</>
            )}
            . Time exit under {TIME_EXIT_DTE} days to expiry.
          </>
        )}
      </p>

      {position.sizing_override && (
        <p className="mt-2 rounded border border-[var(--warn-border)] bg-[var(--warn-bg)] px-2 py-1 text-[10px] leading-relaxed text-[var(--warn-fg)]">
          <span className="font-semibold">Sizing override logged at entry:</span>{" "}
          {position.sizing_override}
        </p>
      )}

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-[var(--muted)] hover:text-[var(--foreground)]">
          Close or delete
        </summary>

        <div className="mt-3 space-y-4 border-t border-[var(--border)] pt-3">
          <ActionForm action={closePosition} className="space-y-2">
            {(pending) => (
              <>
                <input type="hidden" name="id" value={position.id} />
                <div className="grid gap-2 sm:grid-cols-3">
                  <label className="space-y-1">
                    <span className="block text-[10px] text-[var(--muted)]">Closed on</span>
                    <input
                      type="date"
                      name="closed_on"
                      required
                      defaultValue={today}
                      className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-xs"
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="block text-[10px] text-[var(--muted)]">
                      Exit premium (per share)
                    </span>
                    <input
                      type="number"
                      name="exit_premium"
                      step="any"
                      min={0}
                      required
                      defaultValue={mark?.mid ?? undefined}
                      className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-xs tabular-nums"
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="block text-[10px] text-[var(--muted)]">Reason</span>
                    <input
                      name="exit_reason"
                      placeholder="trend break"
                      className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-xs"
                    />
                  </label>
                </div>
                <SubmitButton pending={pending}>Close position</SubmitButton>
              </>
            )}
          </ActionForm>

          <ActionForm action={deletePosition}>
            {(pending) => (
              <>
                <input type="hidden" name="id" value={position.id} />
                <SubmitButton pending={pending} variant="quiet">
                  Delete (entered by mistake)
                </SubmitButton>
                <p className="mt-1 text-[10px] leading-relaxed text-[var(--muted)]">
                  Deleting removes the position and its alerts and marks. To record a trade that
                  ended, close it instead — a deleted position leaves no history for the circuit
                  breaker to measure.
                </p>
              </>
            )}
          </ActionForm>
        </div>
      </details>
    </li>
  );
}

export function OpenPositions({
  positions,
  marks,
  today,
}: {
  positions: Position[];
  marks: Record<number, PositionMark>;
  today: string;
}) {
  if (positions.length === 0) {
    return (
      <p className="text-sm leading-relaxed text-[var(--muted)]">
        No open positions. Add one below, or start from a candidate on the{" "}
        <Link href="/" className="underline">
          screener
        </Link>
        .
      </p>
    );
  }

  return (
    <ul className="space-y-3">
      {positions.map((position) => (
        <OpenRow
          key={position.id}
          position={position}
          mark={marks[position.id]}
          today={today}
        />
      ))}
    </ul>
  );
}

export function ClosedPositions({ positions }: { positions: Position[] }) {
  if (positions.length === 0) return null;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] text-sm">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-[10px] uppercase tracking-wide text-[var(--muted)]">
            <th className="py-2 pr-3 font-medium">Symbol</th>
            <th className="py-2 pr-3 font-medium">Contract</th>
            <th className="py-2 pr-3 text-right font-medium">Entry</th>
            <th className="py-2 pr-3 text-right font-medium">Exit</th>
            <th className="py-2 pr-3 text-right font-medium">Realised</th>
            <th className="py-2 pr-3 font-medium">Closed</th>
            <th className="py-2 font-medium">Reason</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const pnl = positionPnl(position, position.exit_premium);
            return (
              <tr key={position.id} className="border-b border-[var(--border)] last:border-0">
                <td className="py-2 pr-3">
                  <Link href={`/t/${position.symbol}`} className="hover:underline">
                    {position.symbol}
                  </Link>
                </td>
                <td className="py-2 pr-3 font-mono text-xs text-[var(--muted)]">
                  {fmtDate(position.expiry)} {position.strike}C ×{position.contracts}
                </td>
                <td className="py-2 pr-3 text-right font-mono tabular-nums">
                  {fmtUsd(position.entry_premium)}
                </td>
                <td className="py-2 pr-3 text-right font-mono tabular-nums">
                  {fmtUsd(position.exit_premium)}
                </td>
                <td
                  className={`py-2 pr-3 text-right font-mono tabular-nums ${
                    pnl && pnl.dollars < 0 ? "text-[var(--fail)]" : "text-[var(--pass)]"
                  }`}
                >
                  {pnl ? `${pnl.dollars < 0 ? "−" : "+"}${fmtUsd(Math.abs(pnl.dollars), 0)}` : EM_DASH}
                </td>
                <td className="py-2 pr-3 text-xs text-[var(--muted)]">
                  {fmtDate(position.closed_on)}
                </td>
                <td className="py-2 text-xs text-[var(--muted)]">
                  {position.exit_reason ?? EM_DASH}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
