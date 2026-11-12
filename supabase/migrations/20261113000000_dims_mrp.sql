-- DIMS MRP update: the part-number registry.
--
-- * dims_records accepts the 'part-numbers' collection (one row per number: M10-0001 …).
-- * dims_reserve_part_numbers(prefix, count, project, note, by) hands out the next numbers after the highest one
--   in use for that prefix (registry + part numbers already on parts), under a lock per prefix, so two people
--   reserving at the same moment never get the same number. Editors and admins only.
-- * dims_schema_version() tells the app this update is installed.
--
-- Safe to run more than once; run after 20261112000000_dims.sql.

alter table public.dims_records drop constraint if exists dims_records_collection_check;
alter table public.dims_records add constraint dims_records_collection_check
  check (collection in ('parts', 'transactions', 'projects', 'locations', 'vendors', 'production-orders', 'clients',
                        'client-orders', 'part-orders', 'part-numbers'));

create index if not exists dims_records_pn_prefix on public.dims_records ((data ->> 'prefix'))
  where collection = 'part-numbers';

create or replace function public.dims_schema_version() returns integer
language sql immutable set search_path = public as $$ select 2 $$;
grant execute on function public.dims_schema_version() to authenticated;

create or replace function public.dims_reserve_part_numbers(p_prefix text, p_count integer, p_project text default null,
                                                            p_note text default '', p_by text default null)
returns setof public.dims_records
language plpgsql security definer set search_path = public as $$
declare
  pre text := upper(regexp_replace(coalesce(p_prefix, ''), '[^A-Za-z0-9]', '', 'g'));
  top integer;
  i integer;
  pn text;
begin
  if public.dims_role() not in ('admin', 'editor') then
    raise exception 'Only DIMS editors and admins can reserve part numbers' using errcode = '42501';
  end if;
  if pre = '' or length(pre) > 8 then
    raise exception 'Prefix must be 1-8 letters / digits';
  end if;
  if p_count is null or p_count < 1 or p_count > 200 then
    raise exception 'Reserve between 1 and 200 numbers';
  end if;
  perform pg_advisory_xact_lock(hashtext('dims-part-number:' || pre));
  select greatest(
           coalesce((select max((data ->> 'seq')::integer) from public.dims_records
                      where collection = 'part-numbers' and data ->> 'prefix' = pre), 0),
           coalesce((select max(substring(upper(data ->> 'partNumber') from '^' || pre || '-([0-9]+)$')::integer)
                       from public.dims_records
                      where collection = 'parts' and not deleted
                        and upper(data ->> 'partNumber') ~ ('^' || pre || '-[0-9]+$')), 0))
    into top;
  for i in 1 .. p_count loop
    -- 4-digit sequence (C30-0001, P00-0010); numeric material prefixes use 3 (1000-001)
    pn := pre || '-' || lpad((top + i)::text, greatest(case when pre ~ '^[0-9]+$' then 3 else 4 end,
                                                       length((top + i)::text)), '0');
    return query
      insert into public.dims_records (collection, id, data)
      values ('part-numbers', pn, jsonb_build_object(
                'id', pn, 'prefix', pre, 'seq', top + i, 'status', 'reserved', 'projectId', p_project,
                'partId', null, 'note', coalesce(p_note, ''), 'createdBy', coalesce(p_by, ''),
                'createdAt', (extract(epoch from clock_timestamp()) * 1000)::bigint))
      returning *;
  end loop;
end $$;

revoke all on function public.dims_reserve_part_numbers(text, integer, text, text, text) from public, anon;
grant execute on function public.dims_reserve_part_numbers(text, integer, text, text, text) to authenticated;
