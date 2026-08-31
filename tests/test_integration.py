"""任务 9：持久化与集成测试（端到端流程）。

以完全虚构的测试数据（tests/fixtures/）验证从解析、导入、浏览、状态更新
到关闭并重新打开数据库的完整流程。

覆盖（docs/TASKS.md 任务 9）：
1. 解析虚构多工作表 XLSX 与 CSV；
2. 选择一个工作表导入，包含 campaign、job、unknown 人工确认；
3. 导入报告的新增、重复、无效、待确认数量正确；
4. 同一数据重复导入不会创建重复机会；
5. 导入后可通过 fetch/filter/candidate coverage 浏览全部机会；
6. campaign/job 仍可区分，Top 3 不截断；
7. job 打开链接后变为 opened；
8. 用户手动确认后才变为 applied；
9. 链接打开绝不自动变为 applied；
10. 关闭数据库连接后重新打开同一个 tmp_path 数据库，确认 status、
    opened_at、applied_at、priority 均保留；
11. 预览/浏览/筛选不创建或修改持久数据库；
12. 所有测试产物仅位于 tmp_path，不在仓库留下 .db。

不读取 data/private，不访问网络，不真实打开浏览器（mock webbrowser）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from database.db_handler import get_connection, get_preview_connection, init_db
from services.candidate_service import build_company_coverage
from services.dedup_service import preview_identifier
from services.opportunity_importer import (
    list_workbook_sheets,
    parse_csv,
    parse_workbook_sheet,
)
from services.opportunity_service import (
    FILTER_ALL,
    classify_records,
    confirm_applied,
    fetch_all_opportunities,
    filter_opportunities,
    import_opportunities,
    mark_as_opened,
    set_priority,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
XLSX_PATH = FIXTURES_DIR / "sample_workbook.xlsx"
CSV_PATH = FIXTURES_DIR / "sample_mainland.csv"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _open_db(db_path: Path) -> sqlite3.Connection:
    """打开可写连接并初始化表（仅 tmp_path）。"""
    conn = get_connection(db_path)
    init_db(conn)
    return conn


def _build_campaign_confirmation(record: dict) -> dict:
    """为 unknown 记录构建 campaign 确认条目。"""
    mapping = {
        "company_name": "E",
        "application_url": "N",
        "recruitment_type": "F",
        "target_cohort": "G",
        "education_requirement": "H",
        "location": "J",
        "deadline": "K",
    }
    raw = record.get("raw_data") or {}
    if raw.get("L"):
        mapping["announcement_title"] = "L"
    return {"record_type": "campaign", "field_mapping": mapping}


def _build_job_confirmation(record: dict) -> dict:
    """为 unknown 记录构建 job 确认条目。"""
    mapping = {
        "company_name": "E",
        "job_title": "F",
        "application_url": "N",
        "location": "G",
        "education_requirement": "H",
        "job_categories": "I",
        "deadline": "K",
    }
    return {"record_type": "job", "field_mapping": mapping}


def _get_field(conn: sqlite3.Connection, opp_id: int, field: str) -> object:
    return conn.execute(
        f"SELECT {field} FROM opportunities WHERE id = ?", (opp_id,)
    ).fetchone()[field]


# ---------------------------------------------------------------------------
# 1. 解析虚构多工作表 XLSX 与 CSV
# ---------------------------------------------------------------------------


def test_parse_xlsx_multi_sheet():
    """解析虚构多工作表 XLSX，列出工作表名。"""
    sheets = list_workbook_sheets(str(XLSX_PATH))
    assert "中国大陆" in sheets
    assert "中国香港" in sheets
    assert len(sheets) >= 7


def test_parse_xlsx_selected_sheet():
    """只解析选中的工作表（中国大陆），不加载其他表。"""
    records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
    assert len(records) == 14
    types = {r.get("record_type") for r in records}
    assert "campaign" in types
    assert "job" in types
    assert "unknown" in types


def test_parse_csv_single_sheet():
    """CSV 单表解析。"""
    records = parse_csv(str(CSV_PATH))
    assert len(records) == 14
    types = {r.get("record_type") for r in records}
    assert "campaign" in types
    assert "job" in types


# ---------------------------------------------------------------------------
# 2-3. 导入与导入报告
# ---------------------------------------------------------------------------


def test_import_without_confirmations(tmp_path):
    """无确认导入：campaign+job → new/duplicate/invalid，unknown → pending。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        report = import_opportunities(records, conn)
        counts = report["counts"]
        # 14 条：6 条可靠（campaign 3 + job 3），8 条 unknown
        # 可靠中：row 5 与 row 3 重复 → duplicate
        #         row 11 无有效 URL → invalid
        #         row 12 缺 company → invalid
        # new = 3（row 2 campaign, row 3 job, row 13 job）
        # duplicate = 1（row 5）
        # invalid = 2（row 11, row 12）
        # pending = 8
        assert counts["new"] == 3
        assert counts["duplicate"] == 1
        assert counts["invalid"] == 2
        assert counts["pending"] == 8
        assert sum(counts.values()) == report["total"] == 14
        assert report["inserted"] == 3
    finally:
        conn.close()


