"""Task 12A repository-level checks for the Supabase schema migrations.

These tests are intentionally static: CI does not receive production database
credentials and must never contact or mutate the live project.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
FOUNDATION = MIGRATIONS / "20260903022752_task_12a_cloud_data_foundation.sql"
OWNER_FK = MIGRATIONS / "20260903022905_task_12a_cover_application_event_owner_fk.sql"


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8").lower()


def test_task12a_migrations_match_cloud_history() -> None:
    assert FOUNDATION.is_file()
    assert OWNER_FK.is_file()


def test_all_public_tables_enable_rls() -> None:
    sql = _sql(FOUNDATION)
    for table in (
        "profiles",
        "import_batches",
        "opportunities",
        "applications",
        "application_events",
    ):
        assert f"alter table public.{table} enable row level security" in sql


def test_per_user_policies_use_authenticated_uid() -> None:
    sql = _sql(FOUNDATION)
    assert "to authenticated" in sql
    assert "(select auth.uid()) = user_id" in sql
    assert "auth.role()" not in sql
    assert "user_metadata" not in sql


def test_admin_policies_use_app_metadata() -> None:
    sql = _sql(FOUNDATION)
    assert "'app_metadata'" in sql
    assert "'role'" in sql
    assert "'admin'" in sql


def test_application_event_owner_is_covered_by_composite_fk() -> None:
    sql = _sql(OWNER_FK)
    assert "unique (id, user_id)" in sql
    assert "foreign key (application_id, user_id)" in sql
    assert "references public.applications (id, user_id)" in sql
