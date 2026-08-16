/**
 * SPEC.md §7's sleeve meters: open premium vs the 15% cap, positions per sector
 * vs 2, position count vs 5.
 *
 * Everything here is a *warning*. §7 is explicit that the caps are block-level
 * warnings and not hard blocks, so a breached meter turns amber and says what
 * it is — it never refuses anything, and nothing on the page is disabled by it.
 */

import { fmtPercent, fmtUsd } from "@/lib/format";
import {
  MAX_PER_SECTOR,
  MAX_POSITIONS,
  SLEEVE_EXPOSURE_CAP,
  type SleeveMeters as Meters,
} from "@/lib/metrics";

function Meter({
  label,
  value,
  detail,
  fill,
  over,
}: {
  label: string;
  value: string;
  detail: string;
  fill: number | null;
  over: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-[var(--muted)]">{label}</span>
        <span
          className={`font-mono text-sm tabular-nums ${over ? "text-[var(--warn-fg)] font-semibold" : ""}`}
        >
          {value}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--track)]">
        {fill !== null && (
          <div
            className="h-full rounded-full"
            style={{
              // Clamped so a 300% breach does not silently render as a full bar
              // indistinguishable from 100% — the number beside it carries the
              // real magnitude, and the amber says it is over.
              width: `${Math.min(100, Math.max(0, fill * 100))}%`,
              background: over ? "var(--warn-fg)" : "var(--score-high)",
            }}
          />
        )}
      </div>
      <p className="text-[10px] leading-relaxed text-[var(--muted)]">{detail}</p>
    </div>
  );
}

export function SleeveMeters({ meters, equity }: { meters: Meters; equity: number | null }) {
  return (
    <div className="grid gap-5 sm:grid-cols-3">
      <Meter
        label={`Open premium vs ${fmtPercent(SLEEVE_EXPOSURE_CAP, 0)} cap`}
        value={
          meters.exposureFraction === null
            ? fmtUsd(meters.openPremium, 0)
            : fmtPercent(meters.exposureFraction)
        }
        detail={
          equity === null
            ? `${fmtUsd(meters.openPremium, 0)} at cost. Enter account equity to measure it against the cap.`
            : `${fmtUsd(meters.openPremium, 0)} at cost against ${fmtUsd(equity, 0)} equity. Measured at cost, not at current mark — a sleeve that has fallen has not earned room for another trade.`
        }
        fill={
          meters.exposureFraction === null
            ? null
            : meters.exposureFraction / SLEEVE_EXPOSURE_CAP
        }
        over={meters.overExposureCap}
      />

      <Meter
        label={`Open positions vs ${MAX_POSITIONS}`}
        value={`${meters.positionCount} / ${MAX_POSITIONS}`}
        detail="One line per open contract; a scale-in counts as its own position."
        fill={meters.positionCount / MAX_POSITIONS}
        over={meters.overPositionCap}
      />

      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-xs text-[var(--muted)]">Per sector vs {MAX_PER_SECTOR}</span>
        </div>
        {meters.sectors.length === 0 ? (
          <p className="text-[10px] leading-relaxed text-[var(--muted)]">No open positions.</p>
        ) : (
          <ul className="space-y-1">
            {meters.sectors.map((sector) => (
              <li key={sector.sector} className="flex items-baseline justify-between gap-3">
                <span className="truncate text-xs">{sector.sector}</span>
                <span
                  className={`font-mono text-xs tabular-nums ${
                    sector.over ? "font-semibold text-[var(--warn-fg)]" : "text-[var(--muted)]"
                  }`}
                >
                  {sector.count}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-[10px] leading-relaxed text-[var(--muted)]">
          Sector comes from the scanner&rsquo;s universe table; a symbol it has never seen counts as
          Unknown.
        </p>
      </div>
    </div>
  );
}
