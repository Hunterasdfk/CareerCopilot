"""opportunity_service 单元测试（任务 5）。

覆盖任务 5 指令中的必测项：
- 预览阶段数据库零写入；明确调用 import_opportunities 后才写库；
- campaign / job 正常新增（默认 status=discovered、priority=low）；
- unknown 未确认保持 pending 且不入库；明确确认并完成映射后可入库；
- suggested_record_type 仅为建议（用户确认优先于建议）；
- 映射不完整 → pending（明确规则）；缺必填字段 / 无效 URL / 非白名单
  映射目标 → invalid；
- 数据库已有重复；同一批次内部重复；重复记录不覆盖原记录；
- 四类统计互斥且总数闭合；
- 事务异常回滚；
- raw_data / source_sheet / source_row 保留；record_type CHECK 仍拒绝 unknown；
- XLSX 工作表选择只解析选中表；CSV 单表预览；
- 不读取 data/private；不在仓库产生上传文件或测试数据库。

任务 5 修正新增覆盖：
- 预览不创建持久数据库文件（只读连接或 :memory:）；
- display_title 尊重用户显式映射；
- 来源字段与 raw_data 在预览阶段验证（不先判 new 再抛 TypeError）；
- unknown 分页（页大小上限、页数计算、跨页确认保留、不串数据）。

数据来源：任务 4 的完全虚构夹具（示例科技A / 示例制造B / 示例银行C /
example.com）；测试数据库一律使用 tmp_path，不落仓库目录。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from database.db_handler import (
    get_connection,
    get_preview_connection,
    init_db,
)
from services.dedup_service import preview_identifier
from services.opportunity_importer import parse_csv, parse_workbook_sheet
from services.opportunity_service import (
    CATEGORY_DUPLICATE,
    CATEGORY_INVALID,
    CATEGORY_NEW,
    CATEGORY_PENDING,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAPPABLE_FIELDS,
    classify_records,
    collect_confirmations,
    count_confirmed,
    import_opportunities,
    paginate_unknown,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
XLSX_FIXTURE = FIXTURES_DIR / "sample_workbook.xlsx"
CSV_FIXTURE = FIXTURES_DIR / "sample_mainland.csv"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path):
    """tmp_path 下的临时 SQLite 数据库（绝不落仓库目录）。"""
    conn = get_connection(tmp_path / "test_opportunities.db")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def mainland_records() -> list[dict[str, Any]]:
    """任务 4 虚构夹具的"中国大陆"工作表（14 条非空记录）。"""
    return parse_workbook_sheet(XLSX_FIXTURE, "中国大陆")


def _db_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])


def _confirm(
    records: list[dict[str, Any]],
    source_row: int,
    record_type: str,
    field_mapping: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """按 source_row 构造确认条目（键为 preview_id）。"""
    target = next(r for r in records if r.get("source_row") == source_row)
    return {
        preview_identifier(target): {
            "record_type": record_type,
            "field_mapping": field_mapping,
        }
    }


def _item_at(items: list[dict], source_row: int) -> dict:
    return next(i for i in items if i["record"].get("source_row") == source_row)


# ---------------------------------------------------------------------------
# 预览零写入 / 显式导入
# ---------------------------------------------------------------------------


def test_preview_writes_nothing_to_db(db_conn, mainland_records):
    """预览阶段（classify_records）数据库零写入。"""
    result = classify_records(mainland_records, db_conn)
    assert _db_count(db_conn) == 0
    # 中国大陆夹具：3 new（row2/3/13）+ 1 批次重复（row5）
    # + 2 invalid（row11 无有效链接、row12 缺公司）+ 8 pending（unknown）
    assert result["counts"] == {
        CATEGORY_NEW: 3,
        CATEGORY_DUPLICATE: 1,
        CATEGORY_INVALID: 2,
        CATEGORY_PENDING: 8,
    }


def test_import_only_after_explicit_call(db_conn, mainland_records):
    """只有显式调用 import_opportunities 后才写库。"""
    classify_records(mainland_records, db_conn)
    assert _db_count(db_conn) == 0  # 预览零写入
    report = import_opportunities(mainland_records, db_conn)
    assert report["inserted"] == 3
    assert _db_count(db_conn) == 3


# ---------------------------------------------------------------------------
# campaign / job 正常新增
# ---------------------------------------------------------------------------


def test_campaign_new_inserted_with_defaults(db_conn, mainland_records):
    """campaign 正常新增，默认 status=discovered、priority=low。"""
    report = import_opportunities(mainland_records, db_conn)
    row = db_conn.execute(
        "SELECT * FROM opportunities "
        "WHERE record_type='campaign' AND company_name='示例科技A'"
    ).fetchone()
    assert row is not None
    assert row["display_title"] == "示例科技A 2026秋季校园招聘"
    assert row["recruitment_type"] == "秋招全职"
    assert row["target_cohort"] == "2026届"
    assert row["dedupe_key"].startswith("campaign_")
    assert row["status"] == "discovered"
    assert row["priority"] == "low"
    assert row["import_batch_id"] == report["batch_id"]
    assert row["source_sheet"] == "中国大陆"
    assert row["source_row"] == 2


def test_job_new_inserted(db_conn, mainland_records):
    """job 正常新增。"""
    import_opportunities(mainland_records, db_conn)
    row = db_conn.execute(
        "SELECT * FROM opportunities "
        "WHERE record_type='job' AND job_title='示例后端开发工程师'"
    ).fetchone()
    assert row is not None
    assert row["company_name"] == "示例银行C"
    assert row["location"] == "上海市"
    assert row["application_url"] == "https://example.com/apply/c-2026-4567"
    assert row["dedupe_key"].startswith("job_")
    assert row["source_row"] == 3


# ---------------------------------------------------------------------------
# unknown 人工确认
# ---------------------------------------------------------------------------


def test_unknown_unconfirmed_stays_pending_and_not_inserted(
    db_conn, mainland_records
):
    """unknown 未确认保持 pending，绝不入库。"""
    report = import_opportunities(mainland_records, db_conn)
    pending = report["items"][CATEGORY_PENDING]
    assert len(pending) == 8
    assert all(i["record"]["record_type"] == "unknown" for i in pending)
    assert _db_count(db_conn) == 3  # 只有 campaign/job 入库


def test_unknown_confirmed_can_be_imported(db_conn, mainland_records):
    """unknown 明确确认并完成映射后可以入库。"""
    confirmations = _confirm(
        mainland_records,
        14,
        "campaign",
        {
            "company_name": "E",
            "announcement_title": "L",
            "announcement_url": "M",
            "application_url": "N",
        },
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    assert result["counts"][CATEGORY_PENDING] == 7  # 14 行已确认
    assert result["counts"][CATEGORY_NEW] == 4
    report = import_opportunities(mainland_records, db_conn, confirmations)
    assert report["inserted"] == 4
    row = db_conn.execute(
        "SELECT * FROM opportunities WHERE company_name='示例科技A' AND source_row=14"
    ).fetchone()
    assert row is not None
    assert row["record_type"] == "campaign"
    assert row["display_title"] == "示例科技A 校招"
    assert row["announcement_url"] == "https://example.com/ann/a-other"


def test_user_confirmation_overrides_suggestion(db_conn):
    """suggested_record_type 只是建议：用户确认 campaign 则以确认为准。"""
    hk_records = parse_workbook_sheet(XLSX_FIXTURE, "中国香港")
    target = next(r for r in hk_records if r.get("source_row") == 2)
    assert target["suggested_record_type"] == "job"  # 系统建议 job
    confirmations = {
        preview_identifier(target): {
            "record_type": "campaign",  # 用户主动确认 campaign
            "field_mapping": {
                "company_name": "C",
                "recruitment_type": "F",
                "target_cohort": "G",
                "education_requirement": "H",
                "application_url": "J",
            },
        }
    }
    report = import_opportunities(hk_records, db_conn, confirmations)
    assert report["inserted"] == 1  # 其余 unknown 均保持 pending
    row = db_conn.execute(
        "SELECT * FROM opportunities WHERE company_name='示例科技A' AND source_row=2"
    ).fetchone()
    assert row is not None
    assert row["record_type"] == "campaign"  # 用户确认优先于建议
    assert row["dedupe_key"].startswith("campaign_")


def test_cancelled_confirmation_stays_pending(db_conn, mainland_records):
    """取消确认（确认类型非 campaign/job）→ 保持 pending。"""
    confirmations = _confirm(
        mainland_records, 14, "unknown", {"company_name": "E"}
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    rows_pending = {
        i["record"]["source_row"] for i in result["items"][CATEGORY_PENDING]
    }
    assert 14 in rows_pending


# ---------------------------------------------------------------------------
# invalid：映射不完整（pending）与缺必填字段 / 无效 URL（invalid）
# ---------------------------------------------------------------------------


def test_incomplete_mapping_stays_pending(db_conn, mainland_records):
    """映射不完整（job 确认缺 job_title 映射）→ pending，等待补全。"""
    confirmations = _confirm(
        mainland_records, 7, "job", {"company_name": "E", "application_url": "N"}
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    item = _item_at(result["items"][CATEGORY_PENDING], 7)
    assert "映射不完整" in item["reason"]
    report = import_opportunities(mainland_records, db_conn, confirmations)
    assert report["counts"][CATEGORY_PENDING] == 8  # 7 未确认 + 1 映射不完整
    assert _db_count(db_conn) == 3


def test_missing_company_name_is_invalid(db_conn, mainland_records):
    """可靠布局解析出的记录缺 company_name → invalid（row12 E 列为空）。"""
    result = classify_records(mainland_records, db_conn)
    item = _item_at(result["items"][CATEGORY_INVALID], 12)
    assert "company_name" in item["reason"]


def test_confirmed_job_with_empty_job_title_is_invalid(db_conn, mainland_records):
    """映射结构完整但 job_title 取值为空（映射到空列 J）→ invalid。"""
    confirmations = _confirm(
        mainland_records,
        14,
        "job",
        {"company_name": "E", "job_title": "J", "application_url": "N"},
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    item = _item_at(result["items"][CATEGORY_INVALID], 14)
    assert "job_title" in item["reason"]


def test_invalid_url_is_invalid(db_conn, mainland_records):
    """无效 URL：解析阶段置空导致无有效链接 → invalid；确认映射到非 http 值 → invalid。"""
    result = classify_records(mainland_records, db_conn)
    # (a) row11：M/N 为"见公告/官网投递"，解析置空后无有效链接
    item11 = _item_at(result["items"][CATEGORY_INVALID], 11)
    assert "链接" in item11["reason"]

    # (b) row9：确认映射 application_url → K（"2026-09-30"，非 http）
    confirmations = _confirm(
        mainland_records,
        9,
        "job",
        {"company_name": "E", "job_title": "F", "application_url": "K"},
    )
    result2 = classify_records(mainland_records, db_conn, confirmations)
    item9 = _item_at(result2["items"][CATEGORY_INVALID], 9)
    assert "application_url" in item9["reason"]


def test_mapping_allowlist_rejects_arbitrary_columns(db_conn, mainland_records):
    """映射目标不在 MAPPABLE_FIELDS 白名单（如 status）→ invalid。"""
    assert "status" not in MAPPABLE_FIELDS
    assert "dedupe_key" not in MAPPABLE_FIELDS
    confirmations = _confirm(
        mainland_records,
        14,
        "campaign",
        {"company_name": "E", "application_url": "N", "status": "A"},
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    item = _item_at(result["items"][CATEGORY_INVALID], 14)
    assert "允许列表" in item["reason"]


# ---------------------------------------------------------------------------
# 重复：数据库重复 / 批次内重复 / 不覆盖原记录
# ---------------------------------------------------------------------------


def test_db_duplicate_detected_on_second_import(db_conn, mainland_records):
    """数据库已有重复：第二次导入全部改判 duplicate，不写入。"""
    import_opportunities(mainland_records, db_conn)
    again = parse_workbook_sheet(XLSX_FIXTURE, "中国大陆")
    result = classify_records(again, db_conn)
    assert result["counts"][CATEGORY_NEW] == 0
    assert result["counts"][CATEGORY_DUPLICATE] == 4  # row2/3/13 入库 + row5
    report = import_opportunities(again, db_conn)
    assert report["inserted"] == 0
    assert _db_count(db_conn) == 3


def test_batch_internal_duplicate_detected(db_conn, mainland_records):
    """同一批次内部重复：row5 与 row3 业务内容相同 → duplicate。"""
    result = classify_records(mainland_records, db_conn)
    rows_new = {
        i["record"]["source_row"] for i in result["items"][CATEGORY_NEW]
    }
    rows_dup = {
        i["record"]["source_row"] for i in result["items"][CATEGORY_DUPLICATE]
    }
    assert 3 in rows_new
    assert 5 in rows_dup


def test_duplicate_does_not_overwrite_existing_record(db_conn, mainland_records):
    """重复记录不覆盖原记录的 status/priority/notes 等字段。"""
    import_opportunities(mainland_records, db_conn)
    db_conn.execute(
        "UPDATE opportunities SET status='shortlisted', priority='high', "
        "notes='手动备注' WHERE source_row=3"
    )
    db_conn.commit()
    report = import_opportunities(mainland_records, db_conn)  # 再次导入
    assert report["inserted"] == 0
    row = db_conn.execute(
        "SELECT * FROM opportunities WHERE source_row=3"
    ).fetchone()
    assert row["status"] == "shortlisted"  # 原记录未被覆盖
    assert row["priority"] == "high"
    assert row["notes"] == "手动备注"
    assert _db_count(db_conn) == 3


# ---------------------------------------------------------------------------
# 统计闭合 / 事务 / 数据保留
# ---------------------------------------------------------------------------


def test_categories_exclusive_and_total_closes(db_conn, mainland_records):
    """四类互斥且数量之和等于参与预览的非空记录总数。"""
    confirmations = _confirm(
        mainland_records,
        14,
        "campaign",
        {
            "company_name": "E",
            "announcement_title": "L",
            "announcement_url": "M",
        },
    )
    result = classify_records(mainland_records, db_conn, confirmations)
    counts = result["counts"]
    assert sum(counts.values()) == result["total"] == len(mainland_records)
    seen: list[str] = []
    for category in (CATEGORY_NEW, CATEGORY_DUPLICATE, CATEGORY_INVALID, CATEGORY_PENDING):
        for item in result["items"][category]:
            seen.append(item["preview_id"])
    assert len(seen) == len(set(seen)) == result["total"]  # 互斥且无遗漏


def test_transaction_rollback_on_error(db_conn, mainland_records):
    """写入中途异常 → 事务整体回滚，数据库不留任何数据。"""
    # 用触发器强制某一公司 INSERT 失败（确定性异常）
    db_conn.execute(
        """
        CREATE TRIGGER force_fail BEFORE INSERT ON opportunities
        WHEN NEW.company_name = '示例银行C'
        BEGIN
            SELECT RAISE(ABORT, 'forced test failure');
        END;
        """
    )
    db_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        import_opportunities(mainland_records, db_conn)
    assert _db_count(db_conn) == 0  # row2/13 等先前插入已回滚


def test_raw_data_source_sheet_row_preserved(db_conn, mainland_records):
    """raw_data 按合法 JSON 保存；source_sheet / source_row 保留。"""
    import_opportunities(mainland_records, db_conn)
    row = db_conn.execute(
        "SELECT * FROM opportunities WHERE source_row=2"
    ).fetchone()
    assert row["source_sheet"] == "中国大陆"
    assert row["source_row"] == 2
    raw = json.loads(row["raw_data"])
    assert isinstance(raw, dict)
    assert raw["E"] == "示例科技A"
    assert raw["F"] == "秋招全职"


def test_check_constraint_still_rejects_unknown(db_conn):
    """opportunities.record_type CHECK 仍拒绝 unknown。"""
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO opportunities (record_type, display_title, company_name,"
            " source_sheet, source_row, dedupe_key)"
            " VALUES ('unknown', 'x', 'y', 's', 1, 'k_unknown_reject_test')"
        )


# ---------------------------------------------------------------------------
# CSV 单表 / 安全边界
# ---------------------------------------------------------------------------


def test_csv_single_sheet_preview(db_conn):
    """CSV 单表输入可完成预览分类，统计与 XLSX 中国大陆一致且零写入。"""
    records = parse_csv(CSV_FIXTURE, sheet_name="中国大陆")
    result = classify_records(records, db_conn)
    assert result["total"] == 14
    assert result["counts"] == {
        CATEGORY_NEW: 3,
        CATEGORY_DUPLICATE: 1,
        CATEGORY_INVALID: 2,
        CATEGORY_PENDING: 8,
    }
    assert _db_count(db_conn) == 0


def test_xlsx_sheet_selection_parses_only_selected_sheet():
    """XLSX 工作表选择：只解析选中的表（此处为英国），不加载其他表。"""
    records = parse_workbook_sheet(XLSX_FIXTURE, "英国")
    assert len(records) == 6
    assert {r["source_sheet"] for r in records} == {"英国"}
    assert all(r["source_sheet"] != "中国大陆" for r in records)


def test_no_data_private_access_in_source():
    """service 与页面源码不得引用 data/private 或真实工作簿文件名。"""
    import services.opportunity_service as service

    sources = [Path(service.__file__).read_text(encoding="utf-8")]
    page = PROJECT_ROOT / "pages" / "import_page.py"
    if page.exists():
        sources.append(page.read_text(encoding="utf-8"))
    for source in sources:
        assert "data/private" not in source, "源码不得引用 data/private"
        assert "智联-岗位信息表" not in source, "源码不得引用真实工作簿文件名"


def test_no_repo_upload_files_or_test_db():
    """上传只进系统临时目录；服务层不自带默认数据库路径（连接由调用方传入）。"""
    import services.opportunity_service as service

    service_source = Path(service.__file__).read_text(encoding="utf-8")
    assert "DB_PATH" not in service_source, "服务层不得引用默认数据库路径"

    page = PROJECT_ROOT / "pages" / "import_page.py"
    if page.exists():
        page_source = page.read_text(encoding="utf-8")
        assert "tempfile" in page_source, "上传文件必须保存在系统临时目录"
        assert "UPLOADS_DIR" not in page_source, "上传不得写入 data/uploads"
        assert "data/private" not in page_source
        assert "data/sample" not in page_source


# ===========================================================================
# 任务 5 修正测试
# ===========================================================================


# ---------------------------------------------------------------------------
# 修复一：预览不创建持久数据库
# ---------------------------------------------------------------------------


def test_preview_does_not_create_persistent_db(tmp_path):
    """正式数据库文件不存在时执行预览，结束后数据库文件仍不存在。"""
    db_file = tmp_path / "careercopilot.db"
    assert not db_file.exists()
    conn = get_preview_connection(db_file)
    # 模拟预览查重（内存库已建表）
    from services.dedup_service import find_by_dedupe_key

    assert find_by_dedupe_key(conn, "campaign_nonexistent") is None
    conn.close()
    assert not db_file.exists(), "预览不应创建持久数据库文件"


def test_preview_does_not_create_table_in_empty_db(tmp_path):
    """已有空数据库文件（无 opportunities 表）时预览不会建表。"""
    db_file = tmp_path / "careercopilot.db"
    # 创建一个有效的 SQLite 文件，但不含 opportunities 表
    setup = sqlite3.connect(str(db_file))
    setup.execute("CREATE TABLE other_table (x INTEGER)")
    setup.commit()
    setup.close()

    conn = get_preview_connection(db_file)
    # 无 opportunities 表 → 回退内存库（已建表）
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='opportunities'"
    )
    assert cur.fetchone() is not None  # 内存库有表
    conn.close()

    # 原文件未被修改：仍只有 other_table，无 opportunities
    check = sqlite3.connect(str(db_file))
    tables = [
        r[0]
        for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    check.close()
    assert "other_table" in tables
    assert "opportunities" not in tables, "预览不应在持久文件中建表"


def test_preview_detects_duplicates_in_existing_db(tmp_path, mainland_records):
    """已存在数据库中的重复记录仍能被只读预览识别。"""
    db_file = tmp_path / "careercopilot.db"
    # 先正式导入，建立有记录的数据库
    write_conn = get_connection(db_file)
    init_db(write_conn)
    import_opportunities(mainland_records, write_conn)
    write_conn.close()

    # 预览：只读连接应能识别已有重复
    preview_conn = get_preview_connection(db_file)
    result = classify_records(mainland_records, preview_conn)
    assert result["counts"][CATEGORY_NEW] == 0
    assert result["counts"][CATEGORY_DUPLICATE] == 4
    preview_conn.close()

    # 文件未被修改（仍 3 条）
    check = get_connection(db_file)
    init_db(check)
    assert _db_count(check) == 3
    check.close()


def test_explicit_import_creates_db(tmp_path, mainland_records):
    """点击/显式调用正式导入后才创建数据库并写入记录。"""
    db_file = tmp_path / "careercopilot.db"
    assert not db_file.exists()

    # 预览不创建
    preview_conn = get_preview_connection(db_file)
    classify_records(mainland_records, preview_conn)
    preview_conn.close()
    assert not db_file.exists(), "预览阶段不得创建数据库文件"

    # 正式导入才创建
    write_conn = get_connection(db_file)
    try:
        init_db(write_conn)
        report = import_opportunities(mainland_records, write_conn)
    finally:
        write_conn.close()
    assert db_file.exists(), "正式导入后才创建数据库"
    assert report["inserted"] == 3


# ---------------------------------------------------------------------------
# 修复二：display_title 尊重用户显式映射
# ---------------------------------------------------------------------------


def test_display_title_respects_user_mapping():
    """用户明确映射的 display_title 不被覆盖。"""
    record = {
        "record_type": "unknown",
        "source_sheet": "中国大陆",
        "source_row": 99,
        "raw_data": {
            "C": "示例科技A",
            "D": "示例专属招聘项目",
            "J": "https://example.com/app",
        },
    }
    confirmations = {
        preview_identifier(record): {
            "record_type": "campaign",
            "field_mapping": {
                "company_name": "C",
                "display_title": "D",
                "application_url": "J",
            },
        }
    }
    conn = get_connection(":memory:")
    init_db(conn)
    # 预览阶段 prepared.display_title 严格等于用户映射值
    result = classify_records([record], conn, confirmations)
    assert result["counts"][CATEGORY_NEW] == 1
    prepared = result["items"][CATEGORY_NEW][0]["prepared"]
    assert prepared["display_title"] == "示例专属招聘项目"
    # 正式导入后数据库也保存该值
    report = import_opportunities([record], conn, confirmations)
    assert report["inserted"] == 1
    row = conn.execute(
        "SELECT * FROM opportunities WHERE source_row=99"
    ).fetchone()
    assert row["display_title"] == "示例专属招聘项目"
    conn.close()


# ---------------------------------------------------------------------------
# 修复三：来源字段与 raw_data 在预览阶段验证
# ---------------------------------------------------------------------------


def _make_campaign_record(**overrides) -> dict[str, Any]:
    """构造一条可靠的 campaign 记录，可覆盖任意字段。"""
    base: dict[str, Any] = {
        "record_type": "campaign",
        "display_title": "示例科技A 2026秋招",
        "company_name": "示例科技A",
        "recruitment_type": "秋招全职",
        "target_cohort": "2026届",
        "announcement_url": "https://example.com/ann/a-2026",
        "application_url": "https://example.com/apply/a-2026",
        "source_sheet": "中国大陆",
        "source_row": 2,
        "raw_data": {"E": "示例科技A", "F": "秋招全职"},
    }
    base.update(overrides)
    return base


def test_missing_source_sheet_is_invalid(db_conn):
    """缺 source_sheet → invalid。"""
    record = _make_campaign_record(source_sheet="")
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1
    assert result["counts"][CATEGORY_NEW] == 0


def test_missing_source_row_is_invalid(db_conn):
    """缺 source_row（键不存在）→ invalid。"""
    record = _make_campaign_record()
    del record["source_row"]
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_source_row_none_is_invalid(db_conn):
    """source_row=None → invalid。"""
    record = _make_campaign_record(source_row=None)
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_source_row_invalid_string_is_invalid(db_conn):
    """source_row 为非法字符串 → invalid。"""
    record = _make_campaign_record(source_row="abc")
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_source_row_less_than_2_is_invalid(db_conn):
    """source_row 小于 2 → invalid。"""
    record = _make_campaign_record(source_row=1)
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_raw_data_none_is_invalid(db_conn):
    """raw_data=None → invalid。"""
    record = _make_campaign_record(raw_data=None)
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_raw_data_not_mapping_is_invalid(db_conn):
    """raw_data 不是 Mapping → invalid。"""
    record = _make_campaign_record(raw_data=["not", "a", "dict"])
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_raw_data_not_json_serializable_is_invalid(db_conn):
    """raw_data 包含不可 JSON 序列化对象 → invalid。"""
    record = _make_campaign_record(raw_data={"bad": object()})
    result = classify_records([record], db_conn)
    assert result["counts"][CATEGORY_INVALID] == 1


def test_invalid_source_records_do_not_cause_typeerror_on_import(db_conn):
    """预览判 invalid 的记录在 import 时不会抛 TypeError，数据库零写入。"""
    records = [
        _make_campaign_record(source_row=None),
        _make_campaign_record(raw_data=None),
        _make_campaign_record(source_sheet=""),
    ]
    report = import_opportunities(records, db_conn)
    assert report["inserted"] == 0
    assert _db_count(db_conn) == 0
    # 四类闭合
    assert sum(report["counts"].values()) == report["total"] == 3


# ---------------------------------------------------------------------------
# 修复四：unknown 分页与 session_state 保存（纯函数测试）
# ---------------------------------------------------------------------------


def test_paginate_unknown_default_page_size():
    """1000 条 unknown，默认每页 20 条 → 50 页，第 1 页 [0, 20)。"""
    info = paginate_unknown(1000, DEFAULT_PAGE_SIZE, 1)
    assert info["page_size"] == 20
    assert info["total_pages"] == 50
    assert info["current_page"] == 1
    assert info["start"] == 0
    assert info["end"] == 20
    assert info["has_prev"] is False
    assert info["has_next"] is True


def test_paginate_unknown_50_per_page():
    """选择每页 50 条 → 20 页，第 1 页 [0, 50)。"""
    info = paginate_unknown(1000, 50, 1)
    assert info["page_size"] == 50
    assert info["total_pages"] == 20
    assert info["end"] == 50


def test_paginate_unknown_max_page_size():
    """超过 50 的页大小被截断为 50。"""
    info = paginate_unknown(1000, 100, 1)
    assert info["page_size"] == MAX_PAGE_SIZE


def test_paginate_unknown_page_count():
    """页数计算正确（边界：0、1、20、21）。"""
    assert paginate_unknown(0, 20, 1)["total_pages"] == 1
    assert paginate_unknown(1, 20, 1)["total_pages"] == 1
    assert paginate_unknown(20, 20, 1)["total_pages"] == 1
    assert paginate_unknown(21, 20, 1)["total_pages"] == 2


def test_paginate_unknown_current_page_clamped():
    """当前页超出范围时夹紧到最后一页。"""
    info = paginate_unknown(100, 20, 99)
    assert info["current_page"] == 5  # 共 5 页


def test_collect_confirmations_preserves_other_pages():
    """切换页面后确认结果保留：store 含跨页确认，collect 全部收集。"""
    records = [
        {
            "record_type": "unknown",
            "source_sheet": "中国大陆",
            "source_row": i,
            "raw_data": {"E": "示例科技A"},
        }
        for i in range(2, 102)  # 100 条 unknown
    ]
    store = {
        preview_identifier(records[0]): {  # 第 1 页
            "record_type": "campaign",
            "field_mapping": {"company_name": "E", "application_url": "J"},
        },
        preview_identifier(records[50]): {  # 第 3 页
            "record_type": "job",
            "field_mapping": {
                "company_name": "E",
                "job_title": "F",
                "application_url": "J",
            },
        },
    }
    confirmations = collect_confirmations(store, records)
    assert len(confirmations) == 2  # 跨页确认都收集到


def test_collect_confirmations_isolates_by_file_and_sheet():
    """不同文件/工作表的 store 不串数据（键命名空间隔离）。"""
    rec = [
        {
            "record_type": "unknown",
            "source_sheet": "中国大陆",
            "source_row": 2,
            "raw_data": {"E": "x"},
        }
    ]
    store_a = {
        preview_identifier(rec[0]): {
            "record_type": "campaign",
            "field_mapping": {},
        }
    }
    # 不同 store（模拟不同 file_key/sheet_name）互不影响
    assert len(collect_confirmations(store_a, rec)) == 1
    assert len(collect_confirmations({}, rec)) == 0  # 空 store = 无确认


def test_collect_confirmations_skips_unconfirmed_entries():
    """store 中 record_type 非 campaign/job 的条目被跳过（取消确认）。"""
    rec = [
        {
            "record_type": "unknown",
            "source_sheet": "中国大陆",
            "source_row": 2,
            "raw_data": {"E": "x"},
        }
    ]
    store = {
        preview_identifier(rec[0]): {
            "record_type": "待确认",  # 取消确认
            "field_mapping": {},
        }
    }
    assert len(collect_confirmations(store, rec)) == 0


def test_count_confirmed_across_all_pages():
    """count_confirmed 基于全部 unknown 而非当前页。"""
    records = [
        {
            "record_type": "unknown",
            "source_sheet": "s",
            "source_row": i,
            "raw_data": {},
        }
        for i in range(2, 12)  # 10 条 unknown
    ]
    store = {
        preview_identifier(records[0]): {
            "record_type": "campaign",
            "field_mapping": {},
        },
        preview_identifier(records[1]): {
            "record_type": "job",
            "field_mapping": {},
        },
        preview_identifier(records[2]): {
            "record_type": "unknown",  # 取消确认，不计入
            "field_mapping": {},
        },
    }
    confirmed, unconfirmed = count_confirmed(store, records)
    assert confirmed == 2
    assert unconfirmed == 8


def test_full_sheet_stats_not_just_current_page():
    """全表统计等于全部 unknown 数（1000），而非当前页数量。"""
    records = [
        {
            "record_type": "unknown",
            "source_sheet": "中国大陆",
            "source_row": i,
            "raw_data": {},
        }
        for i in range(2, 1002)  # 1000 条 unknown
    ]
    # 模拟分页：即使只看第 1 页 20 条，统计仍针对全表
    page_info = paginate_unknown(1000, 20, 1)
    assert page_info["end"] - page_info["start"] == 20  # 当前页 20 条
    # 全表统计
    conn = get_connection(":memory:")
    init_db(conn)
    result = classify_records(records, conn)
    assert result["counts"][CATEGORY_PENDING] == 1000  # 全表 1000
    assert result["total"] == 1000
    conn.close()


# ---------------------------------------------------------------------------
# 修复四：分页交互（Streamlit 页面源码静态校验）
# ---------------------------------------------------------------------------


def test_page_source_implements_pagination():
    """页面源码包含分页实现（页大小选择、页码选择、当前页渲染切片）。"""
    page = PROJECT_ROOT / "pages" / "import_page.py"
    assert page.exists(), "import_page.py 应存在"
    source = page.read_text(encoding="utf-8")
    assert "paginate_unknown" in source, "页面应调用分页函数"
    assert "PAGE_SIZE_OPTIONS" in source, "页面应提供页大小选项"
    assert "page_info['start']" in source, "页面应按切片渲染当前页"
    assert "import_confirmations:" in source, "确认存储键应含命名空间"


def test_page_source_uses_preview_connection():
    """页面源码在预览阶段使用 get_preview_connection，仅在导入时 get_connection。"""
    source = (PROJECT_ROOT / "pages" / "import_page.py").read_text(encoding="utf-8")
    assert "get_preview_connection" in source, "预览应使用只读/内存连接"
    # 可写连接只在确认导入按钮分支内
    assert "write_conn = get_connection()" in source
    # init_db 只在可写连接分支内（不在预览路径）
    assert source.count("init_db(write_conn)") == 1
