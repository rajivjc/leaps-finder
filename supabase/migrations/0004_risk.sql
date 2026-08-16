-- M5 risk engine: current equity, contract marks, alert provenance (SPEC.md §7).
--
-- Four additions, each covering a gap §7 assumes but §2's schema does not provide.

-- ---------------------------------------------------------------------------
-- 1. Current account equity.
--
-- `positions.account_equity_at_entry` is a *snapshot* taken when a position was
-- opened; it is deliberately immutable, because the circuit breaker measures
-- loss against the equity the sleeve was sized against. The size calculator and
-- the 15% sleeve cap need the *current* figure instead, and §7 says it is
-- persisted per user — so it gets its own row rather than being inferred from
-- the newest position (which would leave a user with no positions unable to
-- size their first one).
-- ---------------------------------------------------------------------------
create table if not exists user_settings (
  user_id uuid primary key references auth.users on delete cascade,
  account_equity numeric check (account_equity is null or account_equity > 0),
  updated_at timestamptz default now()
);

alter table user_settings enable row level security;

drop policy if exists user_settings_owner_select on user_settings;
create policy user_settings_owner_select on user_settings
  for select using (auth.uid() = user_id);

drop policy if exists user_settings_owner_insert on user_settings;
create policy user_settings_owner_insert on user_settings
  for insert with check (auth.uid() = user_id);

drop policy if exists user_settings_owner_update on user_settings;
create policy user_settings_owner_update on user_settings
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- 2. Sizing overrides.
--
-- §7's caps are "block-level warnings, not hard blocks (user may override; log
-- it)". The log lives on the position it is about: an override is a fact about
-- one entry, and keeping it there means it cannot be separated from the trade
-- it excused. Null = the entry breached nothing, or breached nothing the owner
-- was warned about.
-- ---------------------------------------------------------------------------
alter table positions add column if not exists sizing_override text;

-- ---------------------------------------------------------------------------
-- 3. Daily marks for held contracts.
--
-- §7's premium stop is `current mid ≤ 50% of entry premium`, checked daily. The
-- scanner selects contracts *from* a chain; pricing one the owner already holds
-- is a different lookup, and its result has to be kept rather than consumed and
-- discarded for two reasons:
--
--   * an alert must be able to name the mark that fired it (a message quoting a
--     price from a different run than the one that triggered is the same class
--     of bug as a chart drawn from a scan the page is not showing); and
--   * /positions shows P&L, and every page in this app reads precomputed rows —
--     §10 caps the screener at one second by never fetching upstream at request
--     time, and the positions page has no reason to be the exception.
--
-- Keyed by (position_id, mark_date): a mark is one day's price for one holding,
-- so a rerun of the same day's refresh overwrites rather than duplicating.
-- ---------------------------------------------------------------------------
create table if not exists position_marks (
  position_id bigint references positions(id) on delete cascade,
  mark_date date not null,
  scan_id bigint references scans(id),   -- the refresh run that took the mark
  bid numeric,
  ask numeric,
  mid numeric,
  underlying_close numeric,
  primary key (position_id, mark_date)
);

alter table position_marks enable row level security;

-- Ownership is inherited through the parent position, as with alerts. Writes are
-- the scanner's (service key, bypasses RLS), so there is no insert policy: the
-- owner reads marks, never authors them.
drop policy if exists position_marks_owner_select on position_marks;
create policy position_marks_owner_select on position_marks
  for select using (
    exists (
      select 1 from positions p
      where p.id = position_marks.position_id and p.user_id = auth.uid()
    )
  );

-- ---------------------------------------------------------------------------
-- 4. Alert provenance and sleeve-level ownership.
--
-- `scan_id` / `as_of_date` tie an alert to the run and the trading day whose
-- data produced it, so its message can be read as a statement about a specific
-- day rather than as a floating assertion.
--
-- `user_id` exists because not every alert is about a position. §7's circuit
-- breaker is a fact about the whole sleeve, so it has no position to hang from
-- — and 0002's select policy reaches ownership *through* `position_id`, which
-- would make a sleeve-level alert invisible to the only person allowed to see
-- it. Owning alerts directly fixes that and simplifies the policies.
-- ---------------------------------------------------------------------------
alter table alerts add column if not exists scan_id bigint references scans(id);
alter table alerts add column if not exists as_of_date date;
alter table alerts add column if not exists user_id uuid references auth.users;

-- Backfill from the parent position before the column is required. (Empty in
-- practice — M5 is the first milestone with auth, so no alert has been written
-- yet — but the migration must not depend on that.)
update alerts
   set user_id = p.user_id
  from positions p
 where p.id = alerts.position_id
   and alerts.user_id is null;

alter table alerts alter column user_id set not null;

drop policy if exists alerts_owner_select on alerts;
create policy alerts_owner_select on alerts
  for select using (auth.uid() = user_id);

drop policy if exists alerts_owner_update on alerts;
create policy alerts_owner_update on alerts
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- The column-level grant from 0002 still stands and still matters: `revoke
-- update on alerts` removed table-wide UPDATE, and `grant update (acknowledged)`
-- is column-scoped, so the columns added above are *not* writable by the owner.
-- The scanner's exit record stays the scanner's. Restated, not re-granted —
-- adding a column grants nothing.

create index if not exists alerts_user_idx on alerts (user_id, created_at desc);

-- Deleting a position has to be possible. 0001 gave `alerts.position_id` a plain
-- reference, so a position with any alert against it could not be removed at all
-- — the delete would fail on the foreign key. Cascade is the right resolution:
-- an alert is a statement *about* a position, and it has no meaning once the
-- position it describes is gone. (Closing a position, which is what the owner
-- normally does, leaves everything in place — only an outright delete cascades.)
alter table alerts drop constraint if exists alerts_position_id_fkey;
alter table alerts
  add constraint alerts_position_id_fkey
  foreign key (position_id) references positions(id) on delete cascade;

-- `kind` gained one value in M5: trend_break | stoch_below_20 | premium_stop |
-- time_exit | earnings_soon | circuit_breaker. Still no check constraint — the
-- scanner is the only writer, and a constraint here would turn a new rule into
-- a migration.
