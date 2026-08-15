import { CompareBoard } from "@/components/CompareBoard";
import { Notice } from "@/components/Notice";
import { fmtDate } from "@/lib/format";
import { loadCompare } from "@/lib/queries";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Compare · LEAPS Finder",
};

export default async function ComparePage() {
  const loaded = await loadCompare();

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Compare</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          Up to four names side by side, on raw scores. The factor bars share one fixed 0–100 axis
          rather than being scaled to the best of the group — four weak candidates should look like
          four weak candidates.
        </p>
        {loaded.state === "ok" && loaded.data && (
          <p className="mt-3 text-xs text-[var(--muted)]">
            As of {fmtDate(loaded.data.scan.as_of_date, true)} · {loaded.data.rows.length} names
            evaluated · data: Yahoo Finance, delayed
          </p>
        )}
      </section>

      {loaded.state === "unconfigured" && (
        <Notice title="Supabase is not configured">No scan data is readable.</Notice>
      )}
      {loaded.state === "error" && (
        <Notice title="Could not read scan results">{loaded.message}</Notice>
      )}
      {loaded.state === "ok" && !loaded.data && (
        <Notice title="No completed scans yet">
          There is nothing to compare until the weekly scanner records a successful run.
        </Notice>
      )}
      {loaded.state === "ok" && loaded.data && (
        <CompareBoard rows={loaded.data.rows} tickers={loaded.data.tickers} />
      )}
    </div>
  );
}
