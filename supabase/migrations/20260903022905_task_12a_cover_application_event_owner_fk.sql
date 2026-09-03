-- Bind every timeline event to an application owned by the same user.
-- The first migration's single-column FK prevents orphaned events but does not
-- prevent a user from referencing another user's application ID.

begin;

alter table public.applications
  add constraint applications_id_user_unique unique (id, user_id);

alter table public.application_events
  drop constraint application_events_application_id_fkey,
  add constraint application_events_application_owner_fk
    foreign key (application_id, user_id)
    references public.applications (id, user_id)
    on delete cascade;

drop index public.application_events_application_id_idx;

create index application_events_application_owner_idx
  on public.application_events (application_id, user_id);

commit;
