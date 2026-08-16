"use client";

/**
 * Magic-link sign-in (SPEC.md §0: Supabase Auth, single allow-listed owner).
 *
 * `shouldCreateUser: false` matters. Public signups are disabled in the Supabase
 * dashboard, so this is not the security boundary — but without it a typo'd
 * address would still be *attempted* as a signup and the resulting error would
 * read as a server fault rather than "that is not the owner's address".
 *
 * The response is deliberately not told apart on the client either way: Supabase
 * returns the same success for a known and an unknown address, and surfacing the
 * difference would turn this form into an account-enumeration oracle for a page
 * that is on the public internet.
 */

import { useState } from "react";

import { createClientSideClient } from "@/lib/supabase-browser";

type State = { status: "idle" | "sending" } | { status: "sent" } | { status: "error"; message: string };

export function LoginForm({ next }: { next: string }) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<State>({ status: "idle" });

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const client = createClientSideClient();
    if (!client) {
      setState({ status: "error", message: "Supabase is not configured in this environment." });
      return;
    }

    setState({ status: "sending" });
    const redirect = new URL("/auth/callback", window.location.origin);
    redirect.searchParams.set("next", next);

    const { error } = await client.auth.signInWithOtp({
      email: email.trim(),
      options: { shouldCreateUser: false, emailRedirectTo: redirect.toString() },
    });

    setState(error ? { status: "error", message: error.message } : { status: "sent" });
  }

  if (state.status === "sent") {
    return (
      <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6">
        <h2 className="text-sm font-semibold">Check your email</h2>
        <p className="mt-2 text-sm leading-relaxed text-[var(--muted)]">
          If <span className="font-mono">{email.trim()}</span> is the owner account, a sign-in link
          is on its way. The link opens a session in this browser and expires shortly.
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="max-w-sm space-y-3">
      <label className="block space-y-1">
        <span className="text-xs text-[var(--muted)]">Email</span>
        <input
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="owner@example.com"
          className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1.5 text-sm"
        />
      </label>

      <button
        type="submit"
        disabled={state.status === "sending"}
        className="rounded border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--border)] disabled:opacity-50"
      >
        {state.status === "sending" ? "Sending…" : "Email me a sign-in link"}
      </button>

      {state.status === "error" && (
        <p className="text-xs leading-relaxed text-[var(--warn-fg)]">{state.message}</p>
      )}
    </form>
  );
}
