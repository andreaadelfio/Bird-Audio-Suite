-- Bird Audio Suite: schema `birds` in the Supabase project "progetti".
--
-- The laptop (`bird_audio_cli.py sync`) inserts species, observations and clips and only
-- ever updates the "machine" columns; the review site (GitHub Pages) changes the review
-- columns. Both use the publishable key. Access is deliberately open to `anon`, like MyFinance:
-- whoever has the site URL can read (coordinates included) and write.
--
-- Safe to re-run: it never drops data.
-- After the first run: Project Settings -> API -> "Exposed schemas": add `birds`.

create schema if not exists birds;

-- ---------------------------------------------------------------- tables

create table if not exists birds.species (
  scientific_name text primary key,
  english_name    text not null default '',
  italian_name    text not null default '',
  german_name     text not null default '',
  first_seen_date date,
  last_seen_date  date,
  observed_days   integer not null default 0,
  total_count     integer not null default 0,
  max_confidence  real not null default 0,
  updated_at      timestamptz not null default now()
);

-- One observation per species per day, like a row of the daily iNaturalist CSV.
create table if not exists birds.observations (
  id                    bigint generated always as identity primary key,
  day                   date not null,
  scientific_name       text not null references birds.species (scientific_name) on update cascade,
  observed_at           timestamptz not null,
  place_name            text not null default '',
  latitude              double precision,
  longitude             double precision,
  location_source       text not null default '',
  positional_accuracy_m integer,
  max_confidence        real not null default 0,
  -- review: written by the site only
  status                    text not null default 'pending'
                            check (status in ('pending', 'confirmed', 'rejected', 'uploaded')),
  corrected_scientific_name text,
  notes                     text not null default '',
  reviewed_at               timestamptz,
  inat_observation_id       bigint,
  inat_uuid                 text,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now(),
  unique (day, scientific_name)
);

create table if not exists birds.clips (
  id              text primary key, -- clip file name, unique: "<Nome>_YYYYMMDD_HHMMSS_<conf>.wav"
  observation_id  bigint not null references birds.observations (id) on delete cascade,
  recorded_at     timestamptz not null,
  duration_s      real,
  confidence      real not null,
  audio_url       text not null,
  audio_public_id text,
  spectrogram_url text,
  -- review: written by the site only
  review          text not null default 'unreviewed'
                  check (review in ('unreviewed', 'correct', 'wrong')),
  created_at      timestamptz not null default now()
);

-- Clips deleted for good from the site ("Cestino"): `sync` reads this list and never
-- uploads them again. The WAV files on the laptop are not touched.
create table if not exists birds.deleted_clips (
  id         text primary key, -- same id as birds.clips.id (the clip file name)
  deleted_at timestamptz not null default now()
);

-- iNaturalist upload from the site: the sound id of each uploaded clip, so an interrupted
-- upload resumes without duplicates (the observation id is in observations.inat_observation_id)
alter table birds.clips add column if not exists inat_sound_id bigint;

create index if not exists clips_observation_idx on birds.clips (observation_id);
create index if not exists observations_status_day_idx on birds.observations (status, day desc);


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

drop trigger if exists species_touch on birds.species;
create trigger species_touch before update on birds.species
  for each row execute function birds.touch_updated_at();

-- ---------------------------------------------------------------- report views

-- security_invoker: the views apply the caller's RLS, they do not bypass it.
create or replace view birds.daily_summary with (security_invoker = on) as
select
  o.day,
  o.scientific_name,
  s.italian_name,
  s.english_name,
  o.status,
  o.max_confidence,
  count(c.id)                      as clips,
  coalesce(sum(c.duration_s), 0)   as seconds,
  min(c.recorded_at)               as first_heard,
  max(c.recorded_at)               as last_heard
from birds.observations o
join birds.species s using (scientific_name)
left join birds.clips c on c.observation_id = o.id
group by o.id, s.scientific_name;

create or replace view birds.hourly_activity with (security_invoker = on) as
select
  extract(hour from c.recorded_at at time zone 'Europe/Rome')::int as hour,
  o.scientific_name,
  count(*) as clips
from birds.clips c
join birds.observations o on o.id = c.observation_id
where o.status <> 'rejected'
group by 1, 2;

-- ---------------------------------------------------------------- access (open)

-- earlier drafts: Supabase users, then a review passphrase
drop policy if exists "allowed users read species" on birds.species;
drop policy if exists "allowed users read observations" on birds.observations;
drop policy if exists "allowed users review observations" on birds.observations;
drop policy if exists "allowed users read clips" on birds.clips;
drop policy if exists "allowed users review clips" on birds.clips;
drop policy if exists "public read species" on birds.species;
drop policy if exists "public read observations" on birds.observations;
drop policy if exists "public read clips" on birds.clips;
drop function if exists birds.is_allowed();
drop function if exists birds.review_observation(text, bigint, text, text, text);
drop function if exists birds.review_clip(text, text, text);
drop function if exists birds.check_passphrase(text);
drop function if exists birds.set_passphrase(text);
drop function if exists birds.passphrase_ok(text);
drop table if exists birds.allowed_users;
drop table if exists birds.settings;

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

-- ---------------------------------------------------------------- media (Supabase Storage)

-- Public bucket `bird-audio`: clip MP3s and spectrogram JPEGs uploaded by `sync`, open like
-- the tables. Skipped where Storage does not exist (local stand-in, `make web-dev`).
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
