import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import type { Database } from "@/lib/types";

/**
 * Read-only Supabase client for server components (SPEC.md §8).
 *
 * The anon key only ever sees what RLS allows: scan data is public-select, and
 * positions/alerts are owner-only. Writes are the scanner's job (service key,
 * GitHub Actions).
 */
export function createReadClient(): SupabaseClient<Database> | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  // Returning null rather than throwing keeps `next build` working in CI, where
  // no Supabase project is configured. Callers render an unconfigured state.
  if (!url || !anonKey) return null;

  return createClient<Database>(url, anonKey, {
    auth: { persistSession: false },
  });
}
