import { createServerClient } from "@supabase/ssr";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { cookies } from "next/headers";

import { supabaseConfig } from "@/lib/supabase-browser";
import type { Database } from "@/lib/types";

/**
 * Three clients, all on the anon key, differing only in whose session they carry.
 *
 * RLS is the whole security model here (SPEC.md §2): scan data is public-select,
 * positions and alerts are owner-only, and the service key — the one that
 * bypasses all of it — never leaves GitHub Actions. So the question a client
 * answers is not "what may this key do" but "who is asking", and that is
 * entirely a matter of whether a session cookie rides along.
 */

/**
 * `supabaseConfig` returns null rather than throwing when no project is
 * configured, which keeps `next build` working in CI. Callers render an
 * unconfigured state.
 */
const config = supabaseConfig;

/**
 * Anonymous reads for the public pages (screener, ticker, compare).
 *
 * Deliberately session-free: these pages are the same for everyone, and a
 * client that read cookies would make them uncacheable for no gain.
 */
export function createReadClient(): SupabaseClient<Database> | null {
  const settings = config();
  if (!settings) return null;

  return createClient<Database>(settings.url, settings.key, {
    auth: { persistSession: false },
  });
}

/**
 * The owner's session, for server components and server actions (§8.4).
 *
 * `setAll` is wrapped in a try/catch because a Server Component cannot write
 * cookies — Next throws if it tries. That is not a swallowed error: the
 * middleware refreshes the session on every matching request and writes the
 * rotated cookies there, so by the time a Server Component reads, the tokens
 * are already current. Server Actions and Route Handlers *can* write, and this
 * same client does so for them.
 */
export async function createSessionClient(): Promise<SupabaseClient<Database> | null> {
  const settings = config();
  if (!settings) return null;

  const store = await cookies();

  return createServerClient<Database>(settings.url, settings.key, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (items) => {
        try {
          for (const { name, value, options } of items) store.set(name, value, options);
        } catch {
          // Server Component render pass — middleware owns the refresh.
        }
      },
    },
  });
}
