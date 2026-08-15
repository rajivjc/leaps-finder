/**
 * The five-filter checklist (SPEC.md §8.2), each row with the numbers behind it.
 *
 * A bare green tick is not much use — the question a reader actually has is
 * "how close was it?". Every row therefore states the values it was decided on,
 * so a name that missed by a rounding error is visibly different from one that
 * missed by a mile.
 *
 * The pass booleans come from the scan row rather than being re-derived here.
 * Recomputing them in the browser would be a second implementation of §4/§6
 * thresholds, free to drift from the one that actually set `passes_*`.
 */

import { EM_DASH, fmtPercent, fmtScore, fmtUsd } from "@/lib/format";
import type { ScanResult } from "@/lib/types";

/** M3 addendum thresholds, recorded in the scanner's `scoring.py` docstring. */
const QUALITY_PASS_MIN = 45;
const IV_PASS_MAX = 50;

type Check = { label: string; pass: boolean | null; reason: string };

function trendCheck(row: ScanResult): Check {
  const { spot, sma50, sma200 } = row;
  const reason =
    spot === null || sma50 === null || sma200 === null
      ? "Not enough daily history to define both averages."
      : `Close ${fmtUsd(spot)} vs SMA50 ${fmtUsd(sma50)} and SMA200 ${fmtUsd(sma200)}; ` +
        `the 50-day is ${sma50 > sma200 ? "above" : "below"} the 200-day. ` +
        "Needs all three: close above both averages, and the 50 above the 200.";
  return { label: "Trend", pass: row.trend_pass, reason };
}

function stochCheck(row: ScanResult): Check {
  const inZone = row.in_zone;
  const turning = row.turning_up;
  const pass = inZone === null || turning === null ? null : inZone && turning;

  const reason =
    row.stoch_k === null
      ? "No weekly stochastic could be computed."
      : `%K ${fmtScore(row.stoch_k)} (prior week ${fmtScore(row.stoch_k_prev)}), %D ${fmtScore(row.stoch_d)}. ` +
        `${inZone ? "Inside" : "Outside"} the 20–70 zone; ` +
        `${turning ? "turning up" : "not turning up"} — turning up needs %K above %D and above last week's %K.`;

  return { label: "Weekly stochastic", pass, reason };
}

function qualityCheck(row: ScanResult): Check {
  const reason =
    row.s_quality === null
      ? "No quality metric was available, so the subscore is undefined — which fails the gate rather than passing it by default."
      : `Quality subscore ${fmtScore(row.s_quality)} against the ${QUALITY_PASS_MIN} floor, ` +
        "averaged over operating margin, ROE, net debt / EBITDA, revenue growth and FCF margin.";
  return { label: "Quality", pass: row.quality_pass, reason };
}

function valuationCheck(row: ScanResult): Check {
  const reason =
    row.upside_adj === null
      ? "No analyst target was available, so there is no adjusted upside to test."
      : `Haircut upside ${fmtPercent(row.upside_adj, 1, { signed: true })} — the analyst mean target ` +
        `${fmtUsd(row.analyst_target)} cut by 40% (§6), against a spot of ${fmtUsd(row.spot)}. ` +
        "Passes when it is above zero.";
  return { label: "Valuation / upside", pass: row.valuation_pass, reason };
}

function ivCheck(row: ScanResult): Check {
  const warming = row.iv_rank_status === "warming_up";
  const reason =
    row.iv_rank === null
      ? "No IV30 for this symbol in this scan, so no rank or substitute could be formed."
      : `${warming ? "IV30 / RV20 percentile" : "IV rank"} ${fmtScore(row.iv_rank)} against the ` +
        `${IV_PASS_MAX} ceiling` +
        (warming
          ? " — a substitute, not a true rank: this symbol has fewer than 120 IV snapshots of its own (§6)."
          : ", measured against this symbol's own 252-snapshot range.") +
        ` IV30 is ${row.iv30 === null ? EM_DASH : fmtPercent(row.iv30)}.`;
  return { label: "Low IV", pass: row.iv_pass, reason };
}

function Mark({ pass }: { pass: boolean | null }) {
  if (pass === null) {
    return (
      <span className="text-[var(--muted)]" aria-label="unknown" title="Not determinable from this scan.">
        ?
      </span>
    );
  }
  return (
    <span
      style={{ color: pass ? "var(--pass)" : "var(--fail)" }}
      aria-label={pass ? "pass" : "fail"}
    >
      {pass ? "✓" : "✗"}
    </span>
  );
}

export function FilterChecklist({ row }: { row: ScanResult }) {
  const checks = [
    trendCheck(row),
    stochCheck(row),
    qualityCheck(row),
    valuationCheck(row),
    ivCheck(row),
  ];

  return (
    <ul className="divide-y divide-[var(--border)]">
      {checks.map((check) => (
        <li key={check.label} className="flex gap-3 py-3 first:pt-0 last:pb-0">
          <span className="mt-0.5 w-4 shrink-0 text-center font-mono text-sm">
            <Mark pass={check.pass} />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-medium">{check.label}</p>
            <p className="mt-0.5 text-xs leading-relaxed text-[var(--muted)]">{check.reason}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}