def test_import_with_unknown_confirmations(tmp_path):
    """有确认导入：unknown 确认后可入库。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        confirmations = {}
        for r in records:
            if r.get("record_type") == "unknown":
                raw = r.get("raw_data") or {}
                if raw.get("E") == "示例制造B" and raw.get("F") == "其他类别":
                    pid = preview_identifier(r)
                    confirmations[pid] = _build_campaign_confirmation(r)
                    break
        report = import_opportunities(records, conn, confirmations=confirmations)
        counts = report["counts"]
        assert counts["new"] == 4  # 3 + 1 confirmed
        assert counts["pending"] == 7  # 8 - 1 confirmed
        assert report["inserted"] == 4
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. 同一数据重复导入不会创建重复机会
# ---------------------------------------------------------------------------


def test_reimport_no_duplicates(tmp_path):
    """重复导入相同数据不会创建重复机会。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        # 第一次导入
        report1 = import_opportunities(records, conn)
        assert report1["inserted"] == 3
        # 第二次导入相同数据
        report2 = import_opportunities(records, conn)
        # 全部可靠记录变为 duplicate（3 个 DB 级 + 1 个批次内 = 4）
        assert report2["counts"]["new"] == 0
        assert report2["counts"]["duplicate"] == 4
        assert report2["inserted"] == 0
        # 数据库中只有 3 条记录（重复不写入）
        count = int(
            conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        )
        assert count == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. 导入后浏览全部机会
# ---------------------------------------------------------------------------


def test_browse_after_import(tmp_path):
    """导入后可通过 fetch/filter/browse 浏览全部机会。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
        # fetch_all_opportunities
        opps = fetch_all_opportunities(conn)
        assert len(opps) == 3
        # filter_opportunities
        filtered = filter_opportunities(opps, company_name=FILTER_ALL)
        assert len(filtered) == 3
        # candidate coverage
        coverage = build_company_coverage(opps)
        assert len(coverage) >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. campaign/job 区分，Top 3 不截断
# ---------------------------------------------------------------------------


def test_campaign_job_distinguishable_and_top3_not_truncated(tmp_path):
    """campaign/job 仍可区分，Top 3 不截断。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
        opps = fetch_all_opportunities(conn)
        # campaign/job 区分
        types = {o["record_type"] for o in opps}
        assert "campaign" in types
        assert "job" in types
        # company coverage: highlighted_top_three 不截断
        coverage = build_company_coverage(opps)
        for company in coverage:
            assert len(company["opportunities"]) == company["total_count"]
            assert len(company["highlighted_top_three"]) <= 3
            # Top 3 是前 3 条（或全部，如果少于 3）
            top3 = company["highlighted_top_three"]
            all_opps = company["opportunities"]
            for i, top in enumerate(top3):
                assert top["id"] == all_opps[i]["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7-9. 状态流转：opened / applied / 不自动 applied
# ---------------------------------------------------------------------------


def test_full_status_flow(tmp_path):
    """完整状态流转：打开链接 → opened；确认 → applied；不自动 applied。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
        opps = fetch_all_opportunities(conn)
        # 找一个 job 记录
        job = next(o for o in opps if o["record_type"] == "job")
        job_id = job["id"]

        # 7. 打开链接 → opened
        with patch("webbrowser.open") as mock_open:
            result = mark_as_opened(job_id, conn)
            assert result["action"] == "opened"
            assert result["should_open"] is True
            mock_open.assert_not_called()  # 服务层不打开浏览器
        assert _get_field(conn, job_id, "status") == "opened"
        assert _get_field(conn, job_id, "opened_at") is not None

        # 9. 打开链接绝不自动变为 applied
        assert _get_field(conn, job_id, "status") != "applied"
        assert _get_field(conn, job_id, "applied_at") is None

        # 8. 手动确认 → applied
        result = confirm_applied(job_id, conn)
        assert result["action"] == "applied"
        assert _get_field(conn, job_id, "status") == "applied"
        assert _get_field(conn, job_id, "applied_at") is not None
    finally:
        conn.close()


def test_open_link_never_auto_applied(tmp_path):
    """链接打开绝不自动变为 applied（独立验证）。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
        opps = fetch_all_opportunities(conn)
        job = next(o for o in opps if o["record_type"] == "job")
        mark_as_opened(job["id"], conn)
        assert _get_field(conn, job["id"], "status") == "opened"
        assert _get_field(conn, job["id"], "status") != "applied"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10. 关闭并重新打开数据库，验证持久化
# ---------------------------------------------------------------------------


def test_persistence_after_reopen(tmp_path):
    """关闭数据库连接后重新打开，验证 status/opened_at/applied_at/priority 保留。"""
    db_path = tmp_path / "test.db"
    # 第一次打开：导入 + 状态更新 + 优先级
    conn1 = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn1)
        opps = fetch_all_opportunities(conn1)
        job = next(o for o in opps if o["record_type"] == "job")
        job_id = job["id"]

        # 打开链接 → opened
        mark_as_opened(job_id, conn1)
        # 确认 → applied
        confirm_applied(job_id, conn1)
        # 优先级 → high
        set_priority(job_id, "high", conn1)

        # 记录当前值
        status_before = str(_get_field(conn1, job_id, "status"))
        opened_at_before = _get_field(conn1, job_id, "opened_at")
        applied_at_before = _get_field(conn1, job_id, "applied_at")
        priority_before = str(_get_field(conn1, job_id, "priority"))
    finally:
        conn1.close()

    # 第二次打开：验证持久化
    conn2 = _open_db(db_path)
    try:
        opps = fetch_all_opportunities(conn2)
        # 数据数量一致
        assert len(opps) == 3
        # 找到之前更新的 job
        job = next(o for o in opps if o["record_type"] == "job")
        job_id = job["id"]
        assert str(_get_field(conn2, job_id, "status")) == status_before
        assert _get_field(conn2, job_id, "opened_at") == opened_at_before
        assert _get_field(conn2, job_id, "applied_at") == applied_at_before
        assert str(_get_field(conn2, job_id, "priority")) == priority_before
        assert priority_before == "high"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# 11. 预览/浏览/筛选不创建或修改持久数据库
