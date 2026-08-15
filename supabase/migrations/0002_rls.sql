-- Row Level Security (SPEC.md §2).
--
-- Public read on scan data; no public writes. The scanner writes with the service
-- key, which bypasses RLS entirely, so the tables below get select-only policies
-- and nothing else. Positions and alerts are owner-only.

alter table tickers enable row level security;
alter table scans enable row level security;
alter table scan_results enable row level security;
alter table iv_snapshots enable row level security;
alter table positions enable row level security;
alter table alerts enable row level security;

-- Scan data: readable by anyone (anon key), writable only by the service key.
drop policy if exists tickers_public_read on tickers;
create policy tickers_public_read on tickers
  for select using (true);

drop policy if exists scans_public_read on scans;
create policy scans_public_read on scans
  for select using (true);

drop policy if exists scan_results_public_read on scan_results;
create policy scan_results_public_read on scan_results
  for select using (true);

drop policy if exists iv_snapshots_public_read on iv_snapshots;
create policy iv_snapshots_public_read on iv_snapshots
  for select using (true);

-- Positions: owner-only, all verbs.
drop policy if exists positions_owner_select on positions;
create policy positions_owner_select on positions
  for select using (auth.uid() = user_id);

drop policy if exists positions_owner_insert on positions;
create policy positions_owner_insert on positions
  for insert with check (auth.uid() = user_id);

drop policy if exists positions_owner_update on positions;
create policy positions_owner_update on positions
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists positions_owner_delete on positions;
create policy positions_owner_delete on positions
  for delete using (auth.uid() = user_id);

-- Alerts: ownership is inherited through the parent position.
drop policy if exists alerts_owner_select on alerts;
create policy alerts_owner_select on alerts
  for select using (
    exists (
      select 1 from positions p
      where p.id = alerts.position_id and p.user_id = auth.uid()
    )
  );

-- The owner may only acknowledge alerts; the scanner (service key) creates them.
-- RLS scopes rows, not columns, so the column restriction is a grant: without it
-- the policy below would also let the owner rewrite an alert's kind or message
-- and quietly edit the exit-signal record the scanner wrote.
revoke update on alerts from anon, authenticated;
grant update (acknowledged) on alerts to authenticated;

drop policy if exists alerts_owner_update on alerts;
create policy alerts_owner_update on alerts
  for update using (
    exists (
      select 1 from positions p
      where p.id = alerts.position_id and p.user_id = auth.uid()
    )
  ) with check (
    exists (
      select 1 from positions p
      where p.id = alerts.position_id and p.user_id = auth.uid()
    )
  );

-- Public signups are disabled in Supabase Auth settings (single allow-listed owner,
-- SPEC.md §0); there is no policy that can express that here.
