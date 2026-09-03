-- CareerCopilot task 12A: cloud data foundation.
--
-- This migration intentionally contains schema only. Real workbooks, user data,
-- API keys, connection credentials, and service-role credentials must never be
-- committed to source control.

begin;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;

create or replace function private.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = pg_catalog.now();
  return new;
end;
$$;

revoke execute on function private.touch_updated_at()
  from public, anon, authenticated, service_role;

create table public.profiles (
  user_id uuid primary key
    constraint profiles_user_id_fkey
    references auth.users (id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint profiles_display_name_length
    check (
      display_name is null
      or char_length(display_name) between 1 and 100
    )
);

create table public.import_batches (
  id uuid primary key default gen_random_uuid(),
  created_by uuid not null default auth.uid()
    constraint import_batches_created_by_fkey
    references auth.users (id) on delete restrict,
  source_filename text not null,
  storage_path text,
  file_sha256 text,
  status text not null default 'pending',
  total_rows integer not null default 0,
  imported_rows integer not null default 0,
  duplicate_rows integer not null default 0,
  invalid_rows integer not null default 0,
  pending_rows integer not null default 0,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint import_batches_filename_nonempty
    check (char_length(btrim(source_filename)) between 1 and 255),
  constraint import_batches_storage_path_nonempty
    check (
      storage_path is null
      or char_length(btrim(storage_path)) between 1 and 1000
    ),
  constraint import_batches_sha256_format
    check (file_sha256 is null or file_sha256 ~ '^[0-9a-f]{64}$'),
  constraint import_batches_status_valid
    check (status in ('pending', 'processing', 'completed', 'failed')),
  constraint import_batches_counts_nonnegative
    check (
      total_rows >= 0
      and imported_rows >= 0
      and duplicate_rows >= 0
      and invalid_rows >= 0
      and pending_rows >= 0
    ),
  constraint import_batches_completed_at_consistent
    check (
      (status in ('completed', 'failed') and completed_at is not null)
      or (status in ('pending', 'processing') and completed_at is null)
    )
);

comment on column public.import_batches.file_sha256 is
  'Lowercase SHA-256 of the uploaded source file; never a credential.';

create table public.opportunities (
  id uuid primary key default gen_random_uuid(),
  record_type text not null,
  display_title text not null,
  job_title text,
  job_categories text,
  company_name text not null,
  industry text,
  recruitment_type text,
  target_cohort text,
  education_requirement text,
  location text,
  deadline text,
  announcement_title text,
  announcement_url text,
  application_url text,
  source_sheet text not null,
  source_row integer not null,
  import_batch_id uuid
    constraint opportunities_import_batch_id_fkey
    references public.import_batches (id) on delete set null,
  dedupe_key text not null unique,
  raw_data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint opportunities_record_type_valid
    check (record_type in ('campaign', 'job')),
  constraint opportunities_display_title_nonempty
    check (char_length(btrim(display_title)) between 1 and 500),
  constraint opportunities_company_name_nonempty
    check (char_length(btrim(company_name)) between 1 and 300),
  constraint opportunities_announcement_url_http
    check (announcement_url is null or announcement_url ~* '^https?://'),
  constraint opportunities_application_url_http
    check (application_url is null or application_url ~* '^https?://'),
  constraint opportunities_source_sheet_nonempty
    check (char_length(btrim(source_sheet)) between 1 and 200),
  constraint opportunities_source_row_positive
    check (source_row >= 1),
  constraint opportunities_dedupe_key_nonempty
    check (char_length(btrim(dedupe_key)) between 1 and 200),
  constraint opportunities_raw_data_object
    check (jsonb_typeof(raw_data) = 'object')
);

comment on table public.opportunities is
  'Global CareerCopilot opportunity catalogue; unknown records must never be inserted.';

create table public.applications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid()
    constraint applications_user_id_fkey
    references auth.users (id) on delete cascade,
  opportunity_id uuid
    constraint applications_opportunity_id_fkey
    references public.opportunities (id) on delete set null,
  company_name text not null,
  job_title text not null,
  application_url text,
  status text not null default 'discovered',
  current_stage text not null default 'saved',
  priority text not null default 'medium',
  notes text,
  applied_at timestamptz,
  next_action text,
  next_action_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint applications_company_name_nonempty
    check (char_length(btrim(company_name)) between 1 and 300),
  constraint applications_job_title_nonempty
    check (char_length(btrim(job_title)) between 1 and 500),
  constraint applications_application_url_http
    check (application_url is null or application_url ~* '^https?://'),
  constraint applications_status_valid
    check (
      status in (
        'discovered', 'shortlisted', 'opened', 'applying', 'applied',
        'assessment', 'interview', 'offer', 'rejected', 'withdrawn'
      )
    ),
  constraint applications_current_stage_nonempty
    check (char_length(btrim(current_stage)) between 1 and 120),
  constraint applications_priority_valid
    check (priority in ('high', 'medium', 'low'))
);

comment on table public.applications is
  'Per-user application tracker. current_stage is intentionally free-form for employer-specific workflows.';

