import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

/**
 * Session refresh and the gate on /positions (SPEC.md §8.4).
 *
 * This is Next 16's `proxy` convention — the same request-time hook earlier
 * versions spelled `middleware`, which 16 deprecates.
 *
 * Two jobs, and the first is the subtle one. Supabase access tokens are
 * short-lived; refreshing one rotates the cookies, and only a Route Handler,
 * Server Action or this hook may write cookies in the App Router. It is the one
 * place that runs on *every* request, so it is where the refresh belongs —
 * without it a Server Component would keep reading a token that expired an hour
 * ago and the owner would be silently logged out mid-session.
 *
 * `getUser()` rather than `getSession()` is load-bearing: `getSession` reads the
 * cookie and believes it, while `getUser` revalidates against the auth server.
 * A gate that trusts an unverified cookie is not a gate.
 */
export async function proxy(request: NextRequest) {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  // No project configured (CI, a fresh clone): there is no session to refresh
  // and nothing to protect. The page renders its own unconfigured state.
  if (!url || !key) return NextResponse.next({ request });

  let response = NextResponse.next({ request });

  const supabase = createServerClient(url, key, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (items) => {
        for (const { name, value } of items) request.cookies.set(name, value);
        response = NextResponse.next({ request });
        for (const { name, value, options } of items) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user && request.nextUrl.pathname.startsWith("/positions")) {
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    // So the owner lands back where they were aiming after signing in.
    login.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(login);
  }

  if (user && request.nextUrl.pathname === "/login") {
    const positions = request.nextUrl.clone();
    positions.pathname = "/positions";
    positions.search = "";
    return NextResponse.redirect(positions);
  }

  return response;
}

export const config = {
  /**
   * Everything except static assets and images. The public pages are matched
   * too — not to gate them, but so a signed-in owner's session stays fresh
   * while they browse the screener and does not expire the moment they click
   * through to /positions.
   */
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)"],
};
