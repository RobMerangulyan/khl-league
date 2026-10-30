-- KHL League: database setup for Supabase
-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run.
-- It is safe to run again; it updates prices and functions without deleting teams.

-- ---------------------------------------------------------------
-- Clubs and prices
-- ---------------------------------------------------------------
create table if not exists public.clubs (
  code     text primary key,
  khl_id   int  unique not null,      -- club id used by khl.ru
  name_en  text not null,
  name_ru  text not null,
  city     text not null default '',
  conf     text not null check (conf in ('W','E')),
  price    int  not null check (price between 1 and 20)
);

insert into public.clubs (code, khl_id, name_en, name_ru, city, conf, price) values
  ('LOK',    1, 'Lokomotiv',        'Локомотив',     'Yaroslavl',       'W', 10),
  ('CSKA',   2, 'CSKA',             'ЦСКА',          'Moscow',          'W',  9),
  ('TOR',   26, 'Torpedo',          'Торпедо',       'Nizhny Novgorod', 'W',  4),
  ('SPA',    7, 'Spartak',          'Спартак',       'Moscow',          'W',  5),
  ('SEV',   56, 'Severstal',        'Северсталь',    'Cherepovets',     'W',  3),
  ('DRA',  568, 'Shanghai Dragons', 'Драконы',       'Shanghai',        'W',  4),
  ('DYN',  719, 'Dynamo',           'Динамо М',      'Moscow',          'W',  7),
  ('DMN',  207, 'Dinamo',           'Динамо Мн',     'Minsk',           'W',  6),
  ('SKA',   24, 'SKA',              'СКА',           'St. Petersburg',  'W',  6),
  ('SOC',  451, 'HC Sochi',         'ХК Сочи',       'Sochi',           'W',  1),
  ('LAD',   66, 'Lada',             'Лада',          'Togliatti',       'W',  1),
  ('MMG',   37, 'Metallurg',        'Металлург Мг',  'Magnitogorsk',    'E',  9),
  ('NKH',   71, 'Neftekhimik',      'Нефтехимик',    'Nizhnekamsk',     'E',  2),
  ('BAR',  198, 'Barys',            'Барыс',         'Astana',          'E',  1),
  ('AVT',  190, 'Avtomobilist',     'Автомобилист',  'Yekaterinburg',   'E',  7),
  ('AKB',   53, 'Ak Bars',          'Ак Барс',       'Kazan',           'E',  8),
  ('AMU',   54, 'Amur',             'Амур',          'Khabarovsk',      'E',  2),
  ('SAL',   38, 'Salavat Yulaev',   'Салават Юлаев', 'Ufa',             'E',  4),
  ('ADM',  418, 'Admiral',          'Адмирал',       'Vladivostok',     'E',  2),
  ('AVG',   34, 'Avangard',         'Авангард',      'Omsk',            'E', 10),
  ('SIB',   29, 'Sibir',            'Сибирь',        'Novosibirsk',     'E',  3),
  ('TRK',   25, 'Traktor',          'Трактор',       'Chelyabinsk',     'E',  5)
on conflict (code) do update set
  khl_id = excluded.khl_id, name_en = excluded.name_en, name_ru = excluded.name_ru,
  city = excluded.city, conf = excluded.conf, price = excluded.price;

-- ---------------------------------------------------------------
-- Results written by the updater (GitHub Actions)
-- ---------------------------------------------------------------
create table if not exists public.standings (
  code       text primary key references public.clubs(code),
  gp         int not null default 0,   -- games played      (И)
  w          int not null default 0,   -- regulation wins   (В)
  otw        int not null default 0,   -- overtime wins     (ВО)
  sow        int not null default 0,   -- shootout wins     (ВБ)
  sol        int not null default 0,   -- shootout losses   (ПБ)
  otl        int not null default 0,   -- overtime losses   (ПО)
  otl_en     int not null default 0,   -- (ПП)
  l          int not null default 0,   -- regulation losses (П)
  goals      text not null default '0-0',
  updated_at timestamptz not null default now()
);

create table if not exists public.matches (
  id          int primary key,          -- khl.ru game id
  match_date  date not null,
  time_msk    text not null default '',
  kickoff     timestamptz,
  home        text not null references public.clubs(code),
  away        text not null references public.clubs(code),
  home_score  int,
  away_score  int,
  result_type text check (result_type in ('R','OT','SO')),  -- null = not played yet
  final       boolean not null default false,
  updated_at  timestamptz not null default now()
);
create index if not exists matches_home_idx on public.matches(home, kickoff);
create index if not exists matches_away_idx on public.matches(away, kickoff);

-- League settings. count_from: 'save' (points count after a team is saved) or 'season'.
create table if not exists public.settings (
  key   text primary key,
  value text not null
);
insert into public.settings (key, value) values ('count_from', 'save') on conflict (key) do nothing;
insert into public.settings (key, value) values ('budget', '20') on conflict (key) do nothing;