create table public.application_events (
  id uuid primary key default gen_random_uuid(),
  application_id uuid not null
    constraint application_events_application_id_fkey
    references public.applications (id) on delete cascade,
  user_id uuid not null default auth.uid()
    constraint application_events_user_id_fkey
    references auth.users (id) on delete cascade,
  event_type text not null default 'stage_changed',
  from_stage text,
  to_stage text,
  note text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint application_events_type_valid
    check (
      event_type in (
        'created', 'stage_changed', 'note', 'assessment', 'interview',
        'offer', 'rejected', 'withdrawn', 'other'
      )
    ),
  constraint application_events_from_stage_length
    check (from_stage is null or char_length(from_stage) between 1 and 120),
  constraint application_events_to_stage_length
    check (to_stage is null or char_length(to_stage) between 1 and 120),
  constraint application_events_note_length
    check (note is null or char_length(note) <= 5000)
);

comment on table public.application_events is
  'Append-only per-user application timeline.';

create index import_batches_created_by_created_at_idx
  on public.import_batches (created_by, created_at desc);
create index opportunities_company_name_idx
  on public.opportunities (company_name);
create index opportunities_record_type_idx
  on public.opportunities (record_type);
create index opportunities_import_batch_id_idx
  on public.opportunities (import_batch_id);
create index applications_user_status_idx
  on public.applications (user_id, status);
create index applications_user_updated_at_idx
  on public.applications (user_id, updated_at desc);
create index applications_opportunity_id_idx
  on public.applications (opportunity_id);
create unique index applications_one_per_opportunity_per_user
  on public.applications (user_id, opportunity_id)
  where opportunity_id is not null;
create index application_events_application_id_idx
  on public.application_events (application_id);
create index application_events_user_application_time_idx
  on public.application_events (user_id, application_id, occurred_at desc);

create trigger profiles_touch_updated_at
before update on public.profiles
for each row execute function private.touch_updated_at();

create trigger opportunities_touch_updated_at
before update on public.opportunities
for each row execute function private.touch_updated_at();

create trigger applications_touch_updated_at
before update on public.applications
for each row execute function private.touch_updated_at();

alter table public.profiles enable row level security;
alter table public.import_batches enable row level security;
alter table public.opportunities enable row level security;
alter table public.applications enable row level security;
alter table public.application_events enable row level security;

revoke all on table public.profiles from anon, authenticated;
revoke all on table public.import_batches from anon, authenticated;
revoke all on table public.opportunities from anon, authenticated;
revoke all on table public.applications from anon, authenticated;
revoke all on table public.application_events from anon, authenticated;

grant select, insert, update on table public.profiles to authenticated;
grant select, insert, update, delete on table public.import_batches to authenticated;
grant select, insert, update, delete on table public.opportunities to authenticated;
grant select, insert, update, delete on table public.applications to authenticated;
grant select, insert on table public.application_events to authenticated;

grant all on table public.profiles to service_role;
grant all on table public.import_batches to service_role;
grant all on table public.opportunities to service_role;
grant all on table public.applications to service_role;
grant all on table public.application_events to service_role;

create policy profiles_select_own
on public.profiles for select
to authenticated
using ((select auth.uid()) = user_id);

create policy profiles_insert_own
on public.profiles for insert
to authenticated
with check ((select auth.uid()) = user_id);

create policy profiles_update_own
on public.profiles for update
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy import_batches_select_admin
on public.import_batches for select
to authenticated
using (
  coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy import_batches_insert_admin
on public.import_batches for insert
to authenticated
with check (
  (select auth.uid()) = created_by
  and coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy import_batches_update_admin
on public.import_batches for update
to authenticated
using (
  (select auth.uid()) = created_by
  and coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
)
with check (
  (select auth.uid()) = created_by
  and coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy import_batches_delete_admin
on public.import_batches for delete
to authenticated
using (
  (select auth.uid()) = created_by
  and coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy opportunities_select_authenticated
on public.opportunities for select
to authenticated
using (true);

create policy opportunities_insert_admin
on public.opportunities for insert
to authenticated
with check (
  coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy opportunities_update_admin
on public.opportunities for update
to authenticated
using (
  coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
)
with check (
  coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy opportunities_delete_admin
on public.opportunities for delete
to authenticated
using (
  coalesce((select auth.jwt()) -> 'app_metadata' ->> 'role', '') = 'admin'
);

create policy applications_select_own
on public.applications for select
to authenticated
using ((select auth.uid()) = user_id);

create policy applications_insert_own
on public.applications for insert
to authenticated
with check ((select auth.uid()) = user_id);

create policy applications_update_own
on public.applications for update
to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy applications_delete_own
on public.applications for delete
to authenticated
using ((select auth.uid()) = user_id);

create policy application_events_select_own
on public.application_events for select
to authenticated
using ((select auth.uid()) = user_id);

create policy application_events_insert_own
on public.application_events for insert
to authenticated
with check ((select auth.uid()) = user_id);

commit;
