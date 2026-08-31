"""任务 8 状态流转服务测试（mark_as_opened / confirm_applied）。

覆盖任务 8 指令全部必测项：
- job 打开 application_url；
- campaign 优先 application_url、缺失时回退 announcement_url；
- 无链接不打开、不改状态；
- discovered/shortlisted → opened；
- 高级状态不会被点击链接降级；
- 点击链接绝不变为 applied；
- "确认已投递"才会变为 applied 并设置 applied_at；
- 终态不被确认按钮降级；
- UI 按钮与服务层调用边界；
- 浏览/筛选/分页不写数据库；
- 不读取 data/private。

测试只使用 :memory: 或 tmp_path 和 example.com，不访问网络、不真实
打开浏览器，用 monkeypatch/mock 验证浏览器打开行为。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from database.db_handler import get_connection, get_preview_connection, init_db
from services.opportunity_service import (
    confirm_applied,
    fetch_all_opportunities,
    mark_as_opened,
    set_priority,
    update_status,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn():
    """tmp_path 下的临时 SQLite 数据库（绝不落仓库目录）。"""
    conn = get_connection(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def _insert_opp(
    conn: sqlite3.Connection,
    record_type: str = "job",
    display_title: str = "示例岗位",
    company_name: str = "示例科技A",
    priority: str = "low",
    status: str = "discovered",
    application_url: str = "https://example.com/apply",
    announcement_url: str = "",
    source_row: int = 2,
    dedupe_key: str | None = None,
    **extra: Any,
) -> int:
    """插入一条虚构机会，返回 id。"""
    fields: dict[str, Any] = {
        "record_type": record_type,
        "display_title": display_title,
        "company_name": company_name,
        "priority": priority,
        "status": status,
        "application_url": application_url,
        "announcement_url": announcement_url,
        "source_sheet": "中国大陆",
        "source_row": source_row,
        "dedupe_key": dedupe_key
        or f"k_{record_type}_{company_name}_{source_row}",
        "raw_data": "{}",
    }
    fields.update(extra)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(
        f"INSERT INTO opportunities ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _get_status(conn: sqlite3.Connection, opp_id: int) -> str:
    return str(
        conn.execute(
            "SELECT status FROM opportunities WHERE id = ?", (opp_id,)
        ).fetchone()["status"]
    )


def _get_opened_at(conn: sqlite3.Connection, opp_id: int) -> str | None:
    return conn.execute(
        "SELECT opened_at FROM opportunities WHERE id = ?", (opp_id,)
    ).fetchone()["opened_at"]


def _get_applied_at(conn: sqlite3.Connection, opp_id: int) -> str | None:
    return conn.execute(
        "SELECT applied_at FROM opportunities WHERE id = ?", (opp_id,)
    ).fetchone()["applied_at"]


# ---------------------------------------------------------------------------
# mark_as_opened：URL 选择
# ---------------------------------------------------------------------------


def test_job_opens_application_url(db_conn):
    """job：打开 application_url。"""
    opp_id = _insert_opp(db_conn, record_type="job", application_url="https://example.com/apply/1")
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "opened"
    assert result["url"] == "https://example.com/apply/1"
    assert _get_status(db_conn, opp_id) == "opened"


def test_campaign_prefers_application_url(db_conn):
    """campaign：优先 application_url。"""
    opp_id = _insert_opp(
        db_conn, record_type="campaign",
        application_url="https://example.com/apply/c",
        announcement_url="https://example.com/ann/c",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "opened"
    assert result["url"] == "https://example.com/apply/c"


def test_campaign_falls_back_to_announcement_url(db_conn):
    """campaign：application_url 缺失时回退 announcement_url。"""
    opp_id = _insert_opp(
        db_conn, record_type="campaign",
        application_url="",
        announcement_url="https://example.com/ann/c",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "opened"
    assert result["url"] == "https://example.com/ann/c"


def test_no_link_does_not_open_or_change_status(db_conn):
    """无链接不打开、不改状态。"""
    opp_id = _insert_opp(
        db_conn, record_type="job",
        application_url="",
        announcement_url="",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "no_link"
    assert result["url"] is None
    assert _get_status(db_conn, opp_id) == "discovered"


def test_invalid_url_does_not_open(db_conn):
    """非 http/https URL 不打开。"""
    opp_id = _insert_opp(
        db_conn, record_type="job",
        application_url="见公告",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "no_link"
    assert result["url"] is None
    assert _get_status(db_conn, opp_id) == "discovered"


# ---------------------------------------------------------------------------
# mark_as_opened：状态流转
# ---------------------------------------------------------------------------


def test_discovered_to_opened(db_conn):
    """discovered → opened。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "opened"
    assert _get_status(db_conn, opp_id) == "opened"
    # 首次写入 opened_at
    assert _get_opened_at(db_conn, opp_id) is not None


