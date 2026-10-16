-- Bird Audio Suite: schema `birds` in the Supabase project "progetti".
--
-- Every piece of information lives in one place only:
--   * storage (bucket `bird-audio`): the files, <media_path>.mp3 and <media_path>-spectrogram.jpg;
--   * birds.clips: one row per detection, with its own time, confidence, place and the
--     media_path of its files (URLs are built from it, never stored);
--   * birds.observations (a visit: clips of a species at most 10 minutes apart, the same bird
--     still there): only what you decide while reviewing;
--   * birds.species: the names, and the codes that link the species to eBird and iNaturalist.
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
  german_name     text not null default '',
  ebird_code      text,    -- eBird / Macaulay Library species code (e.g. eurnut2), from the eBird taxonomy
  inat_taxon_id   bigint   -- iNaturalist taxon, for direct links (they open the app on Android)
);

-- a visit (clips of one species at most 10 minutes apart): the unit you confirm, reject or upload
-- to iNaturalist; `day` is the day of its first clip
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
  updated_at                timestamptz not null default now()
);

-- one detection
create table if not exists birds.clips (
  id             text primary key, -- "<Nome>_YYYYMMDD_HHMMSS_<conf>.wav", as written by the laptop
  observation_id bigint not null references birds.observations (id) on delete cascade,
  recorded_at    timestamptz not null,
  confidence     real not null,    -- BirdNET on the saved clip (analysis.py): what clip_conf shows
  live_confidence real,            -- what the live scored while listening (other chunks, older pipelines)
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
  candidates     jsonb,            -- BirdNET's three most likely species in the clip: [[scientific, english, confidence], ...]
  linked_from    text,             -- added on the site to another observation too: the clip it copies
                                   -- (same files: they go only when no clip uses them any more)
  manual         boolean not null default false, -- put in its observation by hand on the site (linked
                                   -- or moved): no BirdNET score for this species, confidence 0
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
create index if not exists observations_species_day_idx on birds.observations (scientific_name, day);

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
alter table birds.clips add column if not exists candidates jsonb;
alter table birds.clips add column if not exists live_confidence real;
alter table birds.clips add column if not exists linked_from text;
alter table birds.clips add column if not exists manual boolean not null default false;
create index if not exists clips_media_path_idx on birds.clips (media_path);
-- the live's score, kept before the analysis of the saved clips replaces confidence
update birds.clips set live_confidence = confidence where live_confidence is null;
alter table birds.species add column if not exists ebird_code text;
alter table birds.species add column if not exists inat_taxon_id bigint;
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

-- one observation per species and day until October 2026: now one per visit
do $$
declare
  c record;
begin
  for c in select conname from pg_constraint
           where conrelid = 'birds.observations'::regclass and contype = 'u' loop
    execute format('alter table birds.observations drop constraint %I', c.conname);
  end loop;
end
$$;

-- split the old day-long observations into visits: a gap of more than 10 minutes between two
-- clips starts a new observation, with the same status and correction (you decided for the whole
-- day). Those already on iNaturalist stay whole, so their link stays right. Safe to re-run:
-- afterwards no observation has such a gap.
do $$
declare
  part record;
  new_id bigint;
begin
  for part in
    with ordered as (
      select c.id, c.observation_id, c.recorded_at,
             case when c.recorded_at - lag(c.recorded_at) over w > interval '10 minutes' then 1 else 0 end as gap
      from birds.clips c
      window w as (partition by c.observation_id order by c.recorded_at)
    ),
    visits as (
      select id, observation_id, recorded_at,
             sum(gap) over (partition by observation_id order by recorded_at) as visit
      from ordered
    )
    select v.observation_id, v.visit, array_agg(v.id) as clip_ids, min(v.recorded_at) as first_heard
    from visits v
    join birds.observations o on o.id = v.observation_id
    where v.visit > 0 and o.status <> 'uploaded'
    group by v.observation_id, v.visit
  loop
    insert into birds.observations (day, scientific_name, status, corrected_scientific_name, notes, reviewed_at)
    select (part.first_heard at time zone 'Europe/Rome')::date, o.scientific_name, o.status,
           o.corrected_scientific_name, o.notes, o.reviewed_at
    from birds.observations o where o.id = part.observation_id
    returning id into new_id;
    update birds.clips set observation_id = new_id where id = any(part.clip_ids);
  end loop;
end
$$;

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

-- a corrected species is what the statistics count (views below): it gets its row in birds.species
-- (names and links filled in within the hour by the PC's uploader), or takes the spelling of the
-- species already there ("turdus merula " -> "Turdus merula")
create or replace function birds.correction_species() returns trigger
language plpgsql as $$
declare
  known text;
begin
  new.corrected_scientific_name := nullif(btrim(new.corrected_scientific_name), '');
  if new.corrected_scientific_name is null then
    return new;
  end if;
  select scientific_name into known from birds.species
   where lower(scientific_name) = lower(new.corrected_scientific_name) limit 1;
  if known is null then
    insert into birds.species (scientific_name) values (new.corrected_scientific_name) on conflict do nothing;
  else
    new.corrected_scientific_name := known;
  end if;
  return new;
end;
$$;

drop trigger if exists observations_correction on birds.observations;
create trigger observations_correction before insert or update of corrected_scientific_name on birds.observations
  for each row execute function birds.correction_species();

-- corrections made before this trigger: their species too, in the spelling already there
insert into birds.species (scientific_name)
select distinct on (lower(btrim(o.corrected_scientific_name))) btrim(o.corrected_scientific_name)
from birds.observations o
where nullif(btrim(o.corrected_scientific_name), '') is not null
  and not exists (select 1 from birds.species s where lower(s.scientific_name) = lower(btrim(o.corrected_scientific_name)))
on conflict do nothing;
update birds.observations o set corrected_scientific_name = s.scientific_name
from birds.species s
where lower(s.scientific_name) = lower(btrim(o.corrected_scientific_name))
  and o.corrected_scientific_name is distinct from s.scientific_name;
update birds.observations set corrected_scientific_name = null
where corrected_scientific_name is not null and btrim(corrected_scientific_name) = '';

-- ---------------------------------------------------------------- views (everything computed)

-- Every statistic is about the species you settled on: the correction (pencil) when there is one,
-- otherwise BirdNET's. BirdNET's confidence counts only where it is about that species: not on a
-- corrected observation (it scored another species). Clips placed by hand have confidence 0.
-- Rows are never deleted for statistics' sake: a week after the iNaturalist upload only the files
-- leave Storage, clips and observations stay.

-- per species, over the clips you kept (rejected observations and wrong clips excluded);
-- the daily average uses the whole monitoring period, so one busy day is not "common"
create view birds.species_stats with (security_invoker = on) as
with heard as (
  select coalesce(o.corrected_scientific_name, o.scientific_name) as scientific_name, o.day,
         case when coalesce(o.corrected_scientific_name, o.scientific_name) = o.scientific_name
              then c.confidence end as confidence
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
  s.ebird_code,
  s.inat_taxon_id,
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

-- per observation (a visit): what the report and the live table show
create view birds.daily_summary with (security_invoker = on) as
select
  o.id                             as observation_id,
  o.day,
  coalesce(o.corrected_scientific_name, o.scientific_name) as scientific_name,
  o.scientific_name                as birdnet_scientific_name,
  s.italian_name,
  s.english_name,
  s.german_name,
  o.status,
  max(case when coalesce(o.corrected_scientific_name, o.scientific_name) = o.scientific_name
           then c.confidence end) as max_confidence,
  count(c.id)                      as clips,
  coalesce(sum(c.duration_s), 0)   as seconds,
  min(c.recorded_at)               as first_heard,
  max(c.recorded_at)               as last_heard,
  st.rarity,
  st.daily_average
from birds.observations o
-- left joins: an observation is never left out of a report, even with a species row missing
left join birds.species s on s.scientific_name = coalesce(o.corrected_scientific_name, o.scientific_name)
left join birds.species_stats st on st.scientific_name = s.scientific_name
left join birds.clips c on c.observation_id = o.id
group by o.id, s.scientific_name, st.rarity, st.daily_average;

create view birds.hourly_activity with (security_invoker = on) as
select
  extract(hour from c.recorded_at at time zone 'Europe/Rome')::int as hour,
  coalesce(o.corrected_scientific_name, o.scientific_name) as scientific_name,
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
