-- Tokisclone short-drama catalogue schema for PostgreSQL/Supabase migration.
-- This mirrors library_db.py while keeping media storage provider-neutral.

create extension if not exists pgcrypto;

create table if not exists dramas (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  slug text not null unique,
  description text,
  poster_url text,
  language text,
  genres jsonb not null default '[]'::jsonb,
  episode_count integer,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists sources (
  id uuid primary key default gen_random_uuid(),
  drama_id uuid not null references dramas(id) on delete cascade,
  platform text not null,
  external_id text,
  source_url text,
  status text not null default 'active',
  last_checked timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists sources_platform_external_uidx
  on sources (lower(platform), external_id)
  where external_id is not null and external_id <> '';

create unique index if not exists sources_platform_url_uidx
  on sources (lower(platform), source_url)
  where source_url is not null and source_url <> '';

create index if not exists sources_drama_idx on sources(drama_id);

create table if not exists episodes (
  id uuid primary key default gen_random_uuid(),
  drama_id uuid not null references dramas(id) on delete cascade,
  episode_number integer not null check (episode_number > 0),
  title text,
  duration_seconds double precision,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (drama_id, episode_number)
);

create table if not exists episode_sources (
  id uuid primary key default gen_random_uuid(),
  episode_id uuid not null references episodes(id) on delete cascade,
  source_id uuid not null references sources(id) on delete cascade,
  playback_url text,
  status text not null default 'available',
  last_checked timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (episode_id, source_id)
);

create table if not exists files (
  id uuid primary key default gen_random_uuid(),
  episode_id uuid not null references episodes(id) on delete cascade,
  storage_provider text not null,
  provider_file_id text not null,
  provider_path text,
  sha256 text,
  bytes bigint,
  mime_type text,
  verified boolean not null default false,
  verification jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (storage_provider, provider_file_id)
);

create index if not exists files_episode_idx on files(episode_id);
create index if not exists files_sha256_idx on files(sha256);

create table if not exists ingestion_jobs (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  drama_id uuid references dramas(id) on delete set null,
  source_id uuid references sources(id) on delete set null,
  episode_id uuid references episodes(id) on delete set null,
  state text not null default 'queued',
  error text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ingestion_jobs_state_idx on ingestion_jobs(state, created_at);
