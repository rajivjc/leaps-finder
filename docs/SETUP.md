# Supabase setup

Everything needed to point a fresh clone of this repo at your own Supabase project: create it,
apply the schema, verify the security policies actually took, and get the two keys to the four
places they belong.

Budget about fifteen minutes. Nothing here costs money — the whole design fits the free tier.

> **The one rule for this document:** the secret key bypasses every security policy in the
> database. Treat it like a root password. It belongs in exactly two places, listed in step 6,
> and neither of them is a file you commit.

---

## 1. Create the project

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard) and click **New project**.
2. Name it `leaps-finder` and pick the region closest to you. The scanner runs on GitHub's
   runners rather than your machine, so this mostly affects how snappy the dashboard feels.
3. Generate a strong database password and save it to your password manager immediately. This
   app never needs it — the scanner talks to the REST API, not Postgres directly — but it
   cannot be recovered later, only reset.

## 2. Apply the migrations

Open **SQL Editor → New query** in the dashboard and run these two files, in order, one at a
time:

1. [`supabase/migrations/0001_initial_schema.sql`](../supabase/migrations/0001_initial_schema.sql)
   — tables and indexes
2. [`supabase/migrations/0002_rls.sql`](../supabase/migrations/0002_rls.sql)
   — row-level security policies and grants

Order matters: the second file references tables the first one creates.

<details>
<summary>Using the Supabase CLI instead</summary>

`supabase link --project-ref <ref>` followed by `supabase db push` also works, but the CLI
expects migration filenames in its `20260815120000_name.sql` timestamp convention, and this
repo numbers them `0001_`/`0002_` for readability. Rename them first if you go this route.

</details>

## 3. Verify RLS actually took

Do not skip this. The publishable key is *designed* to end up in a browser where anyone can
read it — row-level security is the only thing standing between that key and your data. Run in
the SQL Editor:

```sql
select relname as table_name, relrowsecurity as rls_enabled
from pg_class
where relnamespace = 'public'::regnamespace and relkind = 'r'
order by relname;
```

All six tables must come back `true`:

| table | who can read | who can write |
|---|---|---|
| `tickers` | anyone | scanner only |
| `scans` | anyone | scanner only |
| `scan_results` | anyone | scanner only |
| `iv_snapshots` | anyone | scanner only |
| `positions` | the owner | the owner |
| `alerts` | the owner | scanner creates, owner acknowledges |

"Scanner only" is enforced by the absence of any insert or update policy: the scanner writes
with the secret key, which bypasses RLS, and no one else has a way in. Confirm the policies
themselves landed:

```sql
select tablename, policyname, cmd from pg_policies
where schemaname = 'public' order by tablename, policyname;
```

## 4. Lock down authentication

Go to **Authentication → Sign In / Providers → Email** and turn **"Allow new users to sign
up"** off.

This project is designed for a single owner ([SPEC.md §0](../SPEC.md)). Doing this before you
share the project URL anywhere means the window in which a stranger can create an account never
opens. You will add yourself later through **Authentication → Users → Invite user** when the
positions feature lands in M5.

## 5. Collect the keys

**Settings → API Keys.** Take the modern pair rather than the legacy JWTs — Supabase is
[deprecating `anon` and `service_role` by the end of 2026](https://supabase.com/docs/guides/api/api-keys).

| What | Looks like | Privileges |
|---|---|---|
| Project URL | `https://abcdefgh.supabase.co` | public |
| **Publishable** key | `sb_publishable_…` | respects RLS — safe in a browser |
| **Secret** key | `sb_secret_…` | **bypasses RLS entirely** |

Both work as drop-in replacements in this codebase; the environment variable names still say
`ANON_KEY` and `SERVICE_KEY`, which are just names. If your dashboard only offers the legacy
pair, they still function: `anon` maps to publishable, `service_role` to secret.

## 6. Distribute the keys

Four destinations. The split between them *is* the security model.

### GitHub Actions — where the scanner runs

Repository **Settings → Secrets and variables → Actions → New repository secret**, or from a
terminal in the repo:

```bash
gh secret set SUPABASE_URL
```

```bash
gh secret set SUPABASE_SERVICE_KEY
```

Each prompts for the value without echoing it. The names must match exactly — the workflows in
[`.github/workflows/`](../.github/workflows/) read those two.

### Your laptop — for `make scan`

```bash
cp scanner/.env.example scanner/.env
```

Fill in the same two values. The file is gitignored.

### Vercel — the deployed web app

Project **Settings → Environment Variables**: `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_ANON_KEY`. **Publishable key only.**

### Your laptop again — for `make web`

```bash
cp apps/web/.env.example apps/web/.env.local
```

Same two public values.

### The rule underneath all four

The secret key goes in **GitHub Actions secrets and `scanner/.env`, and nowhere else**. Never
Vercel, never the web app, and never behind a `NEXT_PUBLIC_` prefix — that prefix inlines the
value into the JavaScript bundle every visitor downloads.

## 7. Protecting them

- **`.gitignore` already covers `.env` and `.env.*`**, with `.env.example` explicitly
  re-included. An absent-minded `git add -A` will not sweep up a real key. Glance at
  `git status` before committing anyway.
- **Turn on push protection**: repository **Settings → Advanced Security → Push protection**.
  It is free on public repositories and blocks a commit containing a recognized key before it
  leaves your machine. GitHub also scans public repos and notifies Supabase on a match.
- **Actions masks secret values in logs**, but do not `echo` one or paste it into a bug report.
  This codebase keeps the key out of its own `repr` for the same reason — tracebacks end up in
  public Actions logs.
- **Never add a `pull_request_target` workflow** that runs code from a fork. Forked pull
  requests do not receive your secrets by default, and that default is worth keeping.

### If a key leaks

Assume any key that touched a commit, a screenshot, a chat window, or a pasted log is burned,
even if you deleted the message afterwards.

1. **Settings → API Keys**, revoke the exposed key and create a replacement.
2. Update the GitHub Actions secret and your local `scanner/.env`.
3. Rotating the publishable key is harmless. Rotating the secret key breaks only the scanner,
   and only until those two places are updated.

## 8. Verify end to end

```bash
make scan
```

This runs the identical job GitHub Actions runs, against your real project. It writes the
ticker universe and a `scans` row, and exits non-zero if more than 20% of the universe failed
to fetch — partial data is recorded as `failed` rather than presented as a complete scan.

Check **Table Editor → `scans`** for a row with `status = 'ok'`, and `scan_results` for the
per-symbol rows behind it.

Then run it the way it will actually run: repository **Actions → Weekly scan → Run workflow**.

## Notes on the free tier

- Projects **pause after 7 days of inactivity**. The weekly scan alone is enough to prevent
  that; the daily refresh job in M5 makes it certain.
- The row count this project generates is small — roughly 250 tickers, a few hundred scan
  results per week, and one IV snapshot per symbol per day.

---

*This project is educational tooling on delayed, unofficial data. Nothing in it constitutes
financial advice.*
