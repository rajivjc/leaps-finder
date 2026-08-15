/**
 * The spec formulas that live in TypeScript rather than in the scanner
 * (SPEC.md §5.5 cushion, §7 position sizing) plus the null semantics the whole
 * UI leans on.
 *
 * These are here for the same reason the scanner's tests are: the numbers are
 * pinned in the spec, so they get checked against it rather than against
 * whatever the code currently does.
 */

import { describe, expect, it } from "vitest";

import { EM_DASH, fmtPercent, fmtScore, fmtUsd } from "@/lib/format";
import { cushion, positionSize, targetAdjusted } from "@/lib/metrics";

function row(spot: number | null, upside: number | null, breakeven: number | null) {
  return { spot, upside_adj: upside, breakeven };
}

describe("cushion (SPEC §5.5)", () => {
  it("divides by the breakeven, not the spot", () => {
    // spot 100, upside_adj 20% -> target 120; breakeven 110.
    // (120 - 110) / 110 = 0.0909..., where dividing by spot would give 0.10.
    expect(cushion(row(100, 0.2, 110))).toBeCloseTo(10 / 110, 12);
    expect(cushion(row(100, 0.2, 110))).not.toBeCloseTo(0.1, 6);
  });

  it("returns a negative cushion rather than flooring it at zero", () => {
    // The haircut target sits below the breakeven — the case worth surfacing.
    expect(cushion(row(100, 0.05, 118))).toBeCloseTo((105 - 118) / 118, 12);
    expect(cushion(row(100, 0.05, 118))).toBeLessThan(0);
  });

  it("is zero when the target lands exactly on the breakeven", () => {
    expect(cushion(row(100, 0.1, 110))).toBeCloseTo(0, 12);
  });

  it("is null when any input is missing, never zero", () => {
    expect(cushion(row(null, 0.2, 110))).toBeNull();
    expect(cushion(row(100, null, 110))).toBeNull();
    expect(cushion(row(100, 0.2, null))).toBeNull();
  });

  it("is null rather than infinite when the breakeven is not positive", () => {
    expect(cushion(row(100, 0.2, 0))).toBeNull();
  });

  it("negative adjusted upside still produces a defined cushion", () => {
    // A target below spot is a real reading, not a missing one.
    expect(cushion(row(100, -0.1, 110))).toBeCloseTo((90 - 110) / 110, 12);
  });
});

describe("targetAdjusted", () => {
  it("applies the adjusted upside to spot", () => {
    expect(targetAdjusted({ spot: 200, upside_adj: 0.15 })).toBeCloseTo(230, 12);
  });

  it("is null when either input is missing", () => {
    expect(targetAdjusted({ spot: null, upside_adj: 0.15 })).toBeNull();
    expect(targetAdjusted({ spot: 200, upside_adj: null })).toBeNull();
  });
});

describe("positionSize (SPEC §7)", () => {
  it("budgets 3% of equity and floors to whole contracts", () => {
    // 3% of 100k = 3,000; a 12.50 mid costs 1,250 per contract -> 2 contracts.
    const size = positionSize(100_000, 12.5);

    expect(size).not.toBeNull();
    expect(size!.maxPremiumDollars).toBeCloseTo(3_000, 12);
    expect(size!.maxContracts).toBe(2);
    expect(size!.deployedDollars).toBeCloseTo(2_500, 12);
    expect(size!.deployedFraction).toBeCloseTo(0.025, 12);
  });

  it("floors rather than rounds — 2.9 contracts is 2, not 3", () => {
    // 3% of 100k = 3,000; mid 10.34 -> 1,034 per contract -> 2.90 contracts.
    expect(positionSize(100_000, 10.34)!.maxContracts).toBe(2);
  });

  it("allows an exact fit", () => {
    // 3% of 100k = 3,000; mid 10.00 -> 1,000 per contract -> exactly 3.
    const size = positionSize(100_000, 10)!;
    expect(size.maxContracts).toBe(3);
    expect(size.deployedFraction).toBeCloseTo(0.03, 12);
  });

  it("reports zero contracts when one contract breaches the budget", () => {
    // 3% of 10k = 300; a 5.00 mid costs 500 — the trade does not fit at all.
    const size = positionSize(10_000, 5)!;
    expect(size.maxContracts).toBe(0);
    expect(size.deployedDollars).toBe(0);
  });

  it("is null when there is nothing to size against", () => {
    expect(positionSize(null, 12.5)).toBeNull();
    expect(positionSize(100_000, null)).toBeNull();
    expect(positionSize(0, 12.5)).toBeNull();
    expect(positionSize(-100, 12.5)).toBeNull();
    expect(positionSize(100_000, 0)).toBeNull();
  });
});

describe("formatting null semantics", () => {
  it("renders missing values as an em dash, never as zero", () => {
    expect(fmtScore(null)).toBe(EM_DASH);
    expect(fmtUsd(null)).toBe(EM_DASH);
    expect(fmtPercent(null)).toBe(EM_DASH);
  });

  it("keeps a real zero distinguishable from a missing value", () => {
    expect(fmtScore(0)).toBe("0.0");
    expect(fmtPercent(0)).toBe("0.0%");
  });

  it("treats NaN as missing rather than printing it", () => {
    expect(fmtScore(Number.NaN)).toBe(EM_DASH);
  });

  it("signs percentages only when asked, and only when positive", () => {
    expect(fmtPercent(0.0734, 1, { signed: true })).toBe("+7.3%");
    expect(fmtPercent(-0.0734, 1, { signed: true })).toBe("-7.3%");
    expect(fmtPercent(0.0734)).toBe("7.3%");
  });
});
