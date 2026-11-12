-- DIMS — Daedalus Inventory Management System: Supabase backend.
--
-- One row per record (part, transaction, project, location, vendor, client, order) instead of one JSON blob per
-- collection, so people editing different things never touch the same row. The database keeps stock right on its
-- own: recording a usage / receiving transaction adjusts the part's stock in the same database transaction, so
-- simultaneous withdrawals always add up. Every change is versioned (optimistic locking) and audited.
--
-- Roles (dims_profiles.role):
--   admin   everything, incl. changing people's roles and importing the old data
--   editor  add / edit / delete parts, projects, vendors, orders …            (default for new accounts)
--   floor   record stock in / out (transactions) only — the database applies the stock change
--   viewer  read only
-- The first account to sign up becomes admin. Accounts are created in Supabase ▸ Authentication ▸ Users.
--
-- Safe to run more than once. Leaves the old app_storage table alone (see dims_import_app_storage()).

-- ------------------------------------------------------------------ tables
create table if not exists public.dims_profiles (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  email        text,
  display_name text,
  role         text not null default 'editor' check (role in ('admin', 'editor', 'floor', 'viewer')),
  created_at   timestamptz not null default now()
);

create table if not exists public.dims_records (
  collection text        not null check (collection in ('parts', 'transactions', 'projects', 'locations', 'vendors',
                                                       'production-orders', 'clients', 'client-orders',
                                                       'part-orders')),
  id         text        not null check (length(id) between 1 and 64),
  data       jsonb       not null check (jsonb_typeof(data) = 'object'),
  version    integer     not null default 1,
  deleted    boolean     not null default false,
  updated_at timestamptz not null default now(),
  updated_by uuid                 default auth.uid(),
  primary key (collection, id)
);
create index if not exists dims_records_updated_at on public.dims_records (updated_at);
create index if not exists dims_records_part_of_tx on public.dims_records ((data ->> 'partId'))
  where collection = 'transactions';

create table if not exists public.dims_settings (
  key        text primary key check (key in ('exchange-rate', 'sync-meta')),
  value      jsonb,
  version    integer     not null default 1,
  updated_at timestamptz not null default now(),
  updated_by uuid                 default auth.uid()
);

create table if not exists public.dims_audit (
  id         bigserial primary key,
  at         timestamptz not null default now(),
  user_id    uuid,
  collection text,
  record_id  text,
  action     text,
  old        jsonb,
  new        jsonb
);
create index if not exists dims_audit_record on public.dims_audit (collection, record_id, at desc);

-- ------------------------------------------------------------------ helpers
create or replace function public.dims_role() returns text
language sql stable security definer set search_path = public as $$
  select coalesce((select role from public.dims_profiles where user_id = auth.uid()), 'viewer')
$$;

create or replace function public.dims_num(t text) returns numeric
language plpgsql immutable set search_path = public as $$
begin
  return nullif(trim(t), '')::numeric;
exception when others then
  return null;
end $$;

-- stock change a transaction record stands for (usage takes stock out, receiving puts it in; deleted = none)
create or replace function public.dims_tx_delta(d jsonb, is_deleted boolean) returns numeric
language sql immutable set search_path = public as $$
  select case
    when d is null or is_deleted then 0
    when d ->> 'mode' = 'usage' then -coalesce(public.dims_num(d ->> 'qty'), 0)
    when d ->> 'mode' = 'receiving' then coalesce(public.dims_num(d ->> 'qty'), 0)
    else 0 end
$$;

-- ------------------------------------------------------------------ profiles for new sign-ups
create or replace function public.dims_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.dims_profiles (user_id, email, display_name, role)
  values (new.id, new.email,
          coalesce(new.raw_user_meta_data ->> 'full_name', split_part(coalesce(new.email, ''), '@', 1)),
          case when exists (select 1 from public.dims_profiles where role = 'admin') then 'editor' else 'admin' end)
  on conflict (user_id) do nothing;
  return new;
end $$;

drop trigger if exists dims_on_auth_user on auth.users;
create trigger dims_on_auth_user after insert on auth.users
  for each row execute function public.dims_new_user();

-- accounts that already exist: editors, and the oldest one admin if there is no admin yet
insert into public.dims_profiles (user_id, email, display_name, role)
select u.id, u.email, coalesce(u.raw_user_meta_data ->> 'full_name', split_part(coalesce(u.email, ''), '@', 1)), 'editor'
from auth.users u
on conflict (user_id) do nothing;
update public.dims_profiles set role = 'admin'
where user_id = (select p.user_id from public.dims_profiles p join auth.users u on u.id = p.user_id
                 order by u.created_at limit 1)
  and not exists (select 1 from public.dims_profiles where role = 'admin');

-- ------------------------------------------------------------------ versioning
create or replace function public.dims_touch() returns trigger
language plpgsql set search_path = public as $$
begin
  if tg_table_name = 'dims_records' and (new.collection <> old.collection or new.id <> old.id) then
    raise exception 'collection and id cannot change';
  end if;
  new.version    := old.version + 1;
  new.updated_at := clock_timestamp();
  new.updated_by := coalesce(auth.uid(), old.updated_by);
  return new;
