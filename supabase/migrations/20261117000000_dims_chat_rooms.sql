-- DIMS chat: direct messages and group chats next to the team room.
-- A room lists its members (auth user ids). Messages in a room can only be read and posted by its members — the
-- server enforces it (row-level security), so a DM is private even from admins' DIMS. 'general' stays the team room.
--
-- Safe to run more than once; run after 20261116000000_dims_chat.sql.

create table if not exists public.dims_chat_rooms (
  id          text        primary key check (char_length(id) between 3 and 40 and id <> 'general'),
  kind        text        not null check (kind in ('dm', 'group')),
  name        text        not null default '' check (char_length(name) <= 80),
  members     uuid[]      not null check (cardinality(members) between 1 and 200),
  created_by  uuid        default auth.uid() references auth.users (id) on delete set null,
  created_at  timestamptz not null default now()
);

alter table public.dims_chat_rooms enable row level security;

drop policy if exists "dims chat rooms read" on public.dims_chat_rooms;
create policy "dims chat rooms read" on public.dims_chat_rooms for select to authenticated
  using (auth.uid() = any (members));

drop policy if exists "dims chat rooms create" on public.dims_chat_rooms;
create policy "dims chat rooms create" on public.dims_chat_rooms for insert to authenticated
  with check (auth.uid() = any (members) and created_by = auth.uid());

-- members can rename a group, add people, or leave (a DM's two people never change)
drop policy if exists "dims chat rooms edit" on public.dims_chat_rooms;
create policy "dims chat rooms edit" on public.dims_chat_rooms for update to authenticated
  using (auth.uid() = any (members))
  with check (kind = 'group');

grant select, insert, update on public.dims_chat_rooms to authenticated;

-- may the signed-in user see / post in this channel?
create or replace function public.dims_chat_can(ch text) returns boolean
language sql stable security definer set search_path = public as $$
  select ch = 'general'
      or exists (select 1 from public.dims_chat_rooms r where r.id = ch and auth.uid() = any (r.members));
$$;
revoke all on function public.dims_chat_can(text) from public;
grant execute on function public.dims_chat_can(text) to authenticated;

drop policy if exists "dims chat read" on public.dims_chat;
create policy "dims chat read" on public.dims_chat for select to authenticated
  using (public.dims_chat_can(channel));

drop policy if exists "dims chat post" on public.dims_chat;
create policy "dims chat post" on public.dims_chat for insert to authenticated
  with check (user_id = auth.uid() and public.dims_chat_can(channel));

create index if not exists dims_chat_channel_at on public.dims_chat (channel, at);

create or replace function public.dims_schema_version() returns integer
language sql immutable set search_path = public as $$ select 4 $$;
grant execute on function public.dims_schema_version() to authenticated;
