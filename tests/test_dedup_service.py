"""`dedup_service` 与 `opportunities` 表约束的单元测试（任务 3）。

覆盖 docs/TASKS.md 任务 3 完成标准：
1. `opportunities` 表能被正确创建，`record_type` CHECK 约束生效（写入 `unknown` 应失败）；
2. 给定 campaign / job 记录，能分别生成正确的 `dedupe_key`；
3. 重复记录生成相同 `dedupe_key`；`unknown` 记录不生成 `dedupe_key`，
   标记为待确认（preview 标识），不入库。

测试数据**完全虚构**（示例科技A / 示例制造B / example.com，
见 docs/SOURCE_SCHEMA.md §6），不访问网络，不读取 data/private。
"""

from __future__ import annotations

import sqlite3

import pytest

from database.db_handler import get_connection, init_db
from services.dedup_service import (
    compute_dedupe_key,
    find_by_dedupe_key,
    preview_identifier,
)

# ---------------------------------------------------------------------------
# 完全虚构的测试数据（docs/SOURCE_SCHEMA.md §6 匿名素材规则）
# ---------------------------------------------------------------------------

CAMPAIGN_A: dict = {
    "record_type": "campaign",
    "display_title": "示例科技A 2026 秋季校园招聘",
    "company_name": "示例科技A",
    "recruitment_type": "秋招全职",
    "target_cohort": "2026届",
    "announcement_title": "示例科技A 2026 秋季校园招聘",
    "announcement_url": "https://example.com/announcement/a-2026",
    "application_url": "https://example.com/apply/a-2026",
    "source_sheet": "中国大陆",
    "source_row": 101,
}

JOB_B: dict = {
    "record_type": "job",
    "display_title": "示例后端开发工程师",
    "job_title": "示例后端开发工程师",
    "job_categories": "研发/后端",
    "company_name": "示例制造B",
    "recruitment_type": "秋招全职",
    "target_cohort": "2026届",
    "education_requirement": "本科及以上",
    "location": "上海市",
    "application_url": "https://example.com/apply/b-2026-4567",
    "source_sheet": "中国大陆",
    "source_row": 202,
}

UNKNOWN_ROW: dict = {
    "record_type": "unknown",
    "display_title": "示例待确认记录",
    "company_name": "示例咨询D",
    "source_sheet": "中国大陆",
    "source_row": 303,
    "raw_data": '{"E": "示例咨询D", "F": "示例待确认文本", "G": "示例待确认文本"}',
}

# 插入用列（未在记录中给出的列写 NULL；status/priority 需经 extra_cols 显式传入）
_INSERT_COLS = (
    "record_type",
    "display_title",
    "job_title",
    "job_categories",
    "company_name",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "announcement_title",
    "announcement_url",
    "application_url",
    "source_sheet",
    "source_row",
    "dedupe_key",
    "raw_data",
)