end $$;

drop trigger if exists dims_records_touch on public.dims_records;
create trigger dims_records_touch before update on public.dims_records
  for each row execute function public.dims_touch();
drop trigger if exists dims_settings_touch on public.dims_settings;
create trigger dims_settings_touch before update on public.dims_settings
  for each row execute function public.dims_touch();

create or replace function public.dims_stamp() returns trigger
language plpgsql set search_path = public as $$
begin
  new.version    := 1;
  new.updated_at := clock_timestamp();
  new.updated_by := coalesce(auth.uid(), new.updated_by);
  return new;
end $$;

drop trigger if exists dims_records_stamp on public.dims_records;
create trigger dims_records_stamp before insert on public.dims_records
  for each row execute function public.dims_stamp();

-- ------------------------------------------------------------------ stock follows transactions
create or replace function public.dims_apply_stock() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  d   numeric;
  pid text;
begin
  if coalesce(current_setting('dims.importing', true), '') = 'on' then
    return null;
  end if;
  if tg_op = 'INSERT' then
    d := public.dims_tx_delta(new.data, new.deleted);
  else
    d := public.dims_tx_delta(new.data, new.deleted) - public.dims_tx_delta(old.data, old.deleted);
  end if;
  pid := new.data ->> 'partId';
  if tg_op = 'UPDATE' and pid is distinct from old.data ->> 'partId' then
    -- moved to another part: take it off the old one, put it on the new one
    update public.dims_records
       set data = jsonb_set(data, '{currentStock}',
                            to_jsonb(coalesce(public.dims_num(data ->> 'currentStock'), 0)
                                     - public.dims_tx_delta(old.data, old.deleted)))
     where collection = 'parts' and id = old.data ->> 'partId';
    d := public.dims_tx_delta(new.data, new.deleted);
  end if;
  if d <> 0 and pid is not null then
    update public.dims_records
       set data = jsonb_set(jsonb_set(data, '{currentStock}',
                                      to_jsonb(coalesce(public.dims_num(data ->> 'currentStock'), 0) + d)),
                            '{updatedAt}', to_jsonb((extract(epoch from clock_timestamp()) * 1000)::bigint))
     where collection = 'parts' and id = pid;
  end if;
  return null;
end $$;

drop trigger if exists dims_tx_stock on public.dims_records;
create trigger dims_tx_stock after insert or update on public.dims_records
  for each row when (new.collection = 'transactions') execute function public.dims_apply_stock();

-- ------------------------------------------------------------------ audit trail
create or replace function public.dims_audit_row() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  n jsonb := to_jsonb(new);
  o jsonb := case when tg_op = 'UPDATE' then to_jsonb(old) end;
  s boolean := tg_table_name = 'dims_settings';
begin
  if coalesce(current_setting('dims.importing', true), '') = 'on' then
    return null;
  end if;
  insert into public.dims_audit (user_id, collection, record_id, action, old, new)
  values (auth.uid(),
          case when s then 'settings' else n ->> 'collection' end,
          case when s then n ->> 'key' else n ->> 'id' end,
          case when tg_op = 'INSERT' then 'insert'
               when not s and (n ->> 'deleted')::boolean and not (o ->> 'deleted')::boolean then 'delete'
               when not s and (o ->> 'deleted')::boolean and not (n ->> 'deleted')::boolean then 'restore'
               else 'update' end,
          case when s then o -> 'value' else o -> 'data' end,
          case when s then n -> 'value' else n -> 'data' end);
  return null;
end $$;

drop trigger if exists dims_records_audit on public.dims_records;
create trigger dims_records_audit after insert or update on public.dims_records
  for each row execute function public.dims_audit_row();
drop trigger if exists dims_settings_audit on public.dims_settings;
create trigger dims_settings_audit after insert or update on public.dims_settings
  for each row execute function public.dims_audit_row();

-- ------------------------------------------------------------------ access rules
alter table public.dims_profiles enable row level security;
alter table public.dims_records  enable row level security;
alter table public.dims_settings enable row level security;
alter table public.dims_audit    enable row level security;

revoke all on public.dims_profiles, public.dims_records, public.dims_settings, public.dims_audit from anon;
grant select, insert, update on public.dims_records, public.dims_settings to authenticated;
grant select, update on public.dims_profiles to authenticated;
grant select on public.dims_audit to authenticated;

drop policy if exists "dims read" on public.dims_records;
drop policy if exists "dims insert" on public.dims_records;
drop policy if exists "dims update" on public.dims_records;
create policy "dims read" on public.dims_records for select to authenticated using (true);
create policy "dims insert" on public.dims_records for insert to authenticated
  with check (public.dims_role() in ('admin', 'editor')
              or (public.dims_role() = 'floor' and collection = 'transactions'));