# ---------------------------------------------------------------------------


def test_preview_does_not_create_db(tmp_path):
    """预览不创建持久数据库文件。"""
    db_path = tmp_path / "nonexistent.db"
    assert not db_path.exists()
    # 预览连接
    preview_conn = get_preview_connection(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        # 预览分类（零写入）
        classify_records(records, preview_conn)
    finally:
        preview_conn.close()
    # 文件仍然不存在
    assert not db_path.exists()


def test_browse_does_not_modify_db(tmp_path):
    """浏览/筛选不修改已存在数据库的记录。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
    finally:
        conn.close()

    # 用预览连接浏览
    preview_conn = get_preview_connection(db_path)
    try:
        opps = fetch_all_opportunities(preview_conn)
        filter_opportunities(opps, company_name=FILTER_ALL)
        build_company_coverage(opps)
    finally:
        preview_conn.close()

    # 重新打开可写连接验证记录未被修改
    conn2 = _open_db(db_path)
    try:
        opps = fetch_all_opportunities(conn2)
        assert len(opps) == 3
        for o in opps:
            assert o["status"] == "discovered"
            assert o["priority"] == "low"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# 12. 不读取 data/private + 不产生仓库残留
# ---------------------------------------------------------------------------


def test_no_data_private_access():
    """集成测试相关源码不引用 data/private。"""
    for path in [
        PROJECT_ROOT / "services" / "opportunity_service.py",
        PROJECT_ROOT / "services" / "opportunity_importer.py",
        PROJECT_ROOT / "pages" / "dashboard.py",
        PROJECT_ROOT / "pages" / "import_page.py",
    ]:
        if path.exists():
            source = path.read_text(encoding="utf-8")
            assert "data/private" not in source
            assert "智联-岗位信息表" not in source


def test_no_db_in_repo(tmp_path):
    """测试数据库仅位于 tmp_path，不在仓库留下 .db。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_workbook_sheet(str(XLSX_PATH), "中国大陆")
        import_opportunities(records, conn)
    finally:
        conn.close()
    # 数据库在 tmp_path 中
    assert db_path.exists()
    # 仓库目录中不应有 .db 文件
    repo_db_files = list(PROJECT_ROOT.glob("*.db")) + list(
        PROJECT_ROOT.glob("data/*.db")
    )
    assert len(repo_db_files) == 0


# ---------------------------------------------------------------------------
# CSV 完整流程
# ---------------------------------------------------------------------------


def test_csv_import_and_browse(tmp_path):
    """CSV 单表预览与导入。"""
    db_path = tmp_path / "test.db"
    conn = _open_db(db_path)
    try:
        records = parse_csv(str(CSV_PATH))
        # 预览分类（零写入）
        preview_conn = get_preview_connection(":memory:")
        try:
            classify_records(records, preview_conn)
        finally:
            preview_conn.close()
        # 正式导入
        report = import_opportunities(records, conn)
        assert report["inserted"] == 3
        # 浏览
        opps = fetch_all_opportunities(conn)
        assert len(opps) == 3
    finally:
        conn.close()
