-- Operation Bosphorus: Supabase setup
-- Run once in Supabase dashboard -> SQL Editor -> New query -> Run.
-- Safe to re-run.

------------------------------------------------------------------
-- 1. Crew list. EDIT THE EMAILS before running.
--    Only these emails can create an account or see any data.
------------------------------------------------------------------
create table if not exists public.crew_members (
  email     text primary key check (email = lower(email)),
  crew_key  text not null unique check (crew_key in ('umang','sagar','dev','abhi','nick')),
  is_owner  boolean not null default false
);

insert into public.crew_members (email, crew_key, is_owner) values
  ('umang@example.com', 'umang', false),
  ('sagar@example.com', 'sagar', false),
  ('dev@example.com',   'dev',   false),
  ('abhi@example.com',  'abhi',  false),
  ('nick@example.com',  'nick',  false)
on conflict (email) do update set crew_key = excluded.crew_key, is_owner = excluded.is_owner;

-- Mark the organizer (writes sealed personal messages, unseals awards early):
-- update public.crew_members set is_owner = true where crew_key = 'sagar';

------------------------------------------------------------------
-- 2. Helper functions
------------------------------------------------------------------
create or replace function public.my_crew_key() returns text
language sql stable security definer set search_path = public as $$
  select crew_key from public.crew_members where email = lower(auth.jwt() ->> 'email')
$$;

create or replace function public.is_crew() returns boolean
language sql stable security definer set search_path = public as $$
  select public.my_crew_key() is not null
$$;

create or replace function public.is_owner() returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce((select is_owner from public.crew_members where email = lower(auth.jwt() ->> 'email')), false)
$$;

-- Who may create or change a document in each collection.
create or replace function public.can_write(c text, d jsonb) returns boolean
language sql stable security definer set search_path = public as $$
  select public.is_crew() and case c
    when 'secrets' then public.is_owner()
    when 'votes'   then d ->> 'crew' = public.my_crew_key()                  -- you can only cast your own vote
    when 'vault'   then d ->> 'by'   = public.my_crew_key() or public.is_owner()
    when 'jokes'   then d ->> 'by'   = public.my_crew_key() or public.is_owner()
    when 'adjust'  then public.my_crew_key() = 'umang' or public.is_owner() -- groom's decrees
    else true
  end
$$;

-- Who may delete a document (checked against the existing row).
create or replace function public.can_delete(c text, d jsonb) returns boolean
language sql stable security definer set search_path = public as $$
  select public.is_crew() and case c
    when 'secrets' then public.is_owner()
    when 'vault'   then d ->> 'by' = public.my_crew_key() or public.is_owner()
    when 'jokes'   then d ->> 'by' = public.my_crew_key() or public.is_owner()
    when 'adjust'  then public.my_crew_key() = 'umang' or public.is_owner()
    else true
  end
$$;

------------------------------------------------------------------
-- 3. Shared documents (votes, points, uploads, profiles, ...)
------------------------------------------------------------------
create table if not exists public.docs (
  coll        text not null check (coll in ('crew','challenges','completions','polls','votes','vault','jokes','adjust','bookings','secrets','settings')),
  id          text not null check (length(id) between 1 and 200),
  data        jsonb not null check (pg_column_size(data) < 65536),
  updated_by  text,
  updated_at  timestamptz not null default now(),
  primary key (coll, id)
);
alter table public.docs replica identity full;  -- realtime delete events carry the row

create or replace function public.stamp_doc() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  new.updated_by := public.my_crew_key();
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists stamp_doc on public.docs;
create trigger stamp_doc before insert or update on public.docs
  for each row execute function public.stamp_doc();

alter table public.crew_members enable row level security;
alter table public.docs enable row level security;

drop policy if exists "crew reads crew list" on public.crew_members;
create policy "crew reads crew list" on public.crew_members for select to authenticated using (public.is_crew());

drop policy if exists "crew reads docs"   on public.docs;
drop policy if exists "crew inserts docs" on public.docs;
drop policy if exists "crew updates docs" on public.docs;
drop policy if exists "crew deletes docs" on public.docs;
create policy "crew reads docs"   on public.docs for select to authenticated using (public.is_crew());
create policy "crew inserts docs" on public.docs for insert to authenticated with check (public.can_write(coll, data));
create policy "crew updates docs" on public.docs for update to authenticated using (public.can_write(coll, data)) with check (public.can_write(coll, data));
create policy "crew deletes docs" on public.docs for delete to authenticated using (public.can_delete(coll, data));

-- Live updates
do $$ begin
  alter publication supabase_realtime add table public.docs;
exception when duplicate_object then null; end $$;

------------------------------------------------------------------
-- 4. Block sign-ups from emails that aren't on the crew list
------------------------------------------------------------------
create or replace function public.enforce_crew_signup() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from public.crew_members where email = lower(new.email)) then
    raise exception 'Email is not on the crew list';
  end if;
  return new;
end $$;
drop trigger if exists enforce_crew_signup on auth.users;
create trigger enforce_crew_signup before insert on auth.users
  for each row execute function public.enforce_crew_signup();

------------------------------------------------------------------
-- 5. Private photo/video bucket (50 MB per file = free-plan maximum)
------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('vault', 'vault', false, 52428800, array['image/*','video/*'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "crew views vault"   on storage.objects;
drop policy if exists "crew uploads vault" on storage.objects;
drop policy if exists "crew deletes vault" on storage.objects;
create policy "crew views vault" on storage.objects for select to authenticated
  using (bucket_id = 'vault' and public.is_crew());
create policy "crew uploads vault" on storage.objects for insert to authenticated
  with check (bucket_id = 'vault' and (storage.foldername(name))[1] = public.my_crew_key());
create policy "crew deletes vault" on storage.objects for delete to authenticated
  using (bucket_id = 'vault' and ((storage.foldername(name))[1] = public.my_crew_key() or public.is_owner()));

------------------------------------------------------------------
-- 6. Starter polls (open decisions from the itinerary)
------------------------------------------------------------------
insert into public.docs (coll, id, data) values
  ('polls','p-mon-adventure','{"q":"Mon 16, 3:00 PM in Sapanca: what''s the adventure?","o":["ATV forest trails","Ziplining","Both, split the crew"],"by":"umang","at":1793600000000,"closed":false}'),
  ('polls','p-tue-morning','{"q":"Tue 17, 10:15 AM: Ormanya trails or horse riding?","o":["Ormanya trails + Hobbit houses","Horse riding","Sleep in at the bungalow"],"by":"umang","at":1793600001000,"closed":false}'),
  ('polls','p-wine-route','{"q":"Wine day: keep Tekirdağ or swap to Kırklareli?","o":["Tekirdağ: Chateau Nuzun + Barbare (1.5 hrs)","Kırklareli: Arcadia chef''s table (2.5 hrs each way)"],"by":"umang","at":1793600002000,"closed":false}'),
  ('polls','p-fri-afternoon','{"q":"Fri 20, 2:00 PM before Oligark: Dolmabahçe or recovery?","o":["Dolmabahçe Palace (last entry ≈ 4 PM)","Nap and reset","Hamam round two"],"by":"umang","at":1793600003000,"closed":false}')
on conflict (coll, id) do nothing;
