"""任务 12B：Supabase 配置、访问层与申请记录测试。

测试只使用内存 fake client，不访问用户的 Supabase 项目，也不包含真实密钥、
公司、岗位或链接。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.supabase_service import (
    SupabaseAuthError,
    SupabaseConfig,
    SupabaseConfigurationError,
    SupabaseDataService,
    cloud_opportunity_payload,
    overlay_user_applications,
    sign_in,
)


class FakeBuilder:
    def __init__(self, data, calls, name):
        self.data = data
        self.calls = calls
        self.name = name
        self.range_start = None
        self.range_end = None

    def select(self, *columns, **kwargs):
        self.calls.append((self.name, "select", columns, kwargs))
        return self

    def insert(self, payload, **kwargs):
        self.calls.append((self.name, "insert", payload, kwargs))
        return self

    def upsert(self, payload, **kwargs):
        self.calls.append((self.name, "upsert", payload, kwargs))
        return self

    def update(self, payload, **kwargs):
        self.calls.append((self.name, "update", payload, kwargs))
        return self

    def delete(self, **kwargs):
        self.calls.append((self.name, "delete", kwargs))
        return self

    def eq(self, *args, **kwargs):
        self.calls.append((self.name, "eq", args, kwargs))
        return self

    def order(self, *args, **kwargs):
        self.calls.append((self.name, "order", args, kwargs))
        return self

    def range(self, start, end):
        self.calls.append((self.name, "range", (start, end), {}))
        self.range_start = start
        self.range_end = end
        return self

    def execute(self):
        data = self.data
        if self.range_start is not None and self.range_end is not None:
            data = data[self.range_start : self.range_end + 1]
        return SimpleNamespace(data=data)


class FakeClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def table(self, name):
        return FakeBuilder(self.responses.get(name, []), self.calls, name)


def _auth_response():
    user = SimpleNamespace(id="user-a", email="a@example.com")
    session = SimpleNamespace(
        access_token="access",
        refresh_token="refresh",
        expires_at=123,
        user=user,
    )
    return SimpleNamespace(session=session, user=user)


class FakeAuth:
    def sign_in_with_password(self, credentials):
        self.credentials = credentials
        return _auth_response()


def test_config_reads_nested_secrets_and_rejects_partial_config():
    config = SupabaseConfig.from_sources(
        environ={},
        secrets={
            "supabase": {
                "SUPABASE_URL": "https://example.supabase.co/",
                "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_example",
            }
        },
    )
    assert config is not None
    assert config.url == "https://example.supabase.co"
    assert config.has_service_role is False
    with pytest.raises(SupabaseConfigurationError):
        SupabaseConfig.from_sources(
            environ={}, secrets={"SUPABASE_URL": "https://example.supabase.co"}
        )


def test_config_does_not_accept_service_role_as_publishable():
    with pytest.raises(SupabaseConfigurationError):
        SupabaseConfig.from_sources(
            environ={
                "SUPABASE_URL": "https://example.supabase.co",
                # JWT payload is role=service_role; this is intentionally fake.
                "SUPABASE_PUBLISHABLE_KEY": "eyJhbGciOiJub25lIn0.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.x",
            }
        )
    with pytest.raises(SupabaseConfigurationError):
        SupabaseConfig.from_sources(
            environ={
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_PUBLISHABLE_KEY": "sb_secret_example",
            }
        )


def test_config_returns_none_when_unconfigured():
    assert SupabaseConfig.from_sources(environ={}, secrets={}) is None


def test_sign_in_returns_minimal_session_and_validates_input():
    client = SimpleNamespace(auth=FakeAuth())
    session = sign_in(client, "a@example.com", "password123")
    assert session.user.id == "user-a"
    assert session.as_dict()["access_token"] == "access"
    assert client.auth.credentials["email"] == "a@example.com"
    with pytest.raises(SupabaseAuthError):
        sign_in(client, "not-an-email", "password123")


def test_cloud_opportunity_payload_is_allowlisted_and_sanitizes_url():
    payload = cloud_opportunity_payload(
        {
            "record_type": "job",
            "display_title": "示例岗位",
            "company_name": "示例科技A",
            "job_title": "测试工程师",
            "source_sheet": "中国大陆",
            "source_row": 2,
            "dedupe_key": "job_example",
            "application_url": "javascript:bad",
            "raw_data": {"A": "示例"},
            "status": "offer",
            "user_id": "must-not-pass",
        }
    )
    assert payload is not None
    assert payload["application_url"] is None
    assert "status" not in payload
    assert "user_id" not in payload
    assert payload["raw_data"] == {"A": "示例"}


def test_unknown_is_never_converted_to_cloud_payload():
    assert cloud_opportunity_payload({"record_type": "unknown"}) is None


def test_overlay_supports_uuid_opportunity_ids_and_keeps_global_rows_unchanged():
    opportunities = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "record_type": "job",
            "company_name": "示例科技A",
            "display_title": "测试工程师",
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "record_type": "campaign",
            "company_name": "示例科技B",
            "display_title": "校园招聘",
        },
    ]
    applications = [
        {
            "id": "app-1",
            "opportunity_id": opportunities[0]["id"],
            "status": "interview",
            "priority": "high",
            "current_stage": "技术一面",
        }
    ]
    rows = overlay_user_applications(opportunities, applications)
    assert rows[0]["application_id"] == "app-1"
    assert rows[0]["status"] == "interview"
    assert rows[0]["priority"] == "high"
    assert rows[1]["status"] == "discovered"
    assert "status" not in opportunities[0]


def test_service_lists_cloud_opportunities_and_user_applications():
    client = FakeClient(
        {
            "opportunities": [{"id": "opp-1", "company_name": "示例科技A"}],
            "applications": [{"id": "app-1", "status": "applied"}],
        }
    )
    service = SupabaseDataService(client)
    assert service.list_opportunities()[0]["id"] == "opp-1"
    assert service.list_applications()[0]["status"] == "applied"
    assert any(call[1] == "order" for call in client.calls)


def test_service_paginates_past_supabase_1000_row_response_limit():
    opportunities = [
        {
            "id": f"opp-{index:04d}",
            "company_name": "示例科技",
            "dedupe_key": f"key-{index:04d}",
        }
        for index in range(2505)
    ]
    client = FakeClient({"opportunities": opportunities})
    service = SupabaseDataService(client)

    rows = service.list_opportunities()

    assert len(rows) == 2505
    assert rows[-1]["id"] == "opp-2504"
    opportunity_selects = [
        call
        for call in client.calls
        if call[0] == "opportunities" and call[1] == "select"
    ]
    assert opportunity_selects
    assert all("raw_data" not in call[2][0] for call in opportunity_selects)
    ranges = [
        call[2]
        for call in client.calls
        if call[0] == "opportunities" and call[1] == "range"
    ]
    assert ranges == [(0, 999), (1000, 1999), (2000, 2999)]


def test_service_reads_all_dedupe_keys_across_pages():
    opportunities = [
        {"dedupe_key": f"key-{index:04d}"}
        for index in range(2101)
    ]
    service = SupabaseDataService(FakeClient({"opportunities": opportunities}))

    keys = service.list_dedupe_keys()

    assert len(keys) == 2101
    assert "key-2100" in keys


def test_create_application_only_sends_business_fields_and_adds_event():
    client = FakeClient(
        {
            "applications": [{"id": "app-1", "status": "discovered"}],
            "application_events": [{"id": "event-1"}],
        }
    )
    service = SupabaseDataService(client)
    row = service.create_application(
        company_name="示例科技A",
        job_title="测试工程师",
        application_url="https://example.com/apply",
        current_stage="网申",
        priority="high",
    )
    assert row["id"] == "app-1"
    app_insert = next(call for call in client.calls if call[1] == "insert" and call[0] == "applications")
    assert "user_id" not in app_insert[2]
    assert app_insert[2]["current_stage"] == "网申"
    assert any(call[0] == "application_events" for call in client.calls)


def test_mark_opened_and_confirm_applied_preserve_high_stage():
    client = FakeClient(
        {
            "applications": [
                {
                    "id": "app-1",
                    "status": "interview",
                    "application_url": "https://example.com/apply",
                }
            ]
        }
    )
    service = SupabaseDataService(client)
    opened = service.mark_application_opened("app-1")
    applied = service.confirm_application_applied("app-1")
    assert opened["action"] == "opened_without_status_change"
    assert applied["action"] == "no_change"
