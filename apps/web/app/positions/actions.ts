"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { capBreaches } from "@/lib/metrics";
import { createSessionClient } from "@/lib/supabase";
import type { Position } from "@/lib/types";

/**
 * Writes for /positions (SPEC.md §8.4).
 *
 * Every action re-reads the session server-side and never takes a `user_id`
 * from the form. RLS would reject a forged one anyway — the policies compare
 * against `auth.uid()` — but a client-supplied owner is the sort of thing that
 * works until the day a policy is loosened, so it simply never enters.
 */

export type ActionResult = { ok: true; message?: string } | { ok: false; message: string };

const OK: ActionResult = { ok: true };

function fail(message: string): ActionResult {
  return { ok: false, message };
}

/** A required positive number from a form field. */
function positiveNumber(form: FormData, field: string, label: string): number | string {
  const raw = String(form.get(field) ?? "").trim();
  if (raw === "") return `${label} is required.`;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return `${label} must be a positive number.`;
  return value;
}

/** A required ISO date (the browser's date input already produces this shape). */
function isoDate(form: FormData, field: string): string | null {
  const raw = String(form.get(field) ?? "").trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : null;
}

async function session() {
  const client = await createSessionClient();
  if (!client) return { client: null, user: null };
  const {
    data: { user },
  } = await client.auth.getUser();
  return { client, user };
}

/** SPEC.md §7: account equity, persisted per user (migration 0004). */
export async function saveEquity(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const { client, user } = await session();
  if (!client || !user) return fail("Not signed in.");

  const equity = positiveNumber(form, "account_equity", "Account equity");
  if (typeof equity === "string") return fail(equity);

  const { error } = await client
    .from("user_settings")
    .upsert(
      { user_id: user.id, account_equity: equity, updated_at: new Date().toISOString() },
      { onConflict: "user_id" },
    );

  if (error) return fail(error.message);

  revalidatePath("/positions");
  return { ok: true, message: "Equity saved." };
}

export async function createPosition(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const { client, user } = await session();
  if (!client || !user) return fail("Not signed in.");

  const symbol = String(form.get("symbol") ?? "").trim().toUpperCase();
  if (!symbol) return fail("Symbol is required.");

  const openedOn = isoDate(form, "opened_on");
  const expiry = isoDate(form, "expiry");
  if (!openedOn) return fail("Opened on must be a date.");
  if (!expiry) return fail("Expiry must be a date.");
  if (expiry <= openedOn) return fail("Expiry must be after the open date.");

  const strike = positiveNumber(form, "strike", "Strike");
  if (typeof strike === "string") return fail(strike);
  const entryPremium = positiveNumber(form, "entry_premium", "Entry premium");
  if (typeof entryPremium === "string") return fail(entryPremium);

  const contractsRaw = positiveNumber(form, "contracts", "Contracts");
  if (typeof contractsRaw === "string") return fail(contractsRaw);
  if (!Number.isInteger(contractsRaw)) return fail("Contracts must be a whole number.");

  const candidate = {
    symbol,
    sector: null as string | null,
    contracts: contractsRaw,
    entry_premium: entryPremium,
  };

  // Everything the cap check needs, read fresh: the form's view of the sleeve
  // could be minutes old, and the override log has to describe what was
  // actually true when the position was written.
  const [{ data: settings }, { data: openRows }] = await Promise.all([
    client.from("user_settings").select("account_equity").eq("user_id", user.id).maybeSingle(),
    client.from("positions").select("*").eq("status", "open"),
  ]);

  const open = (openRows ?? []) as Position[];
  const symbols = [...new Set([...open.map((row) => row.symbol), symbol])];
  const { data: tickerRows } = await client
    .from("tickers")
    .select("symbol,sector")
    .in("symbol", symbols);

  const sectors = new Map((tickerRows ?? []).map((row) => [row.symbol, row.sector]));
  const sectorOf = (value: string) => sectors.get(value) ?? null;
  candidate.sector = sectorOf(symbol);

  const equity = settings?.account_equity ?? null;
  const breaches = capBreaches(candidate, open, sectorOf, equity);
  const note = String(form.get("override_note") ?? "").trim();

  // §7: "Block-level warnings, not hard blocks (user may override; log it)."
  // The entry goes in either way; what the breach changes is that it leaves a
  // record. The reasons are recomputed here rather than accepted from the form.
  const sizingOverride =
    breaches.length === 0 && !note
      ? null
      : [...breaches, note ? `Owner's note: ${note}` : ""].filter(Boolean).join(" ");

  const { error } = await client.from("positions").insert({
    user_id: user.id,
    symbol,
    opened_on: openedOn,
    expiry,
    strike,
    contracts: contractsRaw,
    entry_premium: entryPremium,
    // The snapshot §7's circuit breaker measures against: the equity this trade
    // was sized with, frozen at entry so it cannot be revised afterwards.
    account_equity_at_entry: equity,
    status: "open",
    sizing_override: sizingOverride,
  });

  if (error) return fail(error.message);

  revalidatePath("/positions");
  return {
    ok: true,
    message: breaches.length
      ? `${symbol} added, with ${breaches.length} sizing warning(s) logged against it.`
      : `${symbol} added.`,
  };
}

export async function closePosition(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const { client, user } = await session();
  if (!client || !user) return fail("Not signed in.");

  const id = Number(form.get("id"));
  if (!Number.isInteger(id)) return fail("Unknown position.");

  const closedOn = isoDate(form, "closed_on");
  if (!closedOn) return fail("Closed on must be a date.");

  // Zero is a real exit premium — a LEAP can expire worthless — so this one is
  // validated as non-negative rather than positive.
  const raw = String(form.get("exit_premium") ?? "").trim();
  const exitPremium = raw === "" ? null : Number(raw);
  if (exitPremium === null || !Number.isFinite(exitPremium) || exitPremium < 0) {
    return fail("Exit premium must be zero or more.");
  }

  const { error } = await client
    .from("positions")
    .update({
      status: "closed",
      closed_on: closedOn,
      exit_premium: exitPremium,
      exit_reason: String(form.get("exit_reason") ?? "").trim() || null,
    })
    .eq("id", id);

  if (error) return fail(error.message);

  revalidatePath("/positions");
  return { ok: true, message: "Position closed." };
}

export async function deletePosition(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const { client, user } = await session();
  if (!client || !user) return fail("Not signed in.");

  const id = Number(form.get("id"));
  if (!Number.isInteger(id)) return fail("Unknown position.");

  const { error } = await client.from("positions").delete().eq("id", id);
  if (error) return fail(error.message);

  revalidatePath("/positions");
  return { ok: true, message: "Position deleted." };
}

/**
 * Acknowledge an alert — and only acknowledge it.
 *
 * Migration 0002 revokes table-wide UPDATE on `alerts` and grants it back on
 * the `acknowledged` column alone, so the owner cannot rewrite the kind or the
 * message of an exit signal the scanner wrote. Sending any other column here
 * would be rejected by Postgres, deliberately.
 */
export async function acknowledgeAlert(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const { client, user } = await session();
  if (!client || !user) return fail("Not signed in.");

  const id = Number(form.get("id"));
  if (!Number.isInteger(id)) return fail("Unknown alert.");

  const { error } = await client.from("alerts").update({ acknowledged: true }).eq("id", id);
  if (error) return fail(error.message);

  revalidatePath("/positions");
  return OK;
}

export async function signOut(): Promise<void> {
  const client = await createSessionClient();
  await client?.auth.signOut();
  redirect("/");
}
