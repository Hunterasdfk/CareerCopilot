"""看板 service 层与组件测试（任务 7）。

覆盖任务 7 指令中的全部必测项：
- 空数据库；数据库不存在时不创建文件；返回全部机会；
- 公司/地区/record_type/每个 status 筛选；多条件组合；“全部”选项；
- location 缺失处理；无结果空状态；
- 全量视图与候选视图使用同一筛选结果；
- 筛选后 total_count 与列表长度一致；coverage_gap 正确；
- campaign_only 正确；Top 3 不截断；第 4、5 条仍可访问；
- campaign/job 视觉标签不同；1000 条分页；单页上限 100；
- 页码越界自动夹紧；改变筛选后页码正确处理；
- 页面/服务不写数据库；不调用 mark_as_opened / UPDATE；
- 不读取 data/private；不产生数据库或缓存残留；
- 任务 1–6 既有测试继续通过。

测试数据只使用虚构公司、岗位和 example.com 链接；数据库使用
:memory: 或 tmp_path。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from components.opportunity_card import _record_type_badge
from components.filters import (
    _company_format_func,
    _filter_format_func,
    _location_format_func,
)
from database.db_handler import get_connection, get_preview_connection, init_db
from services.candidate_service import build_company_coverage
from services.opportunity_service import (
    DASHBOARD_MAX_PAGE_SIZE,
    FILTER_ALL,
    FILTER_ALL_LABEL,
    MISSING_LOCATION,
    MISSING_LOCATION_LABEL,
    fetch_all_opportunities,
    filter_opportunities,
    get_filter_options,
    paginate_list,
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
    record_type: str = "campaign",
    display_title: str = "示例机会",
    company_name: str = "示例科技A",
    priority: str = "low",
    status: str = "discovered",
    location: str = "北京市",
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
        "location": location,
        "source_sheet": "中国大陆",
        "source_row": source_row,
        "dedupe_key": dedupe_key or f"k_{record_type}_{company_name}_{source_row}",
        "application_url": "https://example.com/apply",
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


def _db_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])


# ---------------------------------------------------------------------------
# 空数据库 / 数据库不存在
# ---------------------------------------------------------------------------


def test_empty_database_returns_empty_list(db_conn):
    """空数据库返回空列表。"""
    assert fetch_all_opportunities(db_conn) == []


def test_dashboard_does_not_create_db_file(tmp_path):
    """数据库不存在时打开看板不创建文件。"""
    db_file = tmp_path / "careercopilot.db"
    assert not db_file.exists()
    conn = get_preview_connection(db_file)
    opps = fetch_all_opportunities(conn)
    conn.close()
    assert opps == []
    assert not db_file.exists(), "看板不得创建持久数据库文件"


def test_fetch_all_returns_all_opportunities(db_conn):
    """返回全部机会（不筛选 status）。"""
    _insert_opp(db_conn, status="discovered", source_row=2, dedupe_key="k1")
    _insert_opp(db_conn, status="interview", source_row=3, dedupe_key="k2")
    _insert_opp(db_conn, status="offer", source_row=4, dedupe_key="k3")
    opps = fetch_all_opportunities(db_conn)
    assert len(opps) == 3
    assert {opp["status"] for opp in opps} == {"discovered", "interview", "offer"}


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------


def test_company_filter(db_conn):
    _insert_opp(db_conn, company_name="示例科技A", source_row=2, dedupe_key="k_a1")
    _insert_opp(db_conn, company_name="示例制造B", source_row=3, dedupe_key="k_b1")
    _insert_opp(db_conn, company_name="示例科技A", source_row=4, dedupe_key="k_a2")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    assert len(filtered) == 2
    assert {opp["company_name"] for opp in filtered} == {"示例科技A"}


def test_location_filter(db_conn):
    _insert_opp(db_conn, location="北京市", source_row=2, dedupe_key="k_bj")
    _insert_opp(db_conn, location="上海市", source_row=3, dedupe_key="k_sh")
    _insert_opp(db_conn, location="北京市", source_row=4, dedupe_key="k_bj2")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, location="北京市")
    assert len(filtered) == 2
    assert {opp["location"] for opp in filtered} == {"北京市"}


def test_location_missing_filter(db_conn):
    """缺失 location 使用 MISSING_LOCATION 哨兵筛选（不与真实"未填写"冲突）。"""
    _insert_opp(db_conn, location="", source_row=2, dedupe_key="k_empty")
    _insert_opp(db_conn, location="北京市", source_row=3, dedupe_key="k_bj")
    opps = fetch_all_opportunities(db_conn)
    # 用哨兵筛选缺失地区
    filtered = filter_opportunities(opps, location=MISSING_LOCATION)
    assert len(filtered) == 1
    assert filtered[0]["location"] == ""


def test_record_type_filter(db_conn):
    _insert_opp(db_conn, record_type="campaign", source_row=2, dedupe_key="k_c")
    _insert_opp(db_conn, record_type="job", source_row=3, dedupe_key="k_j")
    _insert_opp(db_conn, record_type="campaign", source_row=4, dedupe_key="k_c2")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, record_type="job")
    assert len(filtered) == 1
    assert filtered[0]["record_type"] == "job"


@pytest.mark.parametrize(
    "status",
    [
        "discovered",
        "shortlisted",
        "opened",
        "applying",
        "applied",
        "assessment",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
    ],
)
def test_each_status_filter(db_conn, status):
    """每个合法 status 都能被单独筛选。"""
    _insert_opp(db_conn, status=status, source_row=2, dedupe_key=f"k_{status}")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, status=status)
    assert len(filtered) == 1
    assert filtered[0]["status"] == status


def test_multiple_filters_combined(db_conn):
    """多条件组合筛选。"""
    _insert_opp(
        db_conn, company_name="示例科技A", location="北京市",
        record_type="job", status="interview", source_row=2, dedupe_key="k1",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", location="上海市",
        record_type="job", status="interview", source_row=3, dedupe_key="k2",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", location="北京市",
        record_type="campaign", status="interview", source_row=4, dedupe_key="k3",
    )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(
        opps, company_name="示例科技A", location="北京市",
        record_type="job", status="interview",
    )
    assert len(filtered) == 1
    assert filtered[0]["source_row"] == 2


def test_filter_all_does_not_misfilter(db_conn):
    """“全部”选项不得误过滤。"""
    _insert_opp(db_conn, company_name="示例科技A", source_row=2, dedupe_key="k1")
    _insert_opp(db_conn, company_name="示例制造B", source_row=3, dedupe_key="k2")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    assert len(filtered) == 2


def test_filter_options_from_actual_values(db_conn):
    """选项根据数据库实际值生成（哨兵 + 真实值）。"""
    _insert_opp(db_conn, company_name="示例科技A", location="北京市",
               record_type="campaign", status="discovered",
               source_row=2, dedupe_key="k1")
    _insert_opp(db_conn, company_name="示例制造B", location="",
               record_type="job", status="interview",
               source_row=3, dedupe_key="k2")
    opps = fetch_all_opportunities(db_conn)
    options = get_filter_options(opps)
    # 首项为哨兵（不是字符串"全部"）
    assert options["company_name"][0] is FILTER_ALL
    assert "示例科技A" in options["company_name"]
    assert "示例制造B" in options["company_name"]
    # 缺失地区用哨兵表示
    assert MISSING_LOCATION in options["location"]
    assert "campaign" in options["record_type"]
    assert "job" in options["record_type"]
    assert "discovered" in options["status"]
    assert "interview" in options["status"]


def test_empty_state_when_no_results(db_conn):
    """无结果空状态。"""
    _insert_opp(db_conn, company_name="示例科技A", source_row=2, dedupe_key="k1")
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="不存在公司")
    assert len(filtered) == 0


# ---------------------------------------------------------------------------
# 候选清单视图（复用任务 6 build_company_coverage）
# ---------------------------------------------------------------------------


def test_candidate_view_uses_same_filtered_result(db_conn):
    """全量视图与候选视图使用同一筛选结果。"""
    _insert_opp(
        db_conn, company_name="示例科技A", record_type="campaign",
        source_row=2, dedupe_key="k_a1",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", record_type="job",
        source_row=3, dedupe_key="k_a2",
    )
    _insert_opp(
        db_conn, company_name="示例制造B", record_type="job",
        source_row=4, dedupe_key="k_b1",
    )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    coverage = build_company_coverage(filtered)
    # 只剩一家公司
    assert len(coverage) == 1
    assert coverage[0]["company_name"] == "示例科技A"
    assert coverage[0]["total_count"] == 2


def test_filtered_total_count_equals_list_length(db_conn):
    """筛选后的 total_count 与列表长度一致。"""
    for i in range(2, 7):
        _insert_opp(
            db_conn, company_name="示例科技A", source_row=i, dedupe_key=f"k_{i}"
        )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    coverage = build_company_coverage(filtered)
    for company in coverage:
        assert company["total_count"] == len(company["opportunities"])


def test_filtered_coverage_gap_correct(db_conn):
    """筛选后的 coverage_gap 正确。"""
    _insert_opp(
        db_conn, company_name="示例科技A", source_row=2, dedupe_key="k_a1"
    )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    coverage = build_company_coverage(filtered)
    assert coverage[0]["coverage_gap"] == 2  # 1 条 → gap=2


def test_filtered_campaign_only_correct(db_conn):
    """筛选后的 campaign_only 正确。"""
    _insert_opp(
        db_conn, company_name="示例科技A", record_type="campaign",
        source_row=2, dedupe_key="k_c1",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", record_type="campaign",
        source_row=3, dedupe_key="k_c2",
    )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    coverage = build_company_coverage(filtered)
    assert coverage[0]["campaign_only"] is True
    assert coverage[0]["campaign_only_message"] == "需进入官网选择具体岗位"


def test_top_three_not_truncated_in_candidate_view(db_conn):
    """Top 3 不截断，第 4、5 条仍可访问。"""
    for i in range(2, 7):
        _insert_opp(
            db_conn, company_name="示例科技A", source_row=i, dedupe_key=f"k_{i}"
        )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name="示例科技A")
    coverage = build_company_coverage(filtered)
    company = coverage[0]
    assert len(company["opportunities"]) == 5
    assert len(company["highlighted_top_three"]) == 3
    # 第 4、5 条仍可访问
    assert company["opportunities"][3]["source_row"] == 5
    assert company["opportunities"][4]["source_row"] == 6


def test_all_statuses_visible_in_candidate_view(db_conn):
    """discovered/opened/applied/interview 等状态机会仍可显示。"""
    for i, status in enumerate(
        ["discovered", "opened", "applied", "interview"], start=2
    ):
        _insert_opp(
            db_conn, status=status, source_row=i, dedupe_key=f"k_{status}"
        )
    opps = fetch_all_opportunities(db_conn)
    coverage = build_company_coverage(opps)
    assert coverage[0]["total_count"] == 4


# ---------------------------------------------------------------------------
# 视觉区分
# ---------------------------------------------------------------------------


def test_campaign_job_visual_labels_differ():
    """campaign/job 视觉标签不同。"""
    job_badge = _record_type_badge("job")
    campaign_badge = _record_type_badge("campaign")
    assert job_badge != campaign_badge
    assert "JOB" in job_badge
    assert "CAMPAIGN" in campaign_badge


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


def test_paginate_1000_records():
    """1000 条记录分页。"""
    info = paginate_list(1000, 20, 1)
    assert info["total_pages"] == 50
    assert info["start"] == 0
    assert info["end"] == 20
    assert info["has_next"] is True


def test_paginate_max_page_size_100():
    """单页上限不超过 100。"""
    info = paginate_list(1000, 200, 1)
    assert info["page_size"] == DASHBOARD_MAX_PAGE_SIZE


def test_paginate_page_clamped():
    """页码越界自动夹紧。"""
    info = paginate_list(100, 20, 99)
    assert info["current_page"] == 5  # 共 5 页


def test_paginate_filter_change_resets_page():
    """改变筛选后页码正确处理（夹紧到新范围）。"""
    # 原 100 条，第 5 页
    info_old = paginate_list(100, 20, 5)
    assert info_old["current_page"] == 5
    # 筛选后只剩 10 条，页码夹紧到 1
    info_new = paginate_list(10, 20, info_old["current_page"])
    assert info_new["current_page"] == 1


def test_paginate_empty():
    """0 条时分页仍返回有效结构。"""
    info = paginate_list(0, 20, 1)
    assert info["total_pages"] == 1
    assert info["start"] == 0
    assert info["end"] == 0


# ---------------------------------------------------------------------------
# 不写数据库 / 安全边界
# ---------------------------------------------------------------------------


def test_fetch_all_does_not_write_db(db_conn):
    """fetch_all_opportunities 不写数据库。"""
    _insert_opp(db_conn, source_row=2, dedupe_key="k1")
    before = _db_count(db_conn)
    fetch_all_opportunities(db_conn)
    after = _db_count(db_conn)
    assert before == after == 1


def test_filter_does_not_write_db(db_conn):
    """filter_opportunities 不写数据库。"""
    _insert_opp(db_conn, source_row=2, dedupe_key="k1")
    before = _db_count(db_conn)
    opps = fetch_all_opportunities(db_conn)
    filter_opportunities(opps, company_name="示例科技A")
    after = _db_count(db_conn)
    assert before == after == 1


def test_no_mark_as_opened_or_update_in_source():
    """看板只读路径不直接执行 SQL 写操作。

    任务 8 后 dashboard.py 通过 service 层（mark_as_opened / confirm_applied）
    间接更新状态，但页面本身不直接执行 SQL 写操作（INSERT/UPDATE/DELETE）。
    candidate_service.py 始终只读。
    """
    read_only_sources = []
    for path in [
        PROJECT_ROOT / "pages" / "dashboard.py",
        PROJECT_ROOT / "services" / "candidate_service.py",
    ]:
        if path.exists():
            read_only_sources.append(path.read_text(encoding="utf-8"))
    for source in read_only_sources:
        # 页面不直接执行 SQL 写操作（通过 service 层间接调用）
        assert "UPDATE opportunities" not in source, "看板不得直接执行 UPDATE"
        assert "DELETE FROM" not in source, "看板不得执行 DELETE"
        assert "INSERT INTO" not in source, "看板不得执行 INSERT"
    # candidate_service.py 仍只读，不含任何状态变更方法
    candidate_source = (PROJECT_ROOT / "services" / "candidate_service.py").read_text(encoding="utf-8")
    assert "mark_as_opened" not in candidate_source, "候选服务不得调用 mark_as_opened"
    assert "confirm_applied" not in candidate_source, "候选服务不得调用 confirm_applied"


def test_no_data_private_access_in_source():
    """页面与组件源码不得引用 data/private 或真实工作簿文件名。"""
    for path in [
        PROJECT_ROOT / "pages" / "dashboard.py",
        PROJECT_ROOT / "components" / "opportunity_card.py",
        PROJECT_ROOT / "components" / "filters.py",
    ]:
        source = path.read_text(encoding="utf-8")
        assert "data/private" not in source
        assert "智联-岗位信息表" not in source


def test_no_db_residue(tmp_path):
    """使用 :memory: 不产生数据库文件残留。"""
    db_file = tmp_path / "residue.db"
    assert not db_file.exists()
    conn = get_preview_connection(db_file)
    fetch_all_opportunities(conn)
    conn.close()
    assert not db_file.exists()


def test_service_does_not_reference_db_path():
    """看板相关 service 不引用默认数据库路径。"""
    for path in [
        PROJECT_ROOT / "services" / "opportunity_service.py",
        PROJECT_ROOT / "services" / "candidate_service.py",
    ]:
        source = path.read_text(encoding="utf-8")
        assert "DB_PATH" not in source, f"{path.name} 不得引用默认数据库路径"


# ---------------------------------------------------------------------------
# 重构回归：build_company_coverage 与 get_company_coverage 一致
# ---------------------------------------------------------------------------


def test_build_company_coverage_consistent_with_get(db_conn):
    """build_company_coverage（纯函数）与 get_company_coverage 返回结构一致。"""
    from services.candidate_service import get_company_coverage

    _insert_opp(
        db_conn, company_name="示例科技A", record_type="campaign",
        source_row=2, dedupe_key="k_c1",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", record_type="job",
        source_row=3, dedupe_key="k_j1",
    )
    opps = fetch_all_opportunities(db_conn)
    built = build_company_coverage(opps)
    direct = get_company_coverage(db_conn)
    assert len(built) == len(direct) == 1
    assert built[0]["company_name"] == direct[0]["company_name"]
    assert built[0]["total_count"] == direct[0]["total_count"]
    assert built[0]["coverage_gap"] == direct[0]["coverage_gap"]
    assert built[0]["campaign_only"] == direct[0]["campaign_only"]
    assert (
        built[0]["campaign_only_message"]
        == direct[0]["campaign_only_message"]
    )


# ===========================================================================
# 任务 7 修正测试
# ===========================================================================


# ---------------------------------------------------------------------------
# 修复二：公司分页页码选择器
# ---------------------------------------------------------------------------


def test_company_pagination_51_companies_50_per_page(db_conn):
    """51 家公司、每页 50 家时，用户可选择并访问第 2 页。"""
    for i in range(51):
        _insert_opp(
            db_conn,
            company_name=f"示例公司{i:03d}",
            source_row=i + 2,
            dedupe_key=f"k_co_{i}",
        )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    coverage = build_company_coverage(filtered)
    total_companies = len(coverage)
    assert total_companies == 51

    # 公司分页：50/页 → 2 页
    company_page_info = paginate_list(total_companies, 50, 1)
    assert company_page_info["total_pages"] == 2
    assert company_page_info["end"] == 50  # 第 1 页 50 家

    # 用户选择第 2 页
    company_page_info_p2 = paginate_list(total_companies, 50, 2)
    assert company_page_info_p2["current_page"] == 2
    assert company_page_info_p2["start"] == 50
    assert company_page_info_p2["end"] == 51  # 第 2 页 1 家
    page2_companies = coverage[
        company_page_info_p2["start"] : company_page_info_p2["end"]
    ]
    assert len(page2_companies) == 1
    assert page2_companies[0]["company_name"] == "示例公司050"


def test_company_pagination_clamps_on_filter_change(db_conn):
    """筛选导致公司数减少时，页码自动夹紧到有效范围。"""
    for i in range(100):
        _insert_opp(
            db_conn,
            company_name=f"示例公司{i:03d}",
            source_row=i + 2,
            dedupe_key=f"k_co_{i}",
        )
    opps = fetch_all_opportunities(db_conn)
    # 原 100 家，第 2 页
    info_old = paginate_list(100, 50, 2)
    assert info_old["current_page"] == 2
    # 筛选后只剩 10 家，页码夹紧到 1
    info_new = paginate_list(10, 50, info_old["current_page"])
    assert info_new["current_page"] == 1


# ---------------------------------------------------------------------------
# 修复三：哨兵值与真实数据区分
# ---------------------------------------------------------------------------


def test_real_company_named_filter_all_can_be_filtered(db_conn):
    """真实公司名为"全部"必须可单独筛选（不与 FILTER_ALL 哨兵冲突）。"""
    _insert_opp(
        db_conn, company_name="全部", source_row=2, dedupe_key="k_all_co"
    )
    _insert_opp(
        db_conn, company_name="示例科技A", source_row=3, dedupe_key="k_a"
    )
    opps = fetch_all_opportunities(db_conn)

    # 用哨兵 → 不过滤，返回全部
    all_filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    assert len(all_filtered) == 2

    # 用字符串"全部" → 只匹配真实公司名"全部"
    real_filtered = filter_opportunities(opps, company_name="全部")
    assert len(real_filtered) == 1
    assert real_filtered[0]["company_name"] == "全部"


def test_real_location_named_unfilled_can_be_filtered(db_conn):
    """真实地区为"未填写"必须可单独筛选（不与 MISSING_LOCATION 哨兵冲突）。"""
    _insert_opp(
        db_conn, location="未填写", source_row=2, dedupe_key="k_real_unfilled"
    )
    _insert_opp(
        db_conn, location="", source_row=3, dedupe_key="k_empty_loc"
    )
    _insert_opp(
        db_conn, location="北京市", source_row=4, dedupe_key="k_bj"
    )
    opps = fetch_all_opportunities(db_conn)

    # 用哨兵 → 只匹配空/缺失地区（不匹配真实"未填写"字符串）
    missing_filtered = filter_opportunities(opps, location=MISSING_LOCATION)
    assert len(missing_filtered) == 1
    assert missing_filtered[0]["location"] == ""

    # 用字符串"未填写" → 只匹配真实地区"未填写"
    real_filtered = filter_opportunities(opps, location="未填写")
    assert len(real_filtered) == 1
    assert real_filtered[0]["location"] == "未填写"


def test_no_duplicate_filter_all_string_in_options(db_conn):
    """筛选选项不能出现重复的"全部"字符串。"""
    _insert_opp(
        db_conn, company_name="全部", location="未填写",
        source_row=2, dedupe_key="k1",
    )
    _insert_opp(
        db_conn, company_name="示例科技A", location="",
        source_row=3, dedupe_key="k2",
    )
    opps = fetch_all_opportunities(db_conn)
    options = get_filter_options(opps)

    # company_name 选项：哨兵 + 真实"全部" + 示例科技A
    # 只有哨兵不是字符串，真实"全部"是字符串 → 不重复
    string_count = sum(
        1 for v in options["company_name"] if isinstance(v, str) and v == "全部"
    )
    assert string_count == 1  # 只有一个真实"全部"字符串
    assert options["company_name"][0] is FILTER_ALL  # 哨兵

    # location 选项：哨兵 + 缺失哨兵 + 真实"未填写" + ...
    unfilled_count = sum(
        1 for v in options["location"] if isinstance(v, str) and v == "未填写"
    )
    assert unfilled_count == 1  # 只有一个真实"未填写"字符串


def test_sentinel_not_equal_to_any_string():
    """哨兵对象不与任何字符串相等。"""
    assert FILTER_ALL != "全部"
    assert FILTER_ALL != "全部"
    assert MISSING_LOCATION != "未填写"
    assert MISSING_LOCATION != ""
    # 哨兵与自身 identity 相等
    assert FILTER_ALL is FILTER_ALL
    assert MISSING_LOCATION is MISSING_LOCATION
    # 哨兵之间不等
    assert FILTER_ALL is not MISSING_LOCATION


# ---------------------------------------------------------------------------
# 修复四：候选视图大数据渲染保护
# ---------------------------------------------------------------------------


def test_single_company_1000_opportunities_paginated(db_conn):
    """单家公司 1000 条机会：公司内机会分页，不会一次渲染全部卡片。"""
    for i in range(1000):
        _insert_opp(
            db_conn,
            company_name="示例科技A",
            source_row=i + 2,
            dedupe_key=f"k_{i}",
        )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    coverage = build_company_coverage(filtered)

    assert len(coverage) == 1
    company = coverage[0]
    assert company["total_count"] == 1000

    # 公司内机会分页：单页最多 100 条
    opp_page_info = paginate_list(1000, 100, 1)
    assert opp_page_info["page_size"] == 100
    assert opp_page_info["total_pages"] == 10
    assert opp_page_info["end"] - opp_page_info["start"] == 100  # 只渲染 100 条

    # 第 2 页
    opp_page_info_p2 = paginate_list(1000, 100, 2)
    assert opp_page_info_p2["start"] == 100
    assert opp_page_info_p2["end"] == 200

    # 全部机会仍可通过分页访问，Top 3 不截断
    assert len(company["opportunities"]) == 1000
    assert len(company["highlighted_top_three"]) == 3


def test_company_summary_does_not_render_all_cards(db_conn):
    """公司摘要只展示摘要信息，不渲染全部机会卡片。

    验证：候选视图先按公司分页展示摘要，用户选择一家公司后
    才渲染该公司机会（且分页），不会一次渲染数万条卡片。
    """
    # 5 家公司，每家 200 条 = 1000 条机会
    for co in range(5):
        for i in range(200):
            _insert_opp(
                db_conn,
                company_name=f"示例公司{co}",
                source_row=co * 200 + i + 2,
                dedupe_key=f"k_{co}_{i}",
            )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    coverage = build_company_coverage(filtered)

    assert len(coverage) == 5

    # 公司分页：每页 50 家 → 1 页 5 家
    company_page_info = paginate_list(5, 50, 1)
    page_companies = coverage[
        company_page_info["start"] : company_page_info["end"]
    ]
    assert len(page_companies) == 5

    # 用户选择第 1 家公司
    selected = page_companies[0]
    assert selected["total_count"] == 200

    # 该公司内机会分页：单页 100 条 → 2 页
    opp_page_info = paginate_list(200, 100, 1)
    assert opp_page_info["end"] - opp_page_info["start"] == 100
    # 不会一次渲染 200 条，只渲染当前页 100 条
    page_opps = selected["opportunities"][
        opp_page_info["start"] : opp_page_info["end"]
    ]
    assert len(page_opps) == 100


# ---------------------------------------------------------------------------
# 修复四：页码选择器静态校验
# ---------------------------------------------------------------------------


def test_dashboard_has_company_page_selector():
    """dashboard.py 含公司页码选择器（key=dash_company_page_select）。"""
    source = (PROJECT_ROOT / "pages" / "dashboard.py").read_text(
        encoding="utf-8"
    )
    assert "dash_company_page_select" in source, "应含公司页码选择器"
    assert "dash_selected_company" in source, "应含公司选择控件"
    assert "dash_company_opp_page" in source, "应含公司内机会分页"


def test_dashboard_uses_format_func_for_filters():
    """dashboard/filters 使用字段感知 format_func 显示哨兵标签。"""
    source = (PROJECT_ROOT / "components" / "filters.py").read_text(
        encoding="utf-8"
    )
    assert "format_func" in source, "筛选器应用 format_func 显示标签"
    assert "_company_format_func" in source
    assert "_location_format_func" in source
    assert "_filter_format_func" in source
    assert "FILTER_ALL_LABEL" in source
    assert "MISSING_LOCATION_LABEL" in source


# ---------------------------------------------------------------------------
# 任务 7 最后修正：字段感知 format_func 区分哨兵与真实同名值
# ---------------------------------------------------------------------------


def test_company_format_func_four_labels_distinct():
    """公司字段四种显示标签各不相同且对应正确。

    - FILTER_ALL → "全部"
    - 真实公司"全部" → "全部（实际公司名）"
    - 普通公司"示例科技A" → "示例科技A"
    - MISSING_LOCATION 不会出现在公司选项，但仍验证不误判
    """
    # FILTER_ALL 哨兵
    assert _company_format_func(FILTER_ALL) == "全部"
    # 真实公司名为"全部"
    assert _company_format_func("全部") == "全部（实际公司名）"
    # 普通公司名
    assert _company_format_func("示例科技A") == "示例科技A"
    # 四个标签互不相同
    labels = {
        _company_format_func(FILTER_ALL),
        _company_format_func("全部"),
        _company_format_func("示例科技A"),
    }
    assert len(labels) == 3


def test_location_format_func_four_labels_distinct():
    """地区字段四种显示标签各不相同且对应正确。

    - MISSING_LOCATION 哨兵 → "未填写（字段为空）"
    - 真实地区"未填写" → "未填写（原始文本）"
    - 普通地区"北京市" → "北京市"
    - FILTER_ALL → "全部"
    """
    # MISSING_LOCATION 哨兵
    assert _location_format_func(MISSING_LOCATION) == "未填写（字段为空）"
    # 真实地区为"未填写"
    assert _location_format_func("未填写") == "未填写（原始文本）"
    # 普通地区
    assert _location_format_func("北京市") == "北京市"
    # FILTER_ALL 哨兵
    assert _location_format_func(FILTER_ALL) == "全部"
    # 四个标签互不相同
    labels = {
        _location_format_func(MISSING_LOCATION),
        _location_format_func("未填写"),
        _location_format_func("北京市"),
        _location_format_func(FILTER_ALL),
    }
    assert len(labels) == 4


def test_generic_format_func_for_record_type_and_status():
    """record_type / status 用通用 format_func，FILTER_ALL 显示"全部"。"""
    assert _filter_format_func(FILTER_ALL) == "全部"
    assert _filter_format_func("campaign") == "campaign"
    assert _filter_format_func("job") == "job"
    assert _filter_format_func("discovered") == "discovered"
    assert _filter_format_func("interview") == "interview"


def test_format_func_sentinel_not_confused_with_real_string():
    """哨兵与真实同名值的显示标签明确区分（端到端验证）。"""
    # 公司：哨兵 vs 真实"全部"
    sentinel_company = _company_format_func(FILTER_ALL)
    real_company = _company_format_func("全部")
    assert sentinel_company != real_company
    assert sentinel_company == "全部"
    assert "实际公司名" in real_company

    # 地区：哨兵 vs 真实"未填写"
    sentinel_loc = _location_format_func(MISSING_LOCATION)
    real_loc = _location_format_func("未填写")
    assert sentinel_loc != real_loc
    assert "字段为空" in sentinel_loc
    assert "原始文本" in real_loc


# ---------------------------------------------------------------------------
# 任务 7 收尾修复一：公司内机会页码选择器
# ---------------------------------------------------------------------------


def test_dashboard_has_company_opp_page_selector():
    """dashboard.py 含公司内机会页码选择器（key=dash_company_opp_page_select）。"""
    source = (PROJECT_ROOT / "pages" / "dashboard.py").read_text(
        encoding="utf-8"
    )
    assert (
        "dash_company_opp_page_select" in source
    ), "应含公司内机会页码选择器"


def test_company_opp_page_selector_1000_opportunities(db_conn):
    """1000 条机会时用户可访问第 2 页和最后一页，每页最多 100 条。"""
    for i in range(1000):
        _insert_opp(
            db_conn,
            company_name="示例科技A",
            source_row=i + 2,
            dedupe_key=f"k_{i}",
        )
    opps = fetch_all_opportunities(db_conn)
    filtered = filter_opportunities(opps, company_name=FILTER_ALL)
    coverage = build_company_coverage(filtered)
    company = coverage[0]
    total = company["total_count"]
    assert total == 1000

    # 页码选择器可生成 1..10 页
    page_info = paginate_list(total, 100, 1)
    page_options = list(range(1, page_info["total_pages"] + 1))
    assert page_options == list(range(1, 11))  # 10 页

    # 用户选择第 2 页
    selected_page = 2
    page_info_p2 = paginate_list(total, 100, selected_page)
    assert page_info_p2["current_page"] == 2
    assert page_info_p2["start"] == 100
    assert page_info_p2["end"] == 200
    page_opps_p2 = company["opportunities"][
        page_info_p2["start"] : page_info_p2["end"]
    ]
    assert len(page_opps_p2) == 100  # 每页最多 100 条

    # 用户选择最后一页（第 10 页）
    last_page = page_info["total_pages"]
    page_info_last = paginate_list(total, 100, last_page)
    assert page_info_last["current_page"] == 10
    assert page_info_last["start"] == 900
    assert page_info_last["end"] == 1000
    page_opps_last = company["opportunities"][
        page_info_last["start"] : page_info_last["end"]
    ]
    assert len(page_opps_last) == 100

    # Top 3 仍只高亮、不截断
    assert len(company["highlighted_top_three"]) == 3
    assert len(company["opportunities"]) == 1000


def test_company_opp_page_clamps_on_filter_or_company_change(db_conn):
    """切换公司或筛选条件后，公司内机会页码自动夹紧到有效范围。"""
    # 示例科技A 200 条，示例制造B 10 条
    for i in range(200):
        _insert_opp(
            db_conn, company_name="示例科技A",
            source_row=i + 2, dedupe_key=f"k_a_{i}",
        )
    for i in range(10):
        _insert_opp(
            db_conn, company_name="示例制造B",
            source_row=i + 202, dedupe_key=f"k_b_{i}",
        )
    opps = fetch_all_opportunities(db_conn)

    # 示例科技A：200 条，页大小 100 → 2 页，用户在第 2 页
    filtered_a = filter_opportunities(opps, company_name="示例科技A")
    coverage_a = build_company_coverage(filtered_a)
    company_a = coverage_a[0]
    info_a = paginate_list(company_a["total_count"], 100, 2)
    assert info_a["current_page"] == 2  # 第 2 页有效

    # 切换到示例制造B：只有 10 条，页码夹紧到 1
    filtered_b = filter_opportunities(opps, company_name="示例制造B")
    coverage_b = build_company_coverage(filtered_b)
    company_b = coverage_b[0]
    info_b = paginate_list(company_b["total_count"], 100, info_a["current_page"])
    assert info_b["current_page"] == 1  # 越界夹紧到 1
    assert info_b["total_pages"] == 1

    # 筛选导致机会数减少：原 200 条在第 2 页，筛选后只剩 10 条 → 夹紧到 1
    info_clamped = paginate_list(10, 100, info_a["current_page"])
    assert info_clamped["current_page"] == 1
