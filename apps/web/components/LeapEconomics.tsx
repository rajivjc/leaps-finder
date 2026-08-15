/**
 * The selected contract's economics (SPEC.md §5, shown on §8.2's ticker page).
 *
 * The cushion is the number this card exists for. §5.5 defines it as
 *
 *     cushion = (target_adj − breakeven) / breakeven,  target_adj = spot · (1 + upside_adj)
 *
 * — divided by the breakeven, not the spot, and shown honestly. A negative
 * cushion means the already-haircut analyst target does not even reach the
 * price at which this call starts making money, which is the single most
 * decision-relevant thing the page can say. It is displayed exactly as
 * computed, with that sentence attached, rather than floored at zero or
 * quietly omitted.
 */

import { NoContractBadge, ShortDatedBadge } from "@/components/Badge";
import { EM_DASH, fmtInteger, fmtPercent, fmtUsd } from "@/lib/format";
import { cushion, targetAdjusted } from "@/lib/metrics";
import type { ScanResult } from "@/lib/types";

function Row({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <dt className="text-xs text-[var(--muted)]" title={hint}>
        {label}
      </dt>
      <dd className="font-mono text-sm tabular-nums">{value}</dd>
    </div>
  );
}

export function LeapEconomics({ row }: { row: ScanResult }) {
  if (row.opt_strike === null) {
    return (
      <div className="space-y-3">
        <NoContractBadge />
        <p className="text-sm leading-relaxed text-[var(--muted)]">
          No call in the target expiry had a two-sided quote, so this row carries no option
          economics. The option subscore is undefined as a result, which also leaves the composite
          undefined — the missing factor is not treated as a zero.
        </p>
      </div>
    );
  }

  const cushionValue = cushion(row);
  const target = targetAdjusted(row);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm">
          {fmtUsd(row.opt_strike, 0)} call · {row.opt_expiry ?? EM_DASH}
        </span>
        <ShortDatedBadge dte={row.opt_dte} />
      </div>

      <dl className="divide-y divide-[var(--border)]">
        <Row
          label="Delta"
          value={row.opt_delta === null ? EM_DASH : row.opt_delta.toFixed(3)}
          hint="Black-Scholes delta from the contract's implied volatility, the 13-week T-bill rate and the trailing dividend yield (§5.2). The strike chosen is the one nearest 0.70."
        />
        <Row label="Days to expiry" value={fmtInteger(row.opt_dte)} />
        <Row
          label="Bid / mid / ask"
          value={`${fmtUsd(row.opt_bid)} / ${fmtUsd(row.opt_mid)} / ${fmtUsd(row.opt_ask)}`}
        />
        <Row
          label="Spread"
          value={fmtPercent(row.opt_spread_pct)}
          hint="(ask − bid) / mid — the round-trip cost baked into the quote."
        />
        <Row label="Open interest" value={fmtInteger(row.opt_oi)} />
        <Row label="Contract IV" value={fmtPercent(row.opt_iv)} />
        <Row
          label="Breakeven"
          value={`${fmtUsd(row.breakeven)} (${fmtPercent(row.breakeven_pct, 1, { signed: true })})`}
          hint="Strike + mid. The percentage is against spot: how far the stock must travel before expiry just to return the premium."
        />
        <Row
          label="Cost, % of spot"
          value={fmtPercent(row.cost_pct_spot)}
          hint="mid / spot — what this call costs relative to owning the shares outright."
        />
        <Row
          label="Haircut target"
          value={fmtUsd(target)}
          hint="Spot × (1 + upside_adj), where upside_adj is 60% of the analyst mean upside (§6's 40% haircut)."
        />
      </dl>

      <div className="rounded border border-[var(--border)] bg-[var(--surface)] p-3">
        <div className="flex items-baseline justify-between gap-4">
          <span className="text-xs font-medium">Cushion vs haircut target</span>
          <span className="font-mono text-lg tabular-nums">
            {fmtPercent(cushionValue, 1, { signed: true })}
          </span>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-[var(--muted)]">
          {cushionValue === null
            ? "Not computable: it needs the spot, the adjusted upside and the breakeven, and at least one of those is missing."
            : cushionValue < 0
              ? "Negative — the analyst target, already cut by 40%, sits below this contract's breakeven. " +
                "The trade needs the stock to beat that haircut target just to return the premium."
              : "How far the haircut analyst target clears the breakeven, measured against the breakeven itself (§5.5)."}
        </p>
      </div>
    </div>
  );
}