def _insert(conn: sqlite3.Connection, record: dict, extra_cols: tuple = ()) -> None:
    """按给定列把记录插入 opportunities（仅测试用，列名固定白名单）。"""
    cols = _INSERT_COLS + extra_cols
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO opportunities ({', '.join(cols)}) VALUES ({placeholders})",
        [record.get(col) for col in cols],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 夹具：每个测试一个独立的临时数据库
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    init_db(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# 1) 表创建与约束（完成标准 1）
# ---------------------------------------------------------------------------


def test_init_db_creates_opportunities_table(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='opportunities'"
    ).fetchone()
    assert row is not None


def test_init_db_is_idempotent(conn):
    # 重复初始化不应报错、不应重复建表
    init_db(conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='opportunities'"
    ).fetchone()[0]
    assert count == 1


def test_insert_campaign_ok(conn):
    key = compute_dedupe_key(CAMPAIGN_A)
    assert key is not None
    _insert(conn, {**CAMPAIGN_A, "dedupe_key": key})
    stored = conn.execute("SELECT * FROM opportunities").fetchone()
    assert stored["record_type"] == "campaign"
    assert stored["company_name"] == "示例科技A"
    # 系统默认值
    assert stored["priority"] == "low"
    assert stored["status"] == "discovered"


def test_insert_job_ok(conn):
    key = compute_dedupe_key(JOB_B)
    assert key is not None
    _insert(conn, {**JOB_B, "dedupe_key": key})
    stored = conn.execute("SELECT * FROM opportunities").fetchone()
    assert stored["record_type"] == "job"
    assert stored["job_title"] == "示例后端开发工程师"


def test_insert_unknown_rejected_by_check_constraint(conn):
    """`unknown` 不得写入 opportunities（CHECK 约束生效，docs/ARCHITECTURE.md §6）。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, {**UNKNOWN_ROW, "dedupe_key": "preview:中国大陆:303"})
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0


def test_missing_record_type_rejected(conn):
    record = {k: v for k, v in CAMPAIGN_A.items() if k != "record_type"}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, {**record, "dedupe_key": "campaign_x"})


def test_dedupe_key_must_be_unique(conn):
    key = compute_dedupe_key(CAMPAIGN_A)
    _insert(conn, {**CAMPAIGN_A, "dedupe_key": key})
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, {**CAMPAIGN_A, "dedupe_key": key})


def test_required_fields_not_null(conn):
    for missing_col in ("display_title", "company_name", "source_sheet", "source_row"):
        record = {k: v for k, v in CAMPAIGN_A.items() if k != missing_col}
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, {**record, "dedupe_key": f"campaign_missing_{missing_col}"})
        conn.rollback()


def test_invalid_status_rejected(conn):
    key = compute_dedupe_key(CAMPAIGN_A)
    record = {**CAMPAIGN_A, "dedupe_key": key, "status": "not_a_status"}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, record, extra_cols=("status",))


def test_invalid_priority_rejected(conn):
    key = compute_dedupe_key(CAMPAIGN_A)
    record = {**CAMPAIGN_A, "dedupe_key": key, "priority": "urgent"}
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, record, extra_cols=("priority",))


# ---------------------------------------------------------------------------
# 2) dedupe_key 生成规则（完成标准 2）
# ---------------------------------------------------------------------------


def test_campaign_key_is_deterministic_and_normalized():
    key1 = compute_dedupe_key(CAMPAIGN_A)
    key2 = compute_dedupe_key(dict(CAMPAIGN_A))
    assert key1 == key2

    # 首尾空白归一化后仍为同一键
    padded = {
        **CAMPAIGN_A,
        "company_name": "  示例科技A  ",
        "announcement_url": " https://example.com/announcement/a-2026 ",
    }
    assert compute_dedupe_key(padded) == key1

    # 键长满足 VARCHAR(100)：前缀 campaign_ + 64 位十六进制 = 73 字符
    assert key1.startswith("campaign_")
    assert len(key1) <= 100


def test_campaign_key_differs_when_rule_field_changes():
    base_key = compute_dedupe_key(CAMPAIGN_A)

    changed_cohort = {**CAMPAIGN_A, "target_cohort": "2027届"}
    changed_type = {**CAMPAIGN_A, "recruitment_type": "暑期实习"}
    changed_url = {**CAMPAIGN_A, "announcement_url": "https://example.com/announcement/a-2027"}

    assert compute_dedupe_key(changed_cohort) != base_key
    assert compute_dedupe_key(changed_type) != base_key
    assert compute_dedupe_key(changed_url) != base_key


def test_campaign_key_falls_back_to_announcement_title():
    """无 announcement_url 时回退 announcement_title 参与键计算。"""
    no_url = {**CAMPAIGN_A}
    no_url.pop("announcement_url")
    key = compute_dedupe_key(no_url)

    # 同一公告名称、同样无 URL 的重复记录 -> 相同键
    duplicate = {**no_url, "source_row": 999}
    assert compute_dedupe_key(duplicate) == key

    # 公告名称不同 -> 键不同
    other_title = {**no_url, "announcement_title": "示例科技A 2027 春季校园招聘"}
    assert compute_dedupe_key(other_title) != key


def test_job_key_is_deterministic_and_normalized():
    key1 = compute_dedupe_key(JOB_B)
    key2 = compute_dedupe_key(dict(JOB_B))
    assert key1 == key2
    assert key1.startswith("job_")
    assert len(key1) <= 100

    changed_location = {**JOB_B, "location": "北京市"}
    changed_title = {**JOB_B, "job_title": "示例前端开发工程师"}
    changed_url = {**JOB_B, "application_url": "https://example.com/apply/b-2026-9999"}

    assert compute_dedupe_key(changed_location) != key1
    assert compute_dedupe_key(changed_title) != key1
    assert compute_dedupe_key(changed_url) != key1


def test_job_key_falls_back_to_job_categories():
    """无 application_url 时回退 job_categories 参与键计算。"""
    no_url = {**JOB_B}
    no_url.pop("application_url")
    key = compute_dedupe_key(no_url)

    duplicate = {**no_url, "source_row": 999}
    assert compute_dedupe_key(duplicate) == key

    other_category = {**no_url, "job_categories": "研发/前端"}
    assert compute_dedupe_key(other_category) != key


def test_same_values_different_record_type_get_different_keys():
    """同业务取值的 campaign 与 job 键不同（类型前缀），避免 UNIQUE 误撞。"""
    shared = {
        "company_name": "示例零售E",
        "recruitment_type": "秋招全职",
        "target_cohort": "2026届",
        "location": "",
    }
    campaign_like = {
        **shared,
        "record_type": "campaign",
        "display_title": "示例零售E 校招",
        "announcement_url": "https://example.com/apply/e",
        "job_title": "",
        "job_categories": "",
    }
    job_like = {
        **shared,
        "record_type": "job",
        "display_title": "示例零售E 岗位",
        "application_url": "https://example.com/apply/e",
        "announcement_url": "",
        "announcement_title": "",
    }
    campaign_key = compute_dedupe_key(campaign_like)
    job_key = compute_dedupe_key(job_like)
    assert campaign_key != job_key
    assert campaign_key.startswith("campaign_")
    assert job_key.startswith("job_")


# ---------------------------------------------------------------------------
# 3) unknown 行为（完成标准 3：不生成键、待确认标识、不入库）
# ---------------------------------------------------------------------------


def test_unknown_returns_no_dedupe_key():
    assert compute_dedupe_key(UNKNOWN_ROW) is None


def test_missing_or_invalid_record_type_returns_none():
    no_type = {k: v for k, v in CAMPAIGN_A.items() if k != "record_type"}
    assert compute_dedupe_key(no_type) is None
    assert compute_dedupe_key({**CAMPAIGN_A, "record_type": "weird"}) is None


def test_unknown_uses_preview_identifier_not_dedupe_key():
    """unknown 仅用临时 preview 标识（source_sheet + source_row）。"""
    identifier = preview_identifier(UNKNOWN_ROW)
    assert identifier == "preview:中国大陆:303"
    assert compute_dedupe_key(UNKNOWN_ROW) is None


def test_explicit_preview_id_takes_precedence():
    record = {**UNKNOWN_ROW, "preview_id": "tmp-abc-123"}
    assert preview_identifier(record) == "tmp-abc-123"


def test_preview_identifier_tolerates_missing_source_info():
    assert preview_identifier({}) == "preview:?:?"
    assert preview_identifier({"source_sheet": "美国"}) == "preview:美国:?"


def test_unknown_never_lands_in_database_via_dedup_flow(conn):
    """端到端：unknown 在导入去重流程中不计键、不入库，仅登记为待确认。"""
    # 计划入库 campaign/job；unknown 只能进入待确认名单
    pending: list[str] = []
    for record in (CAMPAIGN_A, JOB_B, UNKNOWN_ROW):
        key = compute_dedupe_key(record)
        if key is None:
            pending.append(preview_identifier(record))
            continue
        if find_by_dedupe_key(conn, key) is None:
            _insert(conn, {**record, "dedupe_key": key})

    rows = conn.execute("SELECT record_type, COUNT(*) FROM opportunities GROUP BY record_type").fetchall()
    types = {row["record_type"]: row[1] for row in rows}
    assert types == {"campaign": 1, "job": 1}  # unknown 未入库
    assert pending == ["preview:中国大陆:303"]  # unknown 以 preview 标识待确认


# ---------------------------------------------------------------------------
# 4) 重复识别（完成标准 3：重复记录生成相同键）
# ---------------------------------------------------------------------------


def test_duplicate_campaign_records_share_key_and_are_not_reinserted(conn):
    key = compute_dedupe_key(CAMPAIGN_A)
    _insert(conn, {**CAMPAIGN_A, "dedupe_key": key})

    duplicate = {**CAMPAIGN_A, "source_row": 555}  # 同业务内容、不同源行
    duplicate_key = compute_dedupe_key(duplicate)
    assert duplicate_key == key

    assert find_by_dedupe_key(conn, duplicate_key) is not None
    # 模拟导入器行为：命中重复 -> 跳过写入
    if find_by_dedupe_key(conn, duplicate_key) is not None:
        pass  # skip insert
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


def test_find_by_dedupe_key_returns_none_when_absent(conn):
    assert find_by_dedupe_key(conn, "campaign_does_not_exist") is None
