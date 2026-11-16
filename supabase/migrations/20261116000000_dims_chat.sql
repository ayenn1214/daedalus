-- DIMS chat: one team room. Every signed-in DIMS user (viewer and floor included) can read and post;
-- you can delete your own messages, admins any. Item links (parts, projects, orders …) travel in the text as
-- {{kind:id|label}} tokens, so the table needs nothing special for them.
--
-- Safe to run more than once; run after 20261113000000_dims_mrp.sql.

create table if not exists public.dims_chat (
  id        uuid        primary key default gen_random_uuid(),
  at        timestamptz not null default now(),
  user_id   uuid        default auth.uid() references auth.users (id) on delete set null,
  by_name   text        not null check (char_length(by_name) between 1 and 80),
  channel   text        not null default 'general' check (char_length(channel) between 1 and 40),
  body      text        not null check (char_length(body) between 1 and 4000)
);

create index if not exists dims_chat_at on public.dims_chat (at);

alter table public.dims_chat enable row level security;

drop policy if exists "dims chat read" on public.dims_chat;
create policy "dims chat read" on public.dims_chat for select to authenticated using (true);

drop policy if exists "dims chat post" on public.dims_chat;
create policy "dims chat post" on public.dims_chat for insert to authenticated
  with check (user_id = auth.uid());

drop policy if exists "dims chat delete" on public.dims_chat;
create policy "dims chat delete" on public.dims_chat for delete to authenticated
  using (user_id = auth.uid() or public.dims_role() = 'admin');

grant select, insert, delete on public.dims_chat to authenticated;

create or replace function public.dims_schema_version() returns integer
language sql immutable set search_path = public as $$ select 3 $$;
grant execute on function public.dims_schema_version() to authenticated;