create policy "dims update" on public.dims_records for update to authenticated
  using (public.dims_role() in ('admin', 'editor') or (public.dims_role() = 'floor' and collection = 'transactions'))
  with check (public.dims_role() in ('admin', 'editor')
              or (public.dims_role() = 'floor' and collection = 'transactions'));

drop policy if exists "dims settings read" on public.dims_settings;
drop policy if exists "dims settings insert" on public.dims_settings;
drop policy if exists "dims settings update" on public.dims_settings;
create policy "dims settings read" on public.dims_settings for select to authenticated using (true);
-- anyone signed in may record the weekly backup (sync-meta); other settings need an editor
create policy "dims settings insert" on public.dims_settings for insert to authenticated
  with check (key = 'sync-meta' or public.dims_role() in ('admin', 'editor'));
create policy "dims settings update" on public.dims_settings for update to authenticated
  using (key = 'sync-meta' or public.dims_role() in ('admin', 'editor'))
  with check (key = 'sync-meta' or public.dims_role() in ('admin', 'editor'));

drop policy if exists "dims profiles read" on public.dims_profiles;
drop policy if exists "dims profiles admin" on public.dims_profiles;
create policy "dims profiles read" on public.dims_profiles for select to authenticated using (true);
create policy "dims profiles admin" on public.dims_profiles for update to authenticated
  using (public.dims_role() = 'admin') with check (public.dims_role() = 'admin');

drop policy if exists "dims audit read" on public.dims_audit;
create policy "dims audit read" on public.dims_audit for select to authenticated
  using (public.dims_role() in ('admin', 'editor'));

-- ------------------------------------------------------------------ one-time import of the old data
-- Copies the old one-JSON-per-collection table (app_storage, used by the web app) into dims_records.
-- Records that already exist are left alone, so running it twice is harmless. Admins only.
create or replace function public.dims_import_app_storage() returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  k     text;
  raw   text;
  n     integer;
  total jsonb := '{}'::jsonb;
begin
  if public.dims_role() <> 'admin' then
    raise exception 'Only a DIMS admin can import the old data';
  end if;
  if to_regclass('public.app_storage') is null then
    return jsonb_build_object('error', 'no app_storage table');
  end if;
  perform set_config('dims.importing', 'on', true);
  foreach k in array array['parts', 'transactions', 'projects', 'locations', 'vendors', 'production-orders',
                           'clients', 'client-orders', 'part-orders'] loop
    execute 'select value from public.app_storage where key = $1' into raw using k;
    if raw is null or raw = '' then
      continue;
    end if;
    insert into public.dims_records (collection, id, data)
    select k, e ->> 'id', e
    from jsonb_array_elements(raw::jsonb) e
    where jsonb_typeof(e) = 'object' and coalesce(e ->> 'id', '') <> ''
    on conflict (collection, id) do nothing;
    get diagnostics n = row_count;
    total := total || jsonb_build_object(k, n);
  end loop;
  execute 'select value from public.app_storage where key = $1' into raw using 'exchange-rate';
  if raw is not null and public.dims_num(raw) is not null then
    insert into public.dims_settings (key, value) values ('exchange-rate', to_jsonb(public.dims_num(raw)))
    on conflict (key) do nothing;
  end if;
  execute 'select value from public.app_storage where key = $1' into raw using 'sync-meta';
  if raw is not null and raw <> '' then
    insert into public.dims_settings (key, value) values ('sync-meta', raw::jsonb) on conflict (key) do nothing;
  end if;
  perform set_config('dims.importing', 'off', true);
  return total;
end $$;

revoke all on function public.dims_import_app_storage() from public, anon;
grant execute on function public.dims_import_app_storage() to authenticated;

-- ------------------------------------------------------------------ reporting (Excel / Power BI / SQL)
create or replace view public.dims_parts_report with (security_invoker = true) as
select r.id,
       r.data ->> 'name'                                  as name,
       r.data ->> 'type'                                  as type,
       r.data ->> 'category'                              as category,
       r.data ->> 'partNumber'                            as part_number,
       r.data ->> 'size'                                  as size,
       r.data ->> 'length'                                as length,
       public.dims_num(r.data ->> 'currentStock')         as current_stock,
       public.dims_num(r.data ->> 'reorderPoint')         as reorder_point,
       public.dims_num(r.data ->> 'reorderQty')           as reorder_qty,
       public.dims_num(r.data ->> 'unitCost')             as unit_cost,
       r.data ->> 'unitCostCurrency'                      as currency,
       public.dims_num(r.data ->> 'currentStock') <= public.dims_num(r.data ->> 'reorderPoint') as needs_reorder,
       r.updated_at
from public.dims_records r
where r.collection = 'parts' and not r.deleted;
grant select on public.dims_parts_report to authenticated;

-- ------------------------------------------------------------------ live updates
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    begin
      alter publication supabase_realtime add table public.dims_records;
    exception when duplicate_object then null;
    end;
    begin
      alter publication supabase_realtime add table public.dims_settings;
    exception when duplicate_object then null;
    end;
  end if;
end $$;