-- ---------------------------------------------------------------
-- Managers' teams
-- ---------------------------------------------------------------
create table if not exists public.squads (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  name       text not null check (char_length(name) between 1 and 40),
  teams      text[] not null,
  base       jsonb not null default '{}'::jsonb,  -- club points at the moment each club was bought
  banked     int not null default 0,              -- points kept from clubs that were sold
  created_at timestamptz not null default now(),
  saved_at   timestamptz not null default now()
);

-- League points for one club: 3 regulation win, 2 OT/SO win, 1 OT/SO loss, 0 regulation loss
create or replace function public.club_fp(p_code text) returns int
language sql stable as $$
  select coalesce((select 3*w + 2*(otw+sow) + sol + otl from public.standings where code = p_code), 0)
$$;

-- Save (create or change) the signed-in manager's team.
-- Checks the budget and works out the starting points on the server, so nobody can fake them.
create or replace function public.save_squad(p_name text, p_teams text[])
returns public.squads
language plpgsql security definer set search_path = public as $$
declare
  uid      uuid := auth.uid();
  prev     public.squads;
  has_prev boolean;
  n_valid  int;
  cost     int;
  budget   int := coalesce((select value::int from public.settings where key = 'budget'), 20);
  nbase    jsonb := '{}'::jsonb;
  nbanked  int := 0;
  t        text;
  result   public.squads;
begin
  if uid is null then raise exception 'Sign in to save a team'; end if;
  p_name := btrim(coalesce(p_name, ''));
  if char_length(p_name) < 1 or char_length(p_name) > 40 then
    raise exception 'Team name must be 1 to 40 characters';
  end if;
  if coalesce(array_length(p_teams, 1), 0) <> 4
     or (select count(distinct x) from unnest(p_teams) x) <> 4 then
    raise exception 'Pick exactly 4 different clubs';
  end if;
  select count(*), coalesce(sum(price), 0) into n_valid, cost from public.clubs where code = any(p_teams);
  if n_valid <> 4 then raise exception 'Unknown club in team'; end if;
  if cost > budget then raise exception 'Team costs % coins; the budget is %', cost, budget; end if;

  select * into prev from public.squads where user_id = uid;
  has_prev := found;

  if has_prev then
    nbanked := prev.banked;
    foreach t in array prev.teams loop
      if not (t = any(p_teams)) then
        nbanked := nbanked + greatest(0, public.club_fp(t) - coalesce((prev.base ->> t)::int, 0));
      end if;
    end loop;
  end if;

  foreach t in array p_teams loop
    if has_prev and t = any(prev.teams) then
      nbase := nbase || jsonb_build_object(t, coalesce((prev.base ->> t)::int, 0));
    else
      nbase := nbase || jsonb_build_object(t, public.club_fp(t));
    end if;
  end loop;

  insert into public.squads (user_id, name, teams, base, banked, created_at, saved_at)
  values (uid, p_name, p_teams, nbase, nbanked, now(), now())
  on conflict (user_id) do update
    set name = excluded.name, teams = excluded.teams, base = excluded.base,
        banked = excluded.banked, saved_at = now()
  returning * into result;
  return result;
end;
$$;

revoke all on function public.save_squad(text, text[]) from public, anon;
grant execute on function public.save_squad(text, text[]) to authenticated;

-- ---------------------------------------------------------------
-- Security rules (Row Level Security)
-- Everyone can read. Managers can only delete their own team;
-- saving goes through save_squad(). Results are written only by the updater (service key).
-- ---------------------------------------------------------------
alter table public.clubs     enable row level security;
alter table public.standings enable row level security;
alter table public.matches   enable row level security;
alter table public.settings  enable row level security;
alter table public.squads    enable row level security;

drop policy if exists "read clubs"     on public.clubs;
drop policy if exists "read standings" on public.standings;
drop policy if exists "read matches"   on public.matches;
drop policy if exists "read settings"  on public.settings;
drop policy if exists "read squads"    on public.squads;
drop policy if exists "delete own squad" on public.squads;

create policy "read clubs"     on public.clubs     for select using (true);
create policy "read standings" on public.standings for select using (true);
create policy "read matches"   on public.matches   for select using (true);
create policy "read settings"  on public.settings  for select using (true);
create policy "read squads"    on public.squads    for select using (true);
create policy "delete own squad" on public.squads  for delete to authenticated using (auth.uid() = user_id);

-- ---------------------------------------------------------------
-- Live updates in the browser
-- ---------------------------------------------------------------
do $$
declare tbl text;
begin
  foreach tbl in array array['squads','standings','matches','settings'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = tbl) then
      execute format('alter publication supabase_realtime add table public.%I', tbl);
    end if;
  end loop;
end $$;
