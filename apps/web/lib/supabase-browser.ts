import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

import type { Database } from "@/lib/types";

/**
 * The browser half of the Supabase clients, kept in its own module.
 *
 * `lib/supabase.ts` imports `next/headers`, which is server-only — bundling it
 * into a Client Component is a build error, not a runtime surprise. Everything
 * here reads only `NEXT_PUBLIC_*` variables and so is safe on either side.
 */

export function supabaseConfig(): { url: string; key: string } | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) return null;
  return { url, key };
}

/** True when a Supabase project is configured at all (false in CI). */
export function isConfigured(): boolean {
  return supabaseConfig() !== null;
}

/**
 * The client that starts the magic-link flow.
 *
 * It uses the PKCE flow by default, storing the code verifier in a cookie on
 * this origin — which is why the link has to land on `/auth/callback` here and
 * be exchanged server-side rather than anywhere else.
 */
export function createClientSideClient(): SupabaseClient<Database> | null {
  const settings = supabaseConfig();
  if (!settings) return null;

  return createBrowserClient<Database>(settings.url, settings.key);
}
