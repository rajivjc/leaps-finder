import Link from "next/link";

import { signOut } from "@/app/positions/actions";
import { AlertsList, CircuitBreakerBanner } from "@/components/AlertsList";
import { EquityForm } from "@/components/EquityForm";
import { NewPositionForm } from "@/components/NewPositionForm";
import { Notice } from "@/components/Notice";
import { ClosedPositions, OpenPositions } from "@/components/PositionList";
import { SleeveMeters } from "@/components/SleeveMeters";
import { fmtDate } from "@/lib/format";
import { activeCircuitBreaker, sleeveMeters } from "@/lib/metrics";
import { loadPositions } from "@/lib/queries";

export const dynamic = "force-dynamic";

function Panel({
  title,
  note,
  children,
}: {
  title: string;
  note?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-[var(--border)] p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      {note && <p className="mt-1 text-xs leading-relaxed text-[var(--muted)]">{note}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

export default async function PositionsPage() {
  const loaded = await loadPositions();

  if (loaded.state === "unconfigured") {
    return (
      <Notice title="Supabase is not configured">
        Set <code className="font-mono">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
        <code className="font-mono">NEXT_PUBLIC_SUPABASE_ANON_KEY</code> to use positions.
      </Notice>
    );
  }

  if (loaded.state === "error") {
    return <Notice title="Could not load positions">{loaded.message}</Notice>;
  }

  // The middleware redirects signed-out visitors, so this is a belt-and-braces
  // path rather than the normal one.
  if (!loaded.data) {
    return (
      <Notice title="Not signed in">
        <Link href="/login" className="underline">
          Sign in
        </Link>{" "}
        to see positions.
      </Notice>
    );
  }

  const { positions, marks, refresh, alerts, tickers, sectors, equity, user } = loaded.data;

  const open = positions.filter((position) => position.status !== "closed");
  const closed = positions.filter((position) => position.status === "closed");

  // Prefer the universe-wide map, falling back to the position's own ticker row
  // for a held name the last scan no longer covers.
  const sectorOf = (symbol: string) => sectors[symbol] ?? tickers[symbol]?.sector ?? null;
  const meters = sleeveMeters(open, sectorOf, equity);

  // Resolved on the server so form defaults and DTE do not depend on the
  // viewer's clock or timezone — the same reason `fmtDate` pins UTC.
  const today = new Date().toISOString().slice(0, 10);
  const breaker = activeCircuitBreaker(alerts, new Date());
  const unacknowledged = alerts.filter((alert) => !alert.acknowledged).length;

  return (
    <div className="space-y-8">
      <section>
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">Positions</h1>
          <form action={signOut}>
            <button
              type="submit"
              className="text-xs text-[var(--muted)] underline hover:text-[var(--foreground)]"
            >
              Sign out {user.email ?? ""}
            </button>
          </form>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          Open LEAPS, sized and monitored against{" "}
          <Link href="/about" className="underline">
            §7&rsquo;s risk rules
          </Link>
          . The scanner evaluates the exits — this page shows what it found.
        </p>
        <p className="mt-3 text-xs text-[var(--muted)]">
          {refresh ? (
            <>
              Marks as of {fmtDate(refresh.as_of_date, true)} · every value below comes from that
              one refresh run
            </>
          ) : (
            <>No daily refresh has completed yet, so no position has a current mark.</>
          )}
          {unacknowledged > 0 && ` · ${unacknowledged} unacknowledged alert(s)`}
        </p>
      </section>

      {breaker && <CircuitBreakerBanner alert={breaker} />}

      <Panel
        title="Account equity"
        note="SPEC.md §7: 3% of equity as premium per position, and the sleeve caps measured against the same figure."
      >
        <EquityForm equity={equity} />
      </Panel>

      <Panel
        title="Sleeve exposure"
        note="Warnings, not blocks — §7 lets you override any of these, and logs it when you do."
      >
        <SleeveMeters meters={meters} equity={equity} />
      </Panel>

      <Panel title="Open positions">
        <OpenPositions positions={open} marks={marks} today={today} />
      </Panel>

      <Panel
        title="Alerts"
        note="Written by the scanner: the weekly scan runs the stochastic exit, the daily refresh runs the rest."
      >
        <AlertsList alerts={alerts} positions={positions} />
      </Panel>

      <Panel
        title="Add a position"
        note="Premiums are per share, as the scanner records them; a contract is 100 shares."
      >
        <NewPositionForm
          open={open}
          sectors={sectors}
          equity={equity}
          today={today}
        />
      </Panel>

      {closed.length > 0 && (
        <Panel
          title="Closed positions"
          note="Realised P&L over the trailing four weeks feeds §7's circuit breaker."
        >
          <ClosedPositions positions={closed} />
        </Panel>
      )}
    </div>
  );
}