def test_shortlisted_to_opened(db_conn):
    """shortlisted → opened。"""
    opp_id = _insert_opp(db_conn, status="shortlisted")
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "opened"
    assert _get_status(db_conn, opp_id) == "opened"


@pytest.mark.parametrize(
    "status",
    ["opened", "applying", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"],
)
def test_higher_status_not_downgraded_to_opened(db_conn, status):
    """高级状态不会被点击链接降级为 opened，但仍可打开链接。"""
    opp_id = _insert_opp(db_conn, status=status)
    result = mark_as_opened(opp_id, db_conn)
    # 高阶段：仍可打开链接，但不改状态
    assert result["action"] == "opened_without_status_change"
    assert result["should_open"] is True
    assert result["url"] is not None  # 仍返回有效 URL
    assert _get_status(db_conn, opp_id) == status  # 状态不变


# ---------------------------------------------------------------------------
# 任务 8 修正：高阶段记录仍可打开链接
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["opened", "applying", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"],
)
def test_higher_status_returns_correct_url(db_conn, status):
    """所有高阶段状态均能返回正确 URL。"""
    opp_id = _insert_opp(
        db_conn, record_type="job", status=status,
        application_url="https://example.com/apply/high",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["should_open"] is True
    assert result["url"] == "https://example.com/apply/high"


@pytest.mark.parametrize(
    "status",
    ["opened", "applying", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"],
)
def test_higher_status_opened_at_applied_at_unchanged(db_conn, status):
    """高阶段状态点击链接后 opened_at / applied_at 不变。"""
    opp_id = _insert_opp(db_conn, status=status)
    # 预设 opened_at 和 applied_at
    db_conn.execute(
        "UPDATE opportunities SET opened_at = ?, applied_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", opp_id),
    )
    db_conn.commit()
    mark_as_opened(opp_id, db_conn)
    assert _get_opened_at(db_conn, opp_id) == "2026-01-01T00:00:00+00:00"
    assert _get_applied_at(db_conn, opp_id) == "2026-01-02T00:00:00+00:00"


def test_higher_status_campaign_url_selection(db_conn):
    """campaign 高阶段状态仍按 campaign 链接选择规则。"""
    # campaign 优先 application_url
    opp_id_app = _insert_opp(
        db_conn, record_type="campaign", status="interview",
        application_url="https://example.com/apply/c",
        announcement_url="https://example.com/ann/c",
        source_row=2, dedupe_key="k_camp_app",
    )
    result_app = mark_as_opened(opp_id_app, db_conn)
    assert result_app["url"] == "https://example.com/apply/c"
    assert _get_status(db_conn, opp_id_app) == "interview"

    # campaign 回退 announcement_url
    opp_id_ann = _insert_opp(
        db_conn, record_type="campaign", status="offer",
        application_url="",
        announcement_url="https://example.com/ann/d",
        source_row=3, dedupe_key="k_camp_ann",
    )
    result_ann = mark_as_opened(opp_id_ann, db_conn)
    assert result_ann["url"] == "https://example.com/ann/d"
    assert _get_status(db_conn, opp_id_ann) == "offer"


def test_higher_status_no_link_does_not_open(db_conn):
    """高阶段状态无链接时不打开、不改状态。"""
    opp_id = _insert_opp(
        db_conn, record_type="job", status="interview",
        application_url="", announcement_url="",
    )
    result = mark_as_opened(opp_id, db_conn)
    assert result["action"] == "no_link"
    assert result["should_open"] is False
    assert result["url"] is None
    assert _get_status(db_conn, opp_id) == "interview"


def test_dashboard_py_compiles():
    """dashboard.py 可编译（compileall 等效检查）。"""
    import py_compile

    dashboard_path = PROJECT_ROOT / "pages" / "dashboard.py"
    py_compile.compile(str(dashboard_path), doraise=True)


def test_open_link_never_becomes_applied(db_conn):
    """点击链接绝不变为 applied。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    mark_as_opened(opp_id, db_conn)
    assert _get_status(db_conn, opp_id) == "opened"
    assert _get_status(db_conn, opp_id) != "applied"
    assert _get_applied_at(db_conn, opp_id) is None


def test_mark_as_opened_not_found(db_conn):
    """记录不存在时返回 not_found。"""
    result = mark_as_opened(99999, db_conn)
    assert result["action"] == "not_found"


def test_mark_as_opened_rereads_from_db(db_conn):
    """从数据库重新读取记录，不信任 UI 旧状态。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    # 模拟 UI 缓存的旧状态（直接调用服务，服务应从 DB 读取真实状态）
    conn2 = get_connection(":memory:")
    init_db(conn2)
    # conn2 中没有这条记录 → not_found
    result = mark_as_opened(opp_id, conn2)
    assert result["action"] == "not_found"
    conn2.close()
    # 原连接中正常
    result2 = mark_as_opened(opp_id, db_conn)
    assert result2["action"] == "opened"


def test_service_does_not_open_browser(db_conn):
    """服务层不直接打开浏览器（mock webbrowser 验证不被调用）。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    with patch("webbrowser.open") as mock_open:
        result = mark_as_opened(opp_id, db_conn)
        # 服务层返回 URL，但不调用 webbrowser.open
        mock_open.assert_not_called()
    assert result["url"] is not None


# ---------------------------------------------------------------------------
# confirm_applied：状态流转
# ---------------------------------------------------------------------------


def test_confirm_applied_sets_applied_and_applied_at(db_conn):
    """确认已投递 → applied + applied_at。"""
    opp_id = _insert_opp(db_conn, status="opened")
    result = confirm_applied(opp_id, db_conn)
    assert result["action"] == "applied"
    assert _get_status(db_conn, opp_id) == "applied"
    assert _get_applied_at(db_conn, opp_id) is not None


@pytest.mark.parametrize("status", ["discovered", "shortlisted", "opened", "applying"])
def test_appliable_statuses_to_applied(db_conn, status):
    """discovered/shortlisted/opened/applying → applied。"""
    opp_id = _insert_opp(db_conn, status=status)
    result = confirm_applied(opp_id, db_conn)
    assert result["action"] == "applied"
    assert _get_status(db_conn, opp_id) == "applied"


@pytest.mark.parametrize(
    "status",
    ["assessment", "interview", "offer", "rejected", "withdrawn"],
)
def test_terminal_status_not_downgraded_to_applied(db_conn, status):
    """终态不被确认按钮降级为 applied。"""
    opp_id = _insert_opp(db_conn, status=status)
    result = confirm_applied(opp_id, db_conn)
    assert result["action"] == "no_change"
    assert _get_status(db_conn, opp_id) == status
    assert _get_applied_at(db_conn, opp_id) is None


def test_confirm_applied_idempotent(db_conn):
    """已是 applied 时幂等，不重复写入。"""
    opp_id = _insert_opp(db_conn, status="applied")
    old_applied_at = "2026-01-01T00:00:00+00:00"
    db_conn.execute(
        "UPDATE opportunities SET applied_at = ? WHERE id = ?",
        (old_applied_at, opp_id),
    )
    db_conn.commit()
    result = confirm_applied(opp_id, db_conn)
    assert result["action"] == "no_change"
    # applied_at 不被覆盖
    assert _get_applied_at(db_conn, opp_id) == old_applied_at


def test_confirm_applied_not_found(db_conn):
    """记录不存在时返回 not_found。"""
    result = confirm_applied(99999, db_conn)
    assert result["action"] == "not_found"


# ---------------------------------------------------------------------------
# update_status：白名单
# ---------------------------------------------------------------------------


def test_update_status_whitelist_rejects_arbitrary_string(db_conn):
    """update_status 拒绝白名单外的任意字符串。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    result = update_status(opp_id, "HACKED", db_conn)
    assert result["action"] == "rejected"
    assert _get_status(db_conn, opp_id) == "discovered"


@pytest.mark.parametrize("status", ["assessment", "interview", "offer", "rejected", "withdrawn"])
def test_update_status_whitelist_allows_manual_statuses(db_conn, status):
    """update_status 允许白名单内的手动状态。"""
    opp_id = _insert_opp(db_conn, status="discovered")
    result = update_status(opp_id, status, db_conn)
    assert result["action"] == "updated"
    assert _get_status(db_conn, opp_id) == status


def test_update_status_rejects_discovered(db_conn):
    """update_status 不允许 discovered（由 mark_as_opened/confirm_applied 处理）。"""
    opp_id = _insert_opp(db_conn, status="opened")
    result = update_status(opp_id, "discovered", db_conn)
    assert result["action"] == "rejected"


# ---------------------------------------------------------------------------
# UI 按钮与服务层调用边界
# ---------------------------------------------------------------------------


def test_dashboard_calls_mark_as_opened_on_button():
    """dashboard.py 在按钮点击时调用 mark_as_opened（源码静态校验）。"""
    source = (PROJECT_ROOT / "pages" / "dashboard.py").read_text(
        encoding="utf-8"
    )
    assert "mark_as_opened" in source
    assert "confirm_applied" in source
    assert "webbrowser.open" in source  # UI 层打开浏览器
    assert "interactive" in source  # 仅在真实 DB 存在时渲染按钮


def test_card_has_two_separate_buttons():
    """卡片有两个独立按钮（打开投递链接 / 确认已投递）。"""
    source = (
        PROJECT_ROOT / "components" / "opportunity_card.py"
    ).read_text(encoding="utf-8")
    assert "打开投递链接" in source
    assert "确认已投递" in source
    # 两个按钮 key 不同
    assert "open_link_" in source
    assert "confirm_applied_" in source


def test_service_layer_has_no_webbrowser():
    """服务层不直接打开浏览器。"""
    source = (
        PROJECT_ROOT / "services" / "opportunity_service.py"
    ).read_text(encoding="utf-8")
    assert "webbrowser" not in source, "服务层不得直接打开浏览器"


def test_service_does_not_reference_db_path():
    """服务层不引用默认数据库路径。"""
    source = (
        PROJECT_ROOT / "services" / "opportunity_service.py"
    ).read_text(encoding="utf-8")
    assert "DB_PATH" not in source


# ---------------------------------------------------------------------------
# 浏览/筛选/分页不写数据库
# ---------------------------------------------------------------------------


def test_browsing_does_not_write_db(db_conn):
    """fetch_all_opportunities / filter_opportunities 不写数据库。"""
    _insert_opp(db_conn, status="discovered", dedupe_key="k1")
    before_count = int(
        db_conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    )
    # 浏览
    opps = fetch_all_opportunities(db_conn)
    from services.opportunity_service import filter_opportunities, get_filter_options, paginate_list

    filter_opportunities(opps)
    get_filter_options(opps)
    paginate_list(len(opps), 20, 1)
    after_count = int(
        db_conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    )
    assert before_count == after_count


def test_preview_connection_readonly_does_not_write(tmp_path):
    """预览连接只读，不写数据库。"""
    db_file = tmp_path / "test.db"
    conn = get_connection(db_file)
    init_db(conn)
    _insert_opp(conn, status="discovered", dedupe_key="k1")
    conn.close()

    # 用预览连接（只读）
    preview_conn = get_preview_connection(db_file)
    before = int(
        preview_conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    )
    fetch_all_opportunities(preview_conn)
    after = int(
        preview_conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    )
    preview_conn.close()
    assert before == after == 1


# ---------------------------------------------------------------------------
# 不读取 data/private
# ---------------------------------------------------------------------------


def test_no_data_private_access_in_source():
    """任务 8 相关源码不得引用 data/private。"""
    for path in [
        PROJECT_ROOT / "services" / "opportunity_service.py",
        PROJECT_ROOT / "pages" / "dashboard.py",
        PROJECT_ROOT / "components" / "opportunity_card.py",
    ]:
        source = path.read_text(encoding="utf-8")
        assert "data/private" not in source
        assert "智联-岗位信息表" not in source


# ---------------------------------------------------------------------------
# 参数化 SQL（无注入）
# ---------------------------------------------------------------------------


def test_all_sql_uses_parameters():
    """mark_as_opened / confirm_applied / update_status 使用参数化 SQL。"""
    source = (
        PROJECT_ROOT / "services" / "opportunity_service.py"
    ).read_text(encoding="utf-8")
    # 不含字符串拼接 SQL
    assert "f\"UPDATE opportunities" not in source
    assert "f\"INSERT INTO opportunities" not in source
    assert "f\"SELECT * FROM opportunities" not in source
    assert "f\"DELETE FROM" not in source


# ---------------------------------------------------------------------------
# set_priority：白名单与持久化（任务 9）
# ---------------------------------------------------------------------------


def test_set_priority_high(db_conn):
    """set_priority 更新为 high。"""
    opp_id = _insert_opp(db_conn, priority="low")
    result = set_priority(opp_id, "high", db_conn)
    assert result["action"] == "updated"
    assert result["priority"] == "high"
    actual = str(
        db_conn.execute(
            "SELECT priority FROM opportunities WHERE id = ?", (opp_id,)
        ).fetchone()["priority"]
    )
    assert actual == "high"


def test_set_priority_medium_and_low(db_conn):
    """set_priority 更新为 medium / low。"""
    opp_id = _insert_opp(db_conn, priority="high")
    assert set_priority(opp_id, "medium", db_conn)["action"] == "updated"
    assert set_priority(opp_id, "low", db_conn)["action"] == "updated"


def test_set_priority_rejects_arbitrary_string(db_conn):
    """set_priority 拒绝白名单外字符串。"""
    opp_id = _insert_opp(db_conn, priority="low")
    result = set_priority(opp_id, "urgent", db_conn)
    assert result["action"] == "rejected"
    actual = str(
        db_conn.execute(
            "SELECT priority FROM opportunities WHERE id = ?", (opp_id,)
        ).fetchone()["priority"]
    )
    assert actual == "low"


def test_set_priority_not_found(db_conn):
    """set_priority 记录不存在时返回 not_found。"""
    result = set_priority(99999, "high", db_conn)
    assert result["action"] == "not_found"
