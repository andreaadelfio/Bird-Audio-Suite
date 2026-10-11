-- Bird Audio Suite: schema `birds` in the Supabase project "progetti".
--
-- Every piece of information lives in one place only:
--   * storage (bucket `bird-audio`): the files, <media_path>.mp3 and <media_path>-spectrogram.jpg;
--   * birds.clips: one row per detection, with its own time, confidence, place and the
--     media_path of its files (URLs are built from it, never stored);
--   * birds.observations (species x day): only what you decide while reviewing;
--   * birds.species: only the names.
-- Everything computed (max confidence of the day, first/last heard, counts, rarity) comes
-- from the views at the bottom, never from stored copies.
--
-- The laptop (`bird_audio_cli.py`, live service and `sync`) inserts species, observations and
-- clips; the review site changes the review columns. Both use the publishable key: access is
-- deliberately open to `anon`, like MyFinance.
--
-- Safe to re-run, also on the previous version: it moves the old data to its new place
-- before dropping the duplicated columns, and aborts if anything would be lost.
-- First run: Project Settings -> API -> "Exposed schemas": add `birds`.

begin;

create schema if not exists birds;

-- ---------------------------------------------------------------- tables

create table if not exists birds.species (
  scientific_name text primary key,
  english_name    text not null default '',
  italian_name    text not null default '',
  german_name     text not null default ''
);

-- species x day: the unit you confirm, reject or upload to iNaturalist
create table if not exists birds.observations (
  id                        bigint generated always as identity primary key,
  day                       date not null,
  scientific_name           text not null references birds.species (scientific_name) on update cascade,
  status                    text not null default 'pending'
                            check (status in ('pending', 'confirmed', 'rejected', 'uploaded')),
  corrected_scientific_name text,
  notes                     text not null default '',
  reviewed_at               timestamptz,
  inat_observation_id       bigint,
  inat_uuid                 text,
  uploaded_at               timestamptz,  -- on iNaturalist since: a week later its files leave Storage
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now(),
  unique (day, scientific_name)
);

-- one detection
create table if not exists birds.clips (
  id             text primary key, -- "<Nome>_YYYYMMDD_HHMMSS_<conf>.wav", as written by the laptop
  observation_id bigint not null references birds.observations (id) on delete cascade,
  recorded_at    timestamptz not null,
  confidence     real not null,
  duration_s     real,
  place_name     text not null default '',
  latitude       double precision,
  longitude      double precision,
  media_path     text,             -- in the bucket: <media_path>.mp3, <media_path>-spectrogram.jpg
  segments       jsonb,            -- where BirdNET heard the species: [[start_s, end_s, confidence], ...]
  trim_start_s   real,             -- kept part of the clip (set on the site, the file is untouched)
  trim_end_s     real,
  review         text not null default 'unreviewed'
                 check (review in ('unreviewed', 'correct', 'wrong')),
  inat_sound_id  bigint,           -- set once uploaded to iNaturalist, so uploads resume
  media_deleted_at timestamptz,    -- files removed from Storage (a week after the iNaturalist upload)
  created_at     timestamptz not null default now()
);

-- clips deleted for good from the site's Cestino: the laptop never uploads them again
create table if not exists birds.deleted_clips (
  id         text primary key,
  deleted_at timestamptz not null default now()
);

create index if not exists clips_observation_idx on birds.clips (observation_id);
create index if not exists clips_recorded_idx on birds.clips (recorded_at desc);
create index if not exists observations_status_day_idx on birds.observations (status, day desc);

-- ---------------------------------------------------------------- from the previous version

-- the views depend on the columns that are about to change
drop view if exists birds.daily_summary;
drop view if exists birds.hourly_activity;
drop view if exists birds.species_stats;

alter table birds.clips add column if not exists place_name text not null default '';
alter table birds.clips add column if not exists latitude double precision;
alter table birds.clips add column if not exists longitude double precision;
alter table birds.clips add column if not exists media_path text;
alter table birds.clips add column if not exists inat_sound_id bigint;
alter table birds.clips add column if not exists segments jsonb;
alter table birds.clips add column if not exists trim_start_s real;
alter table birds.clips add column if not exists trim_end_s real;
alter table birds.clips add column if not exists media_deleted_at timestamptz;
alter table birds.observations add column if not exists uploaded_at timestamptz;
-- already uploaded before uploaded_at existed: the last change is when it happened
update birds.observations set uploaded_at = coalesce(reviewed_at, updated_at)
where status = 'uploaded' and uploaded_at is null;

do $$
begin
  -- the place was stored once per observation: every clip of it gets it
  if exists (select 1 from information_schema.columns
             where table_schema = 'birds' and table_name = 'observations' and column_name = 'place_name') then
    execute $sql$
      update birds.clips c
      set place_name = o.place_name, latitude = o.latitude, longitude = o.longitude
      from birds.observations o
      where o.id = c.observation_id and c.latitude is null and c.place_name = ''
    $sql$;
  end if;
  -- full URLs were stored: keep only the path inside the bucket
  if exists (select 1 from information_schema.columns
             where table_schema = 'birds' and table_name = 'clips' and column_name = 'audio_url') then
    execute $sql$
      update birds.clips
      set media_path = substring(audio_url from '/object/public/bird-audio/(.*)\.mp3$')
      where media_path is null
    $sql$;
  end if;
  if exists (select 1 from birds.clips where media_path is null) then
    raise exception 'Ci sono clip senza percorso nel bucket: migrazione annullata, nessun dato toccato';
  end if;
