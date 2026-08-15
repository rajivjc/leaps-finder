/**
 * Display formatting.
 *
 * One rule runs through this file: **null is not zero**. A missing subscore, a
 * symbol with no valid contract, an unknown earnings date — each renders as an
 * em dash, never as 0, and never silently as a blank that reads like a real
 * value of nothing. The scanner is careful to write null where it does not
 * know; throwing that distinction away at the last step would undo it.
 *
 * Formatting is also strictly a display step: nothing here is allowed to feed
 * back into ranking or comparison (SPEC.md §6 — scores are shown raw, and
 * rounding before sorting would reorder ties that are not actually tied).
 */

export const EM_DASH = "—";

function present(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value);
}

/** A plain number to `digits` decimals, or an em dash when not known. */
export function fmtNumber(value: number | null | undefined, digits = 2): string {
  if (!present(value)) return EM_DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** A fraction (0.0734) as a percentage ("7.3%"). Signed when `signed`. */
export function fmtPercent(
  value: number | null | undefined,
  digits = 1,
  { signed = false }: { signed?: boolean } = {},
): string {
  if (!present(value)) return EM_DASH;
  const text = `${(value * 100).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
  return signed && value > 0 ? `+${text}` : text;
}

/** A 0–100 score. One decimal: enough to see the raw value, not a false precision. */
export function fmtScore(value: number | null | undefined): string {
  return fmtNumber(value, 1);
}

export function fmtUsd(value: number | null | undefined, digits = 2): string {
  if (!present(value)) return EM_DASH;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Market cap and similar: "$1.42T", "$318B". */
export function fmtCompactUsd(value: number | null | undefined): string {
  if (!present(value)) return EM_DASH;
  const units: [number, string][] = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
  ];
  for (const [scale, suffix] of units) {
    if (Math.abs(value) >= scale) {
      return `$${(value / scale).toFixed(value / scale >= 100 ? 0 : 2)}${suffix}`;
    }
  }
  return fmtUsd(value, 0);
}

export function fmtInteger(value: number | null | undefined): string {
  if (!present(value)) return EM_DASH;
  return Math.round(value).toLocaleString("en-US");
}

/**
 * A date-only column, rendered in UTC.
 *
 * Dates from Postgres arrive as "2026-08-14" with no zone. Parsing that as
 * local time in a browser west of UTC lands on the 13th, which would label the
 * scan with the wrong Friday — so the zone is pinned explicitly at both ends.
 */
export function fmtDate(value: string | null | undefined, withWeekday = false): string {
  if (!value) return EM_DASH;
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-US", {
    timeZone: "UTC",
    ...(withWeekday ? { weekday: "short" as const } : {}),
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
