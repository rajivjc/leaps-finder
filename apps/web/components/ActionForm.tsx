"use client";

/**
 * The shared shell for every write on /positions.
 *
 * Each mutation is a plain `<form>` bound to a Server Action through
 * `useActionState`: the page keeps working without JavaScript, the pending
 * state is real rather than optimistic, and — the part that matters here —
 * a failed write says so instead of appearing to succeed. A risk page that
 * silently drops a close is worse than one that refuses it loudly.
 */

import { useActionState } from "react";

import type { ActionResult } from "@/app/positions/actions";

export type Action = (
  previous: ActionResult | null,
  form: FormData,
) => Promise<ActionResult>;

export function ActionForm({
  action,
  children,
  className,
  onSuccessMessage = true,
}: {
  action: Action;
  children: (pending: boolean) => React.ReactNode;
  className?: string;
  onSuccessMessage?: boolean;
}) {
  const [state, submit, pending] = useActionState(action, null);

  return (
    <form action={submit} className={className}>
      {children(pending)}
      {state && !state.ok && (
        <p className="mt-2 rounded border border-[var(--warn-border)] bg-[var(--warn-bg)] px-2 py-1 text-xs text-[var(--warn-fg)]">
          {state.message}
        </p>
      )}
      {state?.ok && onSuccessMessage && state.message && (
        <p className="mt-2 text-xs text-[var(--pass)]">{state.message}</p>
      )}
    </form>
  );
}

/** The one button style every action on this page uses. */
export function SubmitButton({
  pending,
  children,
  variant = "default",
}: {
  pending: boolean;
  children: React.ReactNode;
  variant?: "default" | "quiet";
}) {
  const base =
    "rounded border px-2.5 py-1 text-xs font-medium disabled:opacity-50 disabled:cursor-progress";
  const style =
    variant === "quiet"
      ? "border-[var(--border)] text-[var(--muted)] hover:text-[var(--foreground)]"
      : "border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--border)]";

  return (
    <button type="submit" disabled={pending} className={`${base} ${style}`}>
      {pending ? "Working…" : children}
    </button>
  );
}
