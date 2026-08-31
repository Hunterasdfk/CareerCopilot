"""SQLite 数据库操作处理器（任务 3）。

职责（docs/TASKS.md 任务 3、docs/ARCHITECTURE.md §1）：
- 提供数据库连接；
- 初始化 `opportunities` 表。

不做业务 CRUD（属后续 opportunity_service），不做解析、导入与去重计算。
表结构以 docs/DATA_MODEL.md §1 为准。

关键约束（docs/DATA_MODEL.md §1、§3）：
- `record_type` CHECK 只允许 `campaign` / `job`，`unknown` 不入库（见 ARCHITECTURE §6）；
- `dedupe_key` UNIQUE NOT NULL，由 services/dedup_service.py 按规则生成；
- `status` / `priority` 为 §3.1 / §3.3 定义的封闭枚举，同样用 CHECK 落实。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config.settings import DB_PATH

# docs/DATA_MODEL.md §3.1 投递状态枚举
_ALLOWED_STATUS = (
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
)

# docs/DATA_MODEL.md §3.3 优先级枚举
_ALLOWED_PRIORITY = ("high", "medium", "low")

# opportunities 表结构（docs/DATA_MODEL.md §1）
# SQLite 无独立 DateTime 类型，时间字段统一用 TEXT 存 ISO 8601 字符串。
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type VARCHAR(20) NOT NULL
        CHECK (record_type IN ('campaign', 'job')),
    display_title VARCHAR(200) NOT NULL,
    job_title VARCHAR(200),
    job_categories VARCHAR(200),
    company_name VARCHAR(100) NOT NULL,
    industry VARCHAR(100),
    recruitment_type VARCHAR(50),
    target_cohort VARCHAR(50),
    education_requirement VARCHAR(50),
    location VARCHAR(100),
    deadline TEXT,
    announcement_title VARCHAR(200),
    announcement_url VARCHAR(500),
    application_url VARCHAR(500),
    source_sheet VARCHAR(50) NOT NULL,
    source_row INTEGER NOT NULL,
    import_batch_id VARCHAR(50),
    dedupe_key VARCHAR(100) UNIQUE NOT NULL,
    raw_data TEXT,
    priority VARCHAR(20) NOT NULL DEFAULT 'low'
        CHECK (priority IN ('high', 'medium', 'low')),
    status VARCHAR(20) NOT NULL DEFAULT 'discovered'
        CHECK (status IN (
            'discovered', 'shortlisted', 'opened', 'applying', 'applied',
            'assessment', 'interview', 'offer', 'rejected', 'withdrawn'
        )),
    notes TEXT,
    opened_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立 SQLite 连接。

    Args:
        db_path: 数据库文件路径。None 时使用 config.settings.DB_PATH；
            传 ":memory:" 可获得内存库（便于测试）。

    Returns:
        行以 sqlite3.Row 形式访问的连接对象。
    """
    if db_path is None:
        target: str | Path = DB_PATH
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    elif str(db_path) == ":memory:":
        target = ":memory:"
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = path

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """在给定连接上创建 `opportunities` 表（幂等，已存在则跳过）。"""
    conn.execute(SCHEMA_SQL)
    conn.commit()


def _new_memory_db() -> sqlite3.Connection:
    """创建初始化后的内存库（仅供预览，不落盘，不影响任何持久文件）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)  # CREATE TABLE IF NOT EXISTS，在内存库建表
    return conn


def get_preview_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """获取预览阶段只读连接（任务 5 修复一：预览零持久化）。

    预览阶段**不得创建或修改持久数据库文件**、不得修改 Schema 或记录：

    - 若 ``db_path`` 指向的数据库文件**已存在且含 opportunities 表**，返回
      **只读**连接（``PRAGMA query_only = ON``），仅用于 SELECT 查重；
    - 若文件**不存在**或**尚未建表**，返回初始化后的 **:memory:** 内存库
      （``init_db`` 在内存建表，不影响任何持久文件）；
    - 绝不因上传、刷新、选择工作表或预览而创建 ``data/careercopilot.db``；
    - 绝不修改正式数据库的 Schema 或记录。

    正式导入请使用 ``get_connection()`` + ``init_db()`` 打开可写连接
    （仅在用户点击“确认导入”后）。
    """
    if db_path is None:
        db_path = DB_PATH

    if str(db_path) == ":memory:":
        return _new_memory_db()

    path = Path(db_path)
    if not path.exists():
        # 文件不存在 → 不创建，用内存库完成预览
        return _new_memory_db()

    # 文件存在：以普通连接打开并检查是否已建表（连接不会创建新文件）
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        )
        if cur.fetchone() is None:
            # 文件存在但无 opportunities 表 → 不修改文件，回退内存库
            conn.close()
            return _new_memory_db()
        # 表存在：设为只读模式，防止预览误写（任务 5 预览零持久化）
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.DatabaseError:
        # 文件损坏等 → 回退内存库
        return _new_memory_db()
