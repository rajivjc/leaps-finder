import { createReadClient } from "@/lib/supabase";
import type { Scan } from "@/lib/types";

// The screener reads precomputed rows written by the scanner; never prerender it
// at build time, where no Supabase project is configured (CI).
export const dynamic = "force-dynamic";

function formatAsOf(date: string): string {
  return new Date(`${date}T00:00:00Z`).toLocaleDateString("en-US", {
    timeZone: "UTC",
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

type LatestScan =
  | { state: "unconfigured" }
  | { state: "error"; message: string }
  | { state: "empty" }
  | { state: "ok"; scan: Scan };

async function loadLatestScan(): Promise<LatestScan> {
  const supabase = createReadClient();
  if (!supabase) return { state: "unconfigured" };

  const { data, error } = await supabase
    .from("scans")
    .select("*")
    .eq("status", "ok")
    .order("as_of_date", { ascending: false })
    .order("id", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) return { state: "error", message: error.message };
  if (!data) return { state: "empty" };
  return { state: "ok", scan: data };
}

function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6">
      <h2 className="text-sm font-semibold">{title}</h2>
      <div className="mt-2 text-sm leading-relaxed text-[var(--muted)]">{children}</div>
    </div>
  );
}

export default async function ScreenerPage() {
  const latest = await loadLatestScan();

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Screener</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          US large caps screened for LEAPS call entries and scored on trend, quality, option
          economics, valuation upside, and entry timing — a composite shown raw, with no curve
          applied. Preset tiers (Strict / Balanced / Wide) apply the hard filters.
        </p>
        {latest.state === "ok" && (
          <p className="mt-3 text-xs text-[var(--muted)]">
            As of {formatAsOf(latest.scan.as_of_date)} · {latest.scan.matches_count ?? 0} of{" "}
            {latest.scan.universe_count ?? 0} names pass the Wide preset · data: Yahoo Finance,
            delayed
          </p>
        )}
      </section>

      {latest.state === "unconfigured" && (
        <Notice title="Supabase is not configured">
          Set <code className="font-mono">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
          <code className="font-mono">NEXT_PUBLIC_SUPABASE_ANON_KEY</code> to read scan results.
          See <code className="font-mono">apps/web/.env.example</code>.
        </Notice>
      )}

      {latest.state === "error" && (
        <Notice title="Could not read scan results">{latest.message}</Notice>
      )}

      {latest.state === "empty" && (
        <Notice title="No completed scans yet">
          The weekly scanner has not written a successful run. A partially-failed scan is recorded
          as <code className="font-mono">failed</code> and deliberately does not appear here.
        </Notice>
      )}

      {latest.state === "ok" && (
        <Notice title="Results table lands in M4">
          Scan {latest.scan.id} is in the database. Preset tabs, filter drawer, and result cards
          are the frontend milestone.
        </Notice>
      )}
    </div>
  );
}
