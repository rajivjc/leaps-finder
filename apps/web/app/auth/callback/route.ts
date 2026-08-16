import { NextResponse, type NextRequest } from "next/server";

import { createSessionClient } from "@/lib/supabase";

/**
 * Where the magic link lands (SPEC.md §0).
 *
 * Two shapes have to be handled, because which one arrives depends on the email
 * template configured in the Supabase dashboard rather than on anything in this
 * repo:
 *
 *   * `?code=…` — the PKCE flow the browser client starts by default. The code
 *     verifier lives in a cookie set on this origin, which is why this exchange
 *     has to happen server-side here rather than in the browser.
 *   * `?token_hash=…&type=magiclink` — the token-hash template.
 *
 * A Route Handler is the right home for both: it may write cookies, and setting
 * the session *is* writing cookies.
 */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const tokenHash = searchParams.get("token_hash");
  const type = searchParams.get("type");
  const next = searchParams.get("next");

  const destination = next && next.startsWith("/") && !next.startsWith("//") ? next : "/positions";

  const supabase = await createSessionClient();
  if (!supabase) return NextResponse.redirect(`${origin}/login?error=unconfigured`);

  let message: string | null = null;

  if (code) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    message = error?.message ?? null;
  } else if (tokenHash) {
    const { error } = await supabase.auth.verifyOtp({
      type: (type as "magiclink" | "email" | "invite" | "recovery") ?? "magiclink",
      token_hash: tokenHash,
    });
    message = error?.message ?? null;
  } else {
    message = "the sign-in link carried no token";
  }

  if (message) {
    const login = new URL("/login", origin);
    login.searchParams.set("error", message);
    return NextResponse.redirect(login);
  }

  return NextResponse.redirect(`${origin}${destination}`);
}
