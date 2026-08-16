"use client";

/**
 * The alert list and the circuit-breaker banner (SPEC.md §7, §8.4).
 *
 * Each alert is shown with the trading day it was computed for, not just the
 * timestamp it was written at. Those differ — the daily job runs after the
 * close, and a rerun can write yesterday's finding today — and the message
 * quotes prices from the trading day, so pairing it with the wrong date is how
 * a reader ends up checking a number against the wrong session.
 *
 * Acknowledging writes `acknowledged` and nothing else. Migration 0002 revokes
 * table-wide UPDATE on `alerts` and grants it back on that one column, so the
 * scanner's record of *why* it fired is not the owner's to revise.
 */

import { acknowledgeAlert } from "@/app/positions/actions";
import { ActionForm, SubmitButton } from "@/components/ActionForm";
import { EM_DASH, fmtDate } from "@/lib/format";
import { CIRCUIT_BREAKER_DAYS, CIRCUIT_BREAKER_WEEKS } from "@/lib/metrics";
import type { Alert, AlertKind, Position } from "@/lib/types";

const LABELS: Record<AlertKind, string> = {
  trend_break: "Trend break",
  stoch_below_20: "Weekly %K crossed below 20",
  premium_stop: "Premium stop",
  time_exit: "Time exit",
  earnings_soon: "Earnings soon",
  circuit_breaker: "Circuit breaker",
};

/** §7 makes the earnings heads-up informational; the other five are exits. */
const INFORMATIONAL = new Set<AlertKind>(["earnings_soon"]);

export function activeBreaker(alerts: Alert[], now: Date): Alert | null {
  const floor = now.getTime() - CIRCUIT_BREAKER_DAYS * 86_400_000;
  return (
    alerts.find(
      (alert) =>
        alert.kind === "circuit_breaker" &&
        alert.created_at !== null &&
        Date.parse(alert.created_at) > floor,
    ) ?? null
  );
}

export function CircuitBreakerBanner({ alert }: { alert: Alert }) {
  return (
    <div className="rounded-lg border border-[var(--warn-border)] bg-[var(--warn-bg)] p-4 text-[var(--warn-fg)]">
      <h2 className="text-sm font-semibold">
        Circuit breaker tripped — no new entries for {CIRCUIT_BREAKER_WEEKS} weeks
      </h2>
      <p className="mt-1 text-xs leading-relaxed">{alert.message}</p>
      <p className="mt-2 text-[10px] leading-relaxed">
        Tripped {fmtDate(alert.created_at?.slice(0, 10))} on data as of{" "}
        {fmtDate(alert.as_of_date)}. The ban runs from the trip date and is not lifted by the
        sleeve recovering — §7 makes it a fixed {CIRCUIT_BREAKER_WEEKS}-week pause.
      </p>
    </div>
  );
}

export function AlertsList({
  alerts,
  positions,
}: {
  alerts: Alert[];
  positions: Position[];
}) {
  const symbolOf = new Map(positions.map((position) => [position.id, position.symbol]));

  if (alerts.length === 0) {
    return (
      <p className="text-sm leading-relaxed text-[var(--muted)]">
        No alerts. The weekly scan checks §7&rsquo;s stochastic exit; the daily refresh checks the
        trend break, premium stop, time exit, earnings heads-up and the circuit breaker.
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {alerts.map((alert) => {
        const kind = alert.kind ?? "trend_break";
        const informational = INFORMATIONAL.has(kind);
        const symbol = alert.position_id ? symbolOf.get(alert.position_id) : null;

        return (
          <li
            key={alert.id}
            className={`rounded border p-3 ${
              alert.acknowledged
                ? "border-[var(--border)] bg-[var(--surface)] opacity-70"
                : informational
                  ? "border-[var(--border)]"
                  : "border-[var(--warn-border)] bg-[var(--warn-bg)]"
            }`}
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-xs font-semibold">
                {LABELS[kind] ?? kind}
                {symbol && <span className="font-mono font-normal"> · {symbol}</span>}
              </span>
              <span className="text-[10px] text-[var(--muted)]">
                as of {alert.as_of_date ? fmtDate(alert.as_of_date) : EM_DASH}
                {alert.created_at && ` · written ${fmtDate(alert.created_at.slice(0, 10))}`}
              </span>
            </div>

            <p className="mt-1 text-xs leading-relaxed">{alert.message}</p>

            {!alert.acknowledged && (
              <ActionForm action={acknowledgeAlert} className="mt-2" onSuccessMessage={false}>
                {(pending) => (
                  <>
                    <input type="hidden" name="id" value={alert.id} />
                    <SubmitButton pending={pending} variant="quiet">
                      Acknowledge
                    </SubmitButton>
                  </>
                )}
              </ActionForm>
            )}
          </li>
        );
      })}
    </ul>
  );
}
