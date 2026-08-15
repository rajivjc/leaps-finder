import Link from "next/link";

import { Notice } from "@/components/Notice";
import { ScreenerBoard } from "@/components/ScreenerBoard";
import { fmtDate } from "@/lib/format";
import { loadScreener } from "@/lib/queries";

// The screener reads precomputed rows written by the scanner; never prerender it
// at build time, where no Supabase project is configured (CI).
export const dynamic = "force-dynamic";

export default async function ScreenerPage() {
  const loaded = await loadScreener();

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Screener</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          US large caps screened for LEAPS call entries and scored on trend, quality, option
          economics, valuation upside, and entry timing — a composite shown raw, with no curve
          applied. Preset tiers apply the hard filters;{" "}
          <Link href="/about" className="underline">
            every formula is written out on the about page
          </Link>
          .
        </p>
        {loaded.state === "ok" && loaded.data && (
          <p className="mt-3 text-xs text-[var(--muted)]">
            As of {fmtDate(loaded.data.scan.as_of_date, true)} ·{" "}
            {loaded.data.scan.matches_count ?? loaded.data.rows.length} of{" "}
            {loaded.data.scan.universe_count ?? 0} names pass the Wide preset · data: Yahoo Finance,
            delayed
          </p>
        )}
      </section>

      {loaded.state === "unconfigured" && (
        <Notice title="Supabase is not configured">
          Set <code className="font-mono">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
          <code className="font-mono">NEXT_PUBLIC_SUPABASE_ANON_KEY</code> to read scan results. See{" "}
          <code className="font-mono">apps/web/.env.example</code>.
        </Notice>
      )}

      {loaded.state === "error" && (
        <Notice title="Could not read scan results">{loaded.message}</Notice>
      )}

      {loaded.state === "ok" && !loaded.data && (
        <Notice title="No completed scans yet">
          The weekly scanner has not written a successful run. A partially-failed scan is recorded
          as <code className="font-mono">failed</code> and deliberately does not appear here.
        </Notice>
      )}

      {loaded.state === "ok" && loaded.data && (
        <ScreenerBoard
          rows={loaded.data.rows}
          tickers={loaded.data.tickers}
          sparklines={loaded.data.sparklines}
        />
      )}
    </div>
  );
}
