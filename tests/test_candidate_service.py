"""candidate_service 单元测试（任务 6）。

覆盖任务 6 指令中的全部必测项：
- 空数据库返回空列表；
- 单公司 1/2/3/5 条机会的 gap 与 total_count；
- high/medium/low 排序；同优先级 job 优先于 campaign；同优先级同类型
  按 id 升序；
- 多公司分组互不混合；公司顺序确定；
- 只有 campaign → campaign_only=True + 提示；campaign+job → False；
  只有 job → False；
- 全部合法 status 都不被过滤；
- highlighted_top_three 与 opportunities[:3] 一致；不截断；
- total_count 始终等于完整列表长度；coverage_gap 永不小于 0；
- 返回值为普通 dict/list；函数不写数据库；
- mark_campaign_only_companies 不修改调用方原始输入；
- 不访问 data/private；不创建数据库文件或测试残留。

数据来源：完全虚构公司（示例科技A / 示例制造B / 示例银行C）、岗位与
example.com 链接；测试数据库一律使用 :memory: 或 tmp_path。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from database.db_handler import get_connection, init_db
from services.candidate_service import (
    get_company_coverage,
    mark_campaign_only_companies,
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


def _company(coverage: list[dict], name: str) -> dict:
    """从覆盖结果中取出指定公司。"""
    return next(c for c in coverage if c["company_name"] == name)


# ---------------------------------------------------------------------------
# 空数据库
# ---------------------------------------------------------------------------


def test_empty_database_returns_empty_list(db_conn):
    """空数据库返回空列表。"""
    assert get_company_coverage(db_conn) == []


# ---------------------------------------------------------------------------
# coverage_gap 与 total_count
# ---------------------------------------------------------------------------


def test_single_company_one_opportunity_gap_2(db_conn):
    """单公司 1 条机会：gap=2。"""
    _insert_opp(db_conn, source_row=2)
    coverage = get_company_coverage(db_conn)
    assert len(coverage) == 1
    company = coverage[0]
    assert company["total_count"] == 1
    assert company["coverage_gap"] == 2


def test_single_company_two_opportunities_gap_1(db_conn):
    """单公司 2 条机会：gap=1。"""
    _insert_opp(db_conn, source_row=2)
    _insert_opp(db_conn, source_row=3, dedupe_key="k_camp_a_3")
    company = get_company_coverage(db_conn)[0]
    assert company["total_count"] == 2
    assert company["coverage_gap"] == 1


def test_single_company_three_opportunities_gap_0(db_conn):
    """单公司 3 条机会：gap=0。"""
    for i in range(2, 5):
        _insert_opp(db_conn, source_row=i, dedupe_key=f"k_camp_a_{i}")
    company = get_company_coverage(db_conn)[0]
    assert company["total_count"] == 3
    assert company["coverage_gap"] == 0


def test_single_company_five_opportunities_not_truncated(db_conn):
    """单公司 5 条机会：total_count=5，opportunities 长度仍为 5，
    highlighted_top_three 长度为 3，第 4、5 条仍可访问。"""
    for i in range(2, 7):
        _insert_opp(db_conn, source_row=i, dedupe_key=f"k_camp_a_{i}")
    company = get_company_coverage(db_conn)[0]
    assert company["total_count"] == 5
    assert len(company["opportunities"]) == 5
    assert len(company["highlighted_top_three"]) == 3
    # 第 4、5 条仍可访问
    assert company["opportunities"][3]["source_row"] == 5
    assert company["opportunities"][4]["source_row"] == 6
    assert company["coverage_gap"] == 0


# ---------------------------------------------------------------------------
# 排序规则
# ---------------------------------------------------------------------------


def test_priority_sort_high_medium_low(db_conn):
    """high > medium > low 排序正确（不按字符串字母顺序）。"""
    _insert_opp(db_conn, priority="low", source_row=2, dedupe_key="k_low")
    _insert_opp(db_conn, priority="high", source_row=3, dedupe_key="k_high")
    _insert_opp(db_conn, priority="medium", source_row=4, dedupe_key="k_med")
    company = get_company_coverage(db_conn)[0]
    priorities = [opp["priority"] for opp in company["opportunities"]]
    assert priorities == ["high", "medium", "low"]


def test_job_before_campaign_same_priority(db_conn):
    """同优先级 job 优先于 campaign。"""
    _insert_opp(
        db_conn, record_type="campaign", priority="medium",
        source_row=2, dedupe_key="k_camp",
    )
    _insert_opp(
        db_conn, record_type="job", priority="medium",
        source_row=3, dedupe_key="k_job",
    )
    company = get_company_coverage(db_conn)[0]
    types = [opp["record_type"] for opp in company["opportunities"]]
    assert types == ["job", "campaign"]


def test_same_priority_same_type_by_id_ascending(db_conn):
    """同优先级同类型按 id 升序（先导入的优先）。"""
    _insert_opp(
        db_conn, record_type="job", priority="low",
        source_row=2, dedupe_key="k_job_2",
    )
    _insert_opp(
        db_conn, record_type="job", priority="low",
        source_row=3, dedupe_key="k_job_3",
    )
    _insert_opp(
        db_conn, record_type="job", priority="low",
        source_row=4, dedupe_key="k_job_4",
    )
    company = get_company_coverage(db_conn)[0]
    ids = [opp["id"] for opp in company["opportunities"]]
    assert ids == sorted(ids)


def test_combined_sort_priority_then_type_then_id(db_conn):
    """综合排序：priority > record_type > id。"""
    # low campaign (id=1) → 最后
    _insert_opp(
        db_conn, record_type="campaign", priority="low",
        source_row=2, dedupe_key="k_1",
    )
    # medium campaign (id=2) → 第3
    _insert_opp(
        db_conn, record_type="campaign", priority="medium",
        source_row=3, dedupe_key="k_2",
    )
    # medium job (id=3) → 第2
    _insert_opp(
        db_conn, record_type="job", priority="medium",
        source_row=4, dedupe_key="k_3",
    )
    # high campaign (id=4) → 第1（high campaign 优先于 medium job）
    _insert_opp(
        db_conn, record_type="campaign", priority="high",
        source_row=5, dedupe_key="k_4",
    )
    company = get_company_coverage(db_conn)[0]
    opps = company["opportunities"]
    assert opps[0]["priority"] == "high"
    assert opps[0]["record_type"] == "campaign"
    assert opps[1]["priority"] == "medium"
    assert opps[1]["record_type"] == "job"
    assert opps[2]["priority"] == "medium"
    assert opps[2]["record_type"] == "campaign"
    assert opps[3]["priority"] == "low"


# ---------------------------------------------------------------------------
# 多公司分组与公司顺序
# ---------------------------------------------------------------------------


def test_multiple_companies_not_mixed(db_conn):
    """多公司分组互不混合。"""
    _insert_opp(db_conn, company_name="示例科技A", source_row=2, dedupe_key="k_a1")
    _insert_opp(db_conn, company_name="示例制造B", source_row=3, dedupe_key="k_b1")
    _insert_opp(db_conn, company_name="示例科技A", source_row=4, dedupe_key="k_a2")
    coverage = get_company_coverage(db_conn)
    assert len(coverage) == 2
    a = _company(coverage, "示例科技A")
    b = _company(coverage, "示例制造B")
    assert a["total_count"] == 2
    assert b["total_count"] == 1
    # 不混合
    assert {opp["company_name"] for opp in a["opportunities"]} == {"示例科技A"}
    assert {opp["company_name"] for opp in b["opportunities"]} == {"示例制造B"}


def test_company_order_deterministic(db_conn):
    """公司顺序按规范化后的 company_name 升序确定（Unicode 码点序）。"""
    _insert_opp(db_conn, company_name="示例制造B", source_row=2, dedupe_key="k_b")
    _insert_opp(db_conn, company_name="示例科技A", source_row=3, dedupe_key="k_a")
    _insert_opp(db_conn, company_name="示例银行C", source_row=4, dedupe_key="k_c")
    coverage = get_company_coverage(db_conn)
    names = [c["company_name"] for c in coverage]
    # 按 Unicode 码点排序：制(U+5236) < 科(U+79D1) < 银(U+94F6)
    assert names == ["示例制造B", "示例科技A", "示例银行C"]


def test_company_name_trimmed_for_grouping(db_conn):
    """公司名分组时执行首尾空格清理，不修改数据库原值。"""
    _insert_opp(db_conn, company_name="  示例科技A  ", source_row=2, dedupe_key="k_1")
    _insert_opp(db_conn, company_name="示例科技A", source_row=3, dedupe_key="k_2")
    coverage = get_company_coverage(db_conn)
    # 两条被归为同一家公司（strip 后相同）
    assert len(coverage) == 1
    assert coverage[0]["company_name"] == "示例科技A"  # 归一化后的名
    assert coverage[0]["total_count"] == 2


def test_company_order_case_sensitive():
    """公司名排序大小写敏感（稳定选择，用测试固定）。"""
    conn = get_connection(":memory:")
    init_db(conn)
    _insert_opp(conn, company_name="abc", source_row=2, dedupe_key="k_lower")
    _insert_opp(conn, company_name="ABC", source_row=3, dedupe_key="k_upper")
    coverage = get_company_coverage(conn)
    names = [c["company_name"] for c in coverage]
    # 大写字母 ASCII < 小写字母，大小写敏感时 ABC 排在 abc 前
    assert names == ["ABC", "abc"]
    conn.close()


# ---------------------------------------------------------------------------
# campaign_only
# ---------------------------------------------------------------------------


def test_campaign_only_true_with_message(db_conn):
    """只有 campaign → campaign_only=True + 提示文字正确。"""
    _insert_opp(
        db_conn, record_type="campaign", source_row=2, dedupe_key="k_c1"
    )
    _insert_opp(
        db_conn, record_type="campaign", source_row=3, dedupe_key="k_c2"
    )
    company = get_company_coverage(db_conn)[0]
    assert company["campaign_only"] is True
    assert company["campaign_only_message"] == "需进入官网选择具体岗位"


def test_mixed_campaign_job_campaign_only_false(db_conn):
    """campaign+job → campaign_only=False，无提示。"""
    _insert_opp(
        db_conn, record_type="campaign", source_row=2, dedupe_key="k_c1"
    )
    _insert_opp(
        db_conn, record_type="job", source_row=3, dedupe_key="k_j1"
    )
    company = get_company_coverage(db_conn)[0]
    assert company["campaign_only"] is False
    assert company["campaign_only_message"] is None


def test_only_job_campaign_only_false(db_conn):
    """只有 job → campaign_only=False。"""
    _insert_opp(db_conn, record_type="job", source_row=2, dedupe_key="k_j1")
    company = get_company_coverage(db_conn)[0]
    assert company["campaign_only"] is False
    assert company["campaign_only_message"] is None


# ---------------------------------------------------------------------------
# 全部 status 不被过滤
# ---------------------------------------------------------------------------


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
def test_all_statuses_not_filtered(db_conn, status):
    """discovered/shortlisted/opened/applied/interview 等状态都不会被错误过滤。"""
    _insert_opp(
        db_conn, status=status, source_row=2, dedupe_key=f"k_{status}"
    )
    coverage = get_company_coverage(db_conn)
    assert len(coverage) == 1
    assert coverage[0]["total_count"] == 1


def test_multiple_statuses_in_one_company(db_conn):
    """同一公司不同状态的机会全部保留。"""
    for i, status in enumerate(
        ["discovered", "shortlisted", "opened", "applied", "interview"], start=2
    ):
        _insert_opp(
            db_conn, status=status, source_row=i, dedupe_key=f"k_{status}"
        )
    company = get_company_coverage(db_conn)[0]
    assert company["total_count"] == 5
    statuses = {opp["status"] for opp in company["opportunities"]}
    assert statuses == {"discovered", "shortlisted", "opened", "applied", "interview"}


# ---------------------------------------------------------------------------
# Top 3 一致性 / 不截断
# ---------------------------------------------------------------------------


def test_highlighted_top_three_equals_opportunities_slice(db_conn):
    """highlighted_top_three 与 opportunities[:3] 一致。"""
    for i in range(2, 8):
        _insert_opp(
            db_conn, source_row=i, priority="low", dedupe_key=f"k_{i}"
        )
    company = get_company_coverage(db_conn)[0]
    top_three = company["highlighted_top_three"]
    slice_three = company["opportunities"][:3]
    assert [opp["id"] for opp in top_three] == [opp["id"] for opp in slice_three]


def test_highlighted_top_three_fewer_than_three(db_conn):
    """少于 3 条时 highlighted_top_three 返回实际数量。"""
    _insert_opp(db_conn, source_row=2, dedupe_key="k_1")
    company = get_company_coverage(db_conn)[0]
    assert len(company["highlighted_top_three"]) == 1
    assert company["coverage_gap"] == 2


def test_opportunities_not_truncated_by_top_three(db_conn):
    """Top 3 不得从 opportunities 中删除其余记录。"""
    for i in range(2, 7):
        _insert_opp(db_conn, source_row=i, dedupe_key=f"k_{i}")
    company = get_company_coverage(db_conn)[0]
    assert len(company["opportunities"]) == 5
    assert len(company["highlighted_top_three"]) == 3


# ---------------------------------------------------------------------------
# 不变量
# ---------------------------------------------------------------------------


def test_total_count_always_equals_full_list_length(db_conn):
    """total_count 始终等于完整列表长度。"""
    _insert_opp(db_conn, company_name="示例科技A", source_row=2, dedupe_key="k_a1")
    _insert_opp(db_conn, company_name="示例科技A", source_row=3, dedupe_key="k_a2")
    _insert_opp(db_conn, company_name="示例制造B", source_row=4, dedupe_key="k_b1")
    for company in get_company_coverage(db_conn):
        assert company["total_count"] == len(company["opportunities"])


def test_coverage_gap_never_negative(db_conn):
    """coverage_gap 永不小于 0。"""
    for i in range(2, 7):
        _insert_opp(db_conn, source_row=i, dedupe_key=f"k_{i}")
    company = get_company_coverage(db_conn)[0]
    assert company["coverage_gap"] == 0  # 5 条，gap=0，不会变成负数


def test_return_values_are_plain_dict_and_list(db_conn):
    """返回值为普通 dict/list（不把 sqlite3.Row 暴露给 UI）。"""
    _insert_opp(db_conn, source_row=2, dedupe_key="k_1")
    coverage = get_company_coverage(db_conn)
    assert isinstance(coverage, list)
    company = coverage[0]
    assert isinstance(company, dict)
    assert isinstance(company["opportunities"], list)
    assert isinstance(company["opportunities"][0], dict)
    assert isinstance(company["highlighted_top_three"], list)
    assert isinstance(company["highlighted_top_three"][0], dict)
    # 确认不是 sqlite3.Row
    assert not isinstance(company["opportunities"][0], sqlite3.Row)


def test_get_company_coverage_does_not_write_db(db_conn):
    """函数不写数据库（调用前后记录数不变）。"""
    _insert_opp(db_conn, source_row=2, dedupe_key="k_1")
    before = _db_count(db_conn)
    get_company_coverage(db_conn)
    after = _db_count(db_conn)
    assert before == after == 1


def _db_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])


# ---------------------------------------------------------------------------
# mark_campaign_only_companies
# ---------------------------------------------------------------------------


def test_mark_campaign_only_adds_message(db_conn):
    """campaign_only=True 时添加提示。"""
    _insert_opp(
        db_conn, record_type="campaign", source_row=2, dedupe_key="k_c1"
    )
    coverage = get_company_coverage(db_conn)
    marked = mark_campaign_only_companies(coverage)
    assert marked[0]["campaign_only_message"] == "需进入官网选择具体岗位"


def test_mark_campaign_only_no_message_for_mixed(db_conn):
    """混合 campaign/job 时不添加提示。"""
    _insert_opp(
        db_conn, record_type="campaign", source_row=2, dedupe_key="k_c1"
    )
    _insert_opp(
        db_conn, record_type="job", source_row=3, dedupe_key="k_j1"
    )
    coverage = get_company_coverage(db_conn)
    marked = mark_campaign_only_companies(coverage)
    assert marked[0]["campaign_only_message"] is None


def test_mark_campaign_only_idempotent():
    """不重复追加相同提示（幂等）。"""
    coverage = [
        {
            "company_name": "示例科技A",
            "opportunities": [],
            "total_count": 0,
            "coverage_gap": 3,
            "highlighted_top_three": [],
            "campaign_only": True,
            "campaign_only_message": "需进入官网选择具体岗位",
        }
    ]
    marked = mark_campaign_only_companies(coverage)
    assert marked[0]["campaign_only_message"] == "需进入官网选择具体岗位"
    # 再次调用仍只有一条提示
    marked2 = mark_campaign_only_companies(marked)
    assert marked2[0]["campaign_only_message"] == "需进入官网选择具体岗位"


def test_mark_campaign_only_does_not_modify_input():
    """mark_campaign_only_companies 不修改调用方原始输入。"""
    original = [
        {
            "company_name": "示例科技A",
            "opportunities": [{"id": 1, "record_type": "campaign"}],
            "total_count": 1,
            "coverage_gap": 2,
            "highlighted_top_three": [{"id": 1, "record_type": "campaign"}],
            "campaign_only": True,
            "campaign_only_message": None,  # 原始无提示
        }
    ]
    marked = mark_campaign_only_companies(original)
    # 原始输入未被修改
    assert original[0]["campaign_only_message"] is None
    # 返回的新结构有提示
    assert marked[0]["campaign_only_message"] == "需进入官网选择具体岗位"


def test_mark_campaign_only_preserves_opportunities():
    """mark 不删除任何机会。"""
    opps = [{"id": i, "record_type": "campaign"} for i in range(5)]
    coverage = [
        {
            "company_name": "示例科技A",
            "opportunities": list(opps),
            "total_count": 5,
            "coverage_gap": 0,
            "highlighted_top_three": opps[:3],
            "campaign_only": True,
            "campaign_only_message": None,
        }
    ]
    marked = mark_campaign_only_companies(coverage)
    assert len(marked[0]["opportunities"]) == 5
    assert len(marked[0]["highlighted_top_three"]) == 3


# ---------------------------------------------------------------------------
# 安全边界
# ---------------------------------------------------------------------------


def test_no_data_private_access_in_source():
    """service 源码不得引用 data/private 或真实工作簿文件名。"""
    source = (
        PROJECT_ROOT / "services" / "candidate_service.py"
    ).read_text(encoding="utf-8")
    assert "data/private" not in source
    assert "智联-岗位信息表" not in source


def test_service_does_not_reference_db_path():
    """service 不引用默认数据库路径（连接由调用方传入）。"""
    source = (
        PROJECT_ROOT / "services" / "candidate_service.py"
    ).read_text(encoding="utf-8")
    assert "DB_PATH" not in source
    assert "get_connection" not in source  # 不自行建连接


def test_no_db_file_residue(tmp_path):
    """使用 :memory: 不产生数据库文件残留。"""
    db_file = tmp_path / "residue_check.db"
    assert not db_file.exists()
    conn = get_connection(":memory:")
    init_db(conn)
    _insert_opp(conn, source_row=2, dedupe_key="k_1")
    get_company_coverage(conn)
    conn.close()
    assert not db_file.exists()  # 未在 tmp_path 产生文件
