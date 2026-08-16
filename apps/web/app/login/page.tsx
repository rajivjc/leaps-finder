import { LoginForm } from "@/components/LoginForm";
import { Notice } from "@/components/Notice";
import { isConfigured } from "@/lib/supabase-browser";

export const dynamic = "force-dynamic";

/** Only same-origin paths are accepted as a post-login destination: a `next`
 * taken straight from the query string is attacker-controlled, and an absolute
 * URL there would turn this page into an open redirect. */
function safeNext(value: string | string[] | undefined): string {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) return "/positions";
  return candidate;
}

export default async function LoginPage(props: PageProps<"/login">) {
  const params = await props.searchParams;
  const next = safeNext(params.next);
  const error = Array.isArray(params.error) ? params.error[0] : params.error;

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--muted)]">
          Positions are private to the owner of this instance. Everything else — the screener,
          ticker pages, compare — is public and needs no account.
        </p>
      </section>

      {error && (
        <Notice title="That sign-in link did not work">
          {error === "unconfigured"
            ? "This deployment has no Supabase project configured."
            : `${error}. Magic links are single-use and expire quickly — request a fresh one below.`}
        </Notice>
      )}

      {isConfigured() ? (
        <LoginForm next={next} />
      ) : (
        <Notice title="Supabase is not configured">
          Set <code className="font-mono">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
          <code className="font-mono">NEXT_PUBLIC_SUPABASE_ANON_KEY</code> to enable sign-in.
        </Notice>
      )}
    </div>
  );
}