end
$$;

alter table birds.clips alter column media_path set not null;

-- the duplicated columns: their content now comes from birds.clips and the views
alter table birds.observations
  drop column if exists max_confidence,
  drop column if exists observed_at,
  drop column if exists place_name,
  drop column if exists latitude,
  drop column if exists longitude,
  drop column if exists location_source,
  drop column if exists positional_accuracy_m;
alter table birds.clips
  drop column if exists audio_url,
  drop column if exists audio_public_id,
  drop column if exists spectrogram_url;
drop trigger if exists species_touch on birds.species;
alter table birds.species
  drop column if exists first_seen_date,
  drop column if exists last_seen_date,
  drop column if exists observed_days,
  drop column if exists total_count,
  drop column if exists max_confidence,
  drop column if exists updated_at;
drop table if exists birds.allowed_users;
drop table if exists birds.settings;

-- ---------------------------------------------------------------- triggers

create or replace function birds.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists observations_touch on birds.observations;
create trigger observations_touch before update on birds.observations
  for each row execute function birds.touch_updated_at();

-- ---------------------------------------------------------------- views (everything computed)

-- per species, over the clips you kept (rejected observations and wrong clips excluded);
-- the daily average uses the whole monitoring period, so one busy day is not "common"
create view birds.species_stats with (security_invoker = on) as
with heard as (
  select o.scientific_name, o.day, c.confidence
  from birds.clips c
  join birds.observations o on o.id = c.observation_id
  where o.status <> 'rejected' and c.review <> 'wrong'
),
period as (
  select greatest(max(day) - min(day) + 1, 1) as days from heard
)
select
  s.scientific_name,
  s.english_name,
  s.italian_name,
  s.german_name,
  min(h.day)                                                   as first_seen_date,
  max(h.day)                                                   as last_seen_date,
  count(distinct h.day)                                        as observed_days,
  count(h.day)                                                 as total_count,
  coalesce(max(h.confidence), 0)                               as max_confidence,
  round(count(h.day)::numeric / (select days from period), 3)  as daily_average,
  case
    when count(h.day) = 0 then 'unseen'
    when count(h.day) = 1 then 'unique'
    when count(h.day)::numeric / (select days from period) < 0.25 then 'rare'
    when count(h.day)::numeric / (select days from period) < 1 then 'uncommon'
    else 'common'
  end                                                          as rarity
from birds.species s
left join heard h using (scientific_name)
group by s.scientific_name;

-- per observation (species x day): what the report and the live table show
create view birds.daily_summary with (security_invoker = on) as
select
  o.id                             as observation_id,
  o.day,
  o.scientific_name,
  s.italian_name,
  s.english_name,
  s.german_name,
  o.status,
  max(c.confidence)                as max_confidence,
  count(c.id)                      as clips,
  coalesce(sum(c.duration_s), 0)   as seconds,
  min(c.recorded_at)               as first_heard,
  max(c.recorded_at)               as last_heard,
  st.rarity,
  st.daily_average
from birds.observations o
join birds.species s using (scientific_name)
join birds.species_stats st using (scientific_name)
left join birds.clips c on c.observation_id = o.id
group by o.id, s.scientific_name, st.rarity, st.daily_average;

create view birds.hourly_activity with (security_invoker = on) as
select
  extract(hour from c.recorded_at at time zone 'Europe/Rome')::int as hour,
  o.scientific_name,
  count(*) as clips
from birds.clips c
join birds.observations o on o.id = c.observation_id
where o.status <> 'rejected'
group by 1, 2;

-- ---------------------------------------------------------------- access (open)

do $$
declare
  t text;
begin
  foreach t in array array['species', 'observations', 'clips', 'deleted_clips'] loop
    execute format('alter table birds.%I enable row level security', t);
    execute format('drop policy if exists "accesso pubblico" on birds.%I', t);
    execute format(
      'create policy "accesso pubblico" on birds.%I for all to anon, authenticated using (true) with check (true)',
      t
    );
  end loop;
end
$$;

grant usage on schema birds to anon, authenticated, service_role;
grant select, insert, update, delete on all tables in schema birds to anon, authenticated;
grant all on all tables in schema birds to service_role;
grant usage, select on all sequences in schema birds to anon, authenticated, service_role;
grant select on birds.species_stats, birds.daily_summary, birds.hourly_activity to anon, authenticated, service_role;

-- ---------------------------------------------------------------- media (Supabase Storage)

-- Public bucket `bird-audio`, open like the tables. Skipped where Storage does not exist
-- (the local stand-in of `make web-dev`).
do $$
begin
  if to_regclass('storage.buckets') is null then
    return;
  end if;
  insert into storage.buckets (id, name, public)
  values ('bird-audio', 'bird-audio', true)
  on conflict (id) do update set public = true;

  drop policy if exists "bird-audio aperto" on storage.objects;
  create policy "bird-audio aperto" on storage.objects
    for all to anon, authenticated
    using (bucket_id = 'bird-audio')
    with check (bucket_id = 'bird-audio');
end
$$;

commit;

-- the API sees the new columns and views at once
notify pgrst, 'reload schema';
