/**
 * The §8 banner gate, as a test rather than a comment.
 *
 * Acceptance 6 requires that a `results.json` without a non-empty `banner`
 * fails the web build. `parseResults` throwing is what does that — a page
 * cannot render metrics from a file the loader refused — so these cases are the
 * actual enforcement, not a description of it.
 */

import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { latestRunDate, loadBacktest, parseResults, toLine } from "@/lib/backtest";

const VALID = {
  schema_version: 1,
  banner: "**It evaluates:** the timing signal only.",
  run_date: "2026-08-16",
  window: { start: "2016-08-14", end: "2026-08-14", fetch_start: "2015-02-11" },
  coverage: { coverage_ratio: 0.927, status: "ok" },
  track_a: { stock: {}, overlay: {} },
  sleeves: [],
};

/** A copy of `payload` without `key` — clearer than a destructure-to-discard. */
function without(payload: Record<string, unknown>, key: string): Record<string, unknown> {
  const copy = { ...payload };
  delete copy[key];
  return copy;
}

const roots: string[] = [];

function makeRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "backtest-"));
  roots.push(root);
  return root;
}

afterEach(() => {
  while (roots.length > 0) rmSync(roots.pop() as string, { recursive: true, force: true });
});

describe("parseResults — the §8 banner gate", () => {
  it("accepts a run carrying a banner", () => {
    const results = parseResults(JSON.stringify(VALID), "results.json");
    expect(results.banner).toBe(VALID.banner);
    expect(results.run_date).toBe("2026-08-16");
  });

  it("throws when the banner field is absent", () => {
    expect(() => parseResults(JSON.stringify(without(VALID, "banner")), "r.json")).toThrow(
      /banner/,
    );
  });

  it.each(["", "   ", "\n\t "])("throws when the banner is empty (%j)", (banner) => {
    expect(() => parseResults(JSON.stringify({ ...VALID, banner }), "results.json")).toThrow(
      /banner/,
    );
  });

  it("throws when the banner is not a string", () => {
    expect(() => parseResults(JSON.stringify({ ...VALID, banner: 42 }), "r.json")).toThrow(
      /banner/,
    );
  });

  it("reports the banner first, even when other fields are also missing", () => {
    // The banner rule must not be maskable by an unrelated validation error.
    expect(() => parseResults(JSON.stringify({ schema_version: 1 }), "r.json")).toThrow(/banner/);
  });

  it("rejects an unsupported schema version", () => {
    expect(() => parseResults(JSON.stringify({ ...VALID, schema_version: 99 }), "r.json")).toThrow(
      /schema_version/,
    );
  });

  it("rejects a NaN token, which Python can emit and JSON cannot hold", () => {
    const raw = JSON.stringify(VALID).replace('"coverage_ratio":0.927', '"coverage_ratio":NaN');
    expect(() => parseResults(raw, "r.json")).toThrow(/not valid JSON/);
  });

  it("rejects a missing required field", () => {
    expect(() => parseResults(JSON.stringify(without(VALID, "sleeves")), "r.json")).toThrow(
      /sleeves/,
    );
  });
});

describe("loadBacktest", () => {
  it("returns null when nothing has been published", () => {
    expect(loadBacktest(makeRoot())).toBeNull();
    expect(latestRunDate(join(makeRoot(), "absent"))).toBeNull();
  });

  it("picks the newest run directory and ignores non-date entries", () => {
    const root = makeRoot();
    for (const run of ["2026-01-02", "2026-08-16", "2025-12-31"]) {
      mkdirSync(join(root, run));
      writeFileSync(
        join(root, run, "results.json"),
        JSON.stringify({ ...VALID, run_date: run }),
        "utf8",
      );
    }
    mkdirSync(join(root, "notes"));
    writeFileSync(join(root, "README.md"), "not a run", "utf8");

    const loaded = loadBacktest(root);
    expect(loaded?.runDate).toBe("2026-08-16");
    expect(loaded?.results.run_date).toBe("2026-08-16");
  });

  it("fails the build when the newest committed run has no banner", () => {
    const root = makeRoot();
    mkdirSync(join(root, "2026-08-16"));
    writeFileSync(
      join(root, "2026-08-16", "results.json"),
      JSON.stringify(without(VALID, "banner")),
      "utf8",
    );
    expect(() => loadBacktest(root)).toThrow(/banner/);
  });

  it("fails on a run directory with no results.json rather than reporting none", () => {
    const root = makeRoot();
    mkdirSync(join(root, "2026-08-16"));
    expect(() => loadBacktest(root)).toThrow(/results\.json/);
  });
});

describe("toLine", () => {
  it("drops the samples a curve has no value for", () => {
    expect(toLine(["a", "b", "c"], [1, null, 3])).toEqual([
      { time: "a", value: 1 },
      { time: "c", value: 3 },
    ]);
  });

  it("stops at the shorter of the two arrays", () => {
    expect(toLine(["a", "b"], [1, 2, 3])).toHaveLength(2);
    expect(toLine(["a", "b", "c"], [1])).toHaveLength(1);
  });
});
