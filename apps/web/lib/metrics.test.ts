/**
 * The spec formulas that live in TypeScript rather than in the scanner
 * (SPEC.md §5.5 cushion, §7 position sizing) plus the null semantics the whole
 * UI leans on.
 *
 * These are here for the same reason the scanner's tests are: the numbers are
 * pinned in the spec, so they get checked against it rather than against
 * whatever the code currently does.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { EM_DASH, fmtPercent, fmtScore, fmtUsd } from "@/lib/format";
import {
  CIRCUIT_BREAKER_LOSS_FRACTION,
  CIRCUIT_BREAKER_WEEKS,
  IV_PASS_MAX,
  MAX_PER_SECTOR,
  MAX_POSITIONS,
  PREMIUM_STOP_FRACTION,
  QUALITY_PASS_MIN,
  SHARES_PER_CONTRACT,
  SLEEVE_EXPOSURE_CAP,
  TIME_EXIT_DTE,
  capBreaches,
  costBasis,
  cushion,
  daysBetween,
  positionPnl,
  positionSize,
  premiumStopLevel,
  sleeveMeters,
  targetAdjusted,
} from "@/lib/metrics";

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

describe("checklist thresholds stay in step with the scanner", () => {
  // The ticks and crosses on the ticker page come from `quality_pass` /
  // `iv_pass`, which the scanner computes from these constants; the sentence
  // beside each one is written here. If the two drift, the page explains a red
  // cross with a comparison that reads as passing — so the drift fails a test
  // instead of shipping.
  const scoringPy = readFileSync(
    fileURLToPath(new URL("../../../scanner/leaps_scanner/scoring.py", import.meta.url)),
    "utf8",
  );

  function constantIn(source: string, name: string): number {
    const match = source.match(new RegExp(`^${name}\\s*=\\s*([0-9.]+)`, "m"));
    if (!match) throw new Error(`${name} not found in scoring.py`);
    return Number(match[1]);
  }

  it("matches scoring.QUALITY_PASS_MIN", () => {
    expect(QUALITY_PASS_MIN).toBe(constantIn(scoringPy, "QUALITY_PASS_MIN"));
  });

  it("matches scoring.IV_PASS_MAX", () => {
    expect(IV_PASS_MAX).toBe(constantIn(scoringPy, "IV_PASS_MAX"));
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

describe("mark-to-market P&L (SPEC §7)", () => {
  const held = { contracts: 2, entry_premium: 10 };

  it("multiplies by 100 shares a contract", () => {
    expect(costBasis(held)).toBe(2000);
    expect(positionPnl(held, 12)?.dollars).toBe(400);
    expect(positionPnl(held, 6)?.dollars).toBe(-800);
  });

  it("reports the fraction against cost, not against the mark", () => {
    expect(positionPnl(held, 5)?.fraction).toBeCloseTo(-0.5, 10);
  });

  it("returns null without a mark, so the UI can show a dash instead of a zero", () => {
    // "We do not know what this is worth" must not render as "it is worth what
    // you paid" — the two are very different things to tell a holder.
    expect(positionPnl(held, null)).toBeNull();
    expect(positionPnl(held, undefined)).toBeNull();
    expect(positionPnl(held, Number.NaN)).toBeNull();
  });

  it("treats a worthless contract as a real mark, not a missing one", () => {
    expect(positionPnl(held, 0)?.dollars).toBe(-2000);
  });

  it("puts the premium stop at half the entry", () => {
    expect(premiumStopLevel(10)).toBe(5);
  });
});

describe("sleeve meters (SPEC §7)", () => {
  const sectors: Record<string, string> = { AAA: "Tech", BBB: "Tech", CCC: "Health Care" };
  const sectorOf = (symbol: string) => sectors[symbol] ?? null;

  const open = [
    { symbol: "AAA", contracts: 1, entry_premium: 30 },
    { symbol: "BBB", contracts: 2, entry_premium: 20 },
  ];

  it("measures exposure at cost against equity", () => {
    const meters = sleeveMeters(open, sectorOf, 100_000);

    expect(meters.openPremium).toBe(7000);
    expect(meters.exposureFraction).toBeCloseTo(0.07, 10);
    expect(meters.overExposureCap).toBe(false);
  });

  it("flags a breach of the 15% cap", () => {
    expect(sleeveMeters(open, sectorOf, 40_000).overExposureCap).toBe(true);
  });

  it("leaves exposure unmeasured rather than assuming an equity", () => {
    const meters = sleeveMeters(open, sectorOf, null);

    expect(meters.exposureFraction).toBeNull();
    expect(meters.overExposureCap).toBe(false);
  });

  it("counts positions per sector and flags the third in one", () => {
    const crowded = [...open, { symbol: "AAA", contracts: 1, entry_premium: 10 }];
    const tech = sleeveMeters(crowded, sectorOf, 100_000).sectors.find((s) => s.sector === "Tech");

    expect(tech).toEqual({ sector: "Tech", count: 3, over: true });
  });

  it("files a symbol the scanner has never seen under Unknown", () => {
    const meters = sleeveMeters([{ symbol: "ZZZ", contracts: 1, entry_premium: 1 }], sectorOf, null);

    expect(meters.sectors).toEqual([{ sector: "Unknown", count: 1, over: false }]);
  });
});

describe("cap breaches for a prospective entry (SPEC §7)", () => {
  const sectorOf = (symbol: string) => (symbol === "CCC" ? "Health Care" : "Tech");
  const open = [
    { symbol: "AAA", contracts: 1, entry_premium: 30 },
    { symbol: "BBB", contracts: 1, entry_premium: 30 },
  ];

  it("finds nothing wrong with a well-sized entry", () => {
    const breaches = capBreaches(
      { symbol: "CCC", sector: "Health Care", contracts: 1, entry_premium: 25 },
      open,
      sectorOf,
      200_000,
    );

    expect(breaches).toEqual([]);
  });

  it("catches an oversized single position", () => {
    const breaches = capBreaches(
      { symbol: "CCC", sector: "Health Care", contracts: 5, entry_premium: 40 },
      [],
      sectorOf,
      100_000,
    );

    expect(breaches.some((text) => text.includes("per-position budget"))).toBe(true);
  });

  it("catches a third position in the same sector", () => {
    const breaches = capBreaches(
      { symbol: "DDD", sector: "Tech", contracts: 1, entry_premium: 5 },
      open,
      sectorOf,
      1_000_000,
    );

    expect(breaches.some((text) => text.includes("Tech"))).toBe(true);
  });

  it("catches a sixth open position", () => {
    const five = Array.from({ length: 5 }, (_, index) => ({
      symbol: `S${index}`,
      contracts: 1,
      entry_premium: 1,
    }));

    const breaches = capBreaches(
      { symbol: "CCC", sector: "Health Care", contracts: 1, entry_premium: 1 },
      five,
      () => "Health Care",
      1_000_000,
    );

    expect(breaches.some((text) => text.includes(`over the ${MAX_POSITIONS} cap`))).toBe(true);
  });

  it("checks only the count caps when no equity is known", () => {
    // Without a denominator the dollar caps are unmeasurable; reporting them as
    // passed would be as wrong as reporting them as breached.
    const breaches = capBreaches(
      { symbol: "CCC", sector: "Health Care", contracts: 100, entry_premium: 100 },
      [],
      sectorOf,
      null,
    );

    expect(breaches).toEqual([]);
  });
});

describe("daysBetween", () => {
  it("counts calendar days in UTC", () => {
    expect(daysBetween("2026-08-14", "2027-06-18")).toBe(308);
    expect(daysBetween("2026-08-14", "2026-08-14")).toBe(0);
    expect(daysBetween("2026-08-14", "2026-08-13")).toBe(-1);
  });

  it("does not drift across a DST boundary", () => {
    // US DST ends 2026-11-01. Local-time arithmetic would make this 24 hours
    // long and round to the wrong day.
    expect(daysBetween("2026-10-31", "2026-11-02")).toBe(2);
  });
});

describe("risk thresholds stay in step with the scanner", () => {
  // `risk.py` is what actually fires alerts. These copies only let the page say
  // how *close* a position is to a stop — but a page drawing a stop line the
  // scanner does not use is a page that lies quietly, so drift fails a test.
  const riskPy = readFileSync(
    fileURLToPath(new URL("../../../scanner/leaps_scanner/risk.py", import.meta.url)),
    "utf8",
  );

  function constantIn(name: string): number {
    const match = riskPy.match(new RegExp(`^${name}\\s*=\\s*([0-9.]+)`, "m"));
    if (!match) throw new Error(`${name} not found in risk.py`);
    return Number(match[1]);
  }

  it.each([
    ["PREMIUM_STOP_FRACTION", PREMIUM_STOP_FRACTION],
    ["TIME_EXIT_DTE", TIME_EXIT_DTE],
    ["CIRCUIT_BREAKER_LOSS_FRACTION", CIRCUIT_BREAKER_LOSS_FRACTION],
    ["CIRCUIT_BREAKER_WEEKS", CIRCUIT_BREAKER_WEEKS],
    ["SHARES_PER_CONTRACT", SHARES_PER_CONTRACT],
  ])("matches risk.%s", (name, value) => {
    expect(value).toBe(constantIn(name));
  });
});

describe("sleeve caps match the spec table (SPEC §7)", () => {
  it("pins the three sleeve limits", () => {
    expect(SLEEVE_EXPOSURE_CAP).toBe(0.15);
    expect(MAX_POSITIONS).toBe(5);
    expect(MAX_PER_SECTOR).toBe(2);
  });
});
