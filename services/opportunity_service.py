"""机会导入服务（任务 5）。

职责（docs/TASKS.md 任务 5、docs/ARCHITECTURE.md §2/§6）：

- `classify_records()`：预览阶段分类。把解析记录划分为**互斥**的四类
  （数量之和恒等于参与预览的非空记录总数），**数据库零写入**
  （只做 SELECT 查重）：

  1. **pending**   —— `record_type=unknown` 且未完成有效人工确认
     （无确认条目 / 取消确认 / 确认类型非 campaign/job / 映射缺少结构
     必填目标），不生成最终 dedupe_key，不入库；
  2. **invalid**   —— 确认后（或可靠布局解析出）的记录仍未通过验证：
     映射目标不在允许列表、映射引用不存在的原始列、必填字段缺失、
     URL 非法或两者皆缺失，不入库；
  3. **duplicate** —— `dedupe_key` 已存在于数据库，或同一批先前记录
     已出现相同 key，不重复写入；
  4. **new**       —— 可靠 campaign/job 或已确认 unknown，验证通过且
     不重复；**只有这一类可以写入数据库**。

- `import_opportunities()`：由页面"确认导入"按钮显式调用后才写库；
  事务写入，任何异常整体回滚并向上抛出；导入报告使用**实际分类与
  实际写入结果**。

unknown 人工确认结构（界面建议仅供展示，**不得自动确认**）::

    {
        "record_type": "campaign" | "job",
        "field_mapping": {标准字段名: raw_data 列字母},
    }

- 映射目标字段只允许 `MAPPABLE_FIELDS` 白名单，不接受任意数据库列名；
- 确认后重新执行必填字段验证、URL 验证和 dedupe_key 计算；
- 原始 `raw_data` / `source_sheet` / `source_row` 始终保留；
- `unknown` 永不写入 `opportunities` 表（数据库 CHECK 亦拒绝）。

写入要求（docs/DATA_MODEL.md §4/§7）：
- 写入前调用 `compute_dedupe_key()`，同时查数据库重复与批次内重复；
- 不使用 INSERT OR REPLACE；不修改重复记录原有的 status/priority 等字段；
- 参数化 SQL；`raw_data` 按合法 JSON 文本保存；
- 默认 `status='discovered'`、`priority='low'`。

安全约束：不访问网络；不读取任何真实源数据目录；数据库只通过调用方传入的
`sqlite3.Connection` 操作（本模块不引用任何默认数据库路径）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from services.dedup_service import (
    compute_dedupe_key,
    find_by_dedupe_key,
    preview_identifier,
)
from services.layout_detector import is_valid_url

# ---------------------------------------------------------------------------
# 四类分类常量（互斥）
# ---------------------------------------------------------------------------

CATEGORY_NEW = "new"
CATEGORY_DUPLICATE = "duplicate"
CATEGORY_INVALID = "invalid"
CATEGORY_PENDING = "pending"
CATEGORIES: tuple[str, ...] = (
    CATEGORY_NEW,
    CATEGORY_DUPLICATE,
    CATEGORY_INVALID,
    CATEGORY_PENDING,
)

# unknown 确认后允许的 record_type
CONFIRMABLE_RECORD_TYPES: frozenset[str] = frozenset({"campaign", "job"})

# 字段映射允许列表（docs/DATA_MODEL.md §1 业务字段）。
# 系统字段（id/record_type/source_*/dedupe_key/raw_data/status/priority/
# notes/opened_at/applied_at/created_at/updated_at）一律不允许映射，
# 防止用户通过确认界面接受任意数据库列名。
MAPPABLE_FIELDS: frozenset[str] = frozenset(
    {
        "display_title",
        "job_title",
        "job_categories",
        "company_name",
        "industry",
        "recruitment_type",
        "target_cohort",
        "education_requirement",
        "location",
        "deadline",
        "announcement_title",
        "announcement_url",
        "application_url",
    }
)

# URL 类字段：结构必填（至少映射其一），docs/DATA_MODEL.md §4
_URL_FIELDS: frozenset[str] = frozenset({"announcement_url", "application_url"})

# unknown 人工确认分页（任务 5 修复四）
DEFAULT_PAGE_SIZE = 20
PAGE_SIZE_OPTIONS: tuple[int, ...] = (20, 50)
MAX_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _nonempty(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def _item(
    record: Mapping,
    preview_id: str,
    reason: str,
    dedupe_key: str | None = None,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条分类明细（items 列表的元素）。"""
    return {
        "record": record,
        "preview_id": preview_id,
        "reason": reason,
        "dedupe_key": dedupe_key,
        "prepared": prepared,
    }


def _derive_display_title(built: Mapping, record_type: str) -> str:
    """为记录推导 display_title（docs/DATA_MODEL.md §1）。

    优先级（任务 5 修复二：尊重用户显式映射）：

    1. 用户已映射且取值非空的 ``display_title``；
    2. campaign 的 ``announcement_title``；
    3. job 的 ``job_title``；
    4. 其他合理回退（``announcement_title``）；
    5. ``company_name``；
    6. 全空 → 空串（后续必填验证将判 invalid）。

    **不得覆盖用户明确映射的 display_title。**
    """
    if _nonempty(built.get("display_title")):
        return str(built["display_title"]).strip()
    if record_type == "campaign" and _nonempty(built.get("announcement_title")):
        return str(built["announcement_title"]).strip()
    if _nonempty(built.get("job_title")):
        return str(built["job_title"]).strip()
    if _nonempty(built.get("announcement_title")):
        return str(built["announcement_title"]).strip()
    if _nonempty(built.get("company_name")):
        return str(built["company_name"]).strip()
    return ""


def _is_valid_source_row(value: object) -> bool:
    """source_row 必须是有效正整数（数据行不小于 2，任务 5 修复三）。

    接受 int 或可被 ``int()`` 解析的数字字符串；拒绝 None、bool、
    非数字字符串、浮点数以及小于 2 的值。
    """
    if value is None:
        return False
    if isinstance(value, bool):  # bool 是 int 子类，单独拒绝
        return False
    if isinstance(value, int):
        return value >= 2
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            return int(text) >= 2
        except ValueError:
            return False
    # float / 其他类型：拒绝（行号应为整数）
    return False


def _build_from_confirmation(
    record: Mapping, confirmation: Mapping
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """根据确认条目从 `raw_data` 构建候选入库记录。

    判定顺序（写入 docs/TASKS.md 任务 5 分类规则）：

    1. 确认类型非 campaign/job（未确认 / 取消确认）→ **pending**；
    2. 映射目标不在 `MAPPABLE_FIELDS` 白名单 → **invalid**；
    3. 映射引用的列不存在于 `raw_data` → **invalid**；
    4. 映射缺少结构必填目标（company_name；job 还需 job_title；
       公告/投递链接至少其一）→ **pending**（等待用户补全确认）；
    5. 全部通过 → 从 raw_data 取值构建记录（含 display_title 推导，
       继承 raw_data / source_sheet / source_row）。

    Returns:
        (built, error)：built 不为 None 表示确认结构完整、可进入统一验证；
        否则 error = (category, reason)。
    """
    record_type = confirmation.get("record_type")
    if record_type not in CONFIRMABLE_RECORD_TYPES:
        return None, (
            CATEGORY_PENDING,
            "未完成有效确认（record_type 非 campaign/job），保持待确认",
        )

    mapping = confirmation.get("field_mapping") or {}
    if not isinstance(mapping, Mapping):
        return None, (CATEGORY_INVALID, "field_mapping 必须是 {字段: 列字母} 映射")

    for field in mapping:
        if field not in MAPPABLE_FIELDS:
            return None, (
                CATEGORY_INVALID,
                f"映射目标字段不在允许列表：{field}（不允许任意数据库列名）",
            )

    raw_data: Mapping = record.get("raw_data") or {}
    for field, col in mapping.items():
        if col not in raw_data:
            return None, (
                CATEGORY_INVALID,
                f"映射引用了不存在的原始列：{field} → {col}",
            )

    if "company_name" not in mapping:
        return None, (CATEGORY_PENDING, "映射不完整：缺少 company_name 映射")
    if record_type == "job" and "job_title" not in mapping:
        return None, (
            CATEGORY_PENDING,
            "映射不完整：job 记录缺少 job_title 映射",
        )
    if not (_URL_FIELDS & set(mapping)):
        return None, (
            CATEGORY_PENDING,
            "映射不完整：缺少公告/投递链接映射",
        )

    built: dict[str, Any] = {field: raw_data.get(col) for field, col in mapping.items()}
    built["record_type"] = record_type
    built["display_title"] = _derive_display_title(built, str(record_type))
    # 回溯字段必须保留（docs/TASKS.md 任务 5 unknown 确认要求）
    built["raw_data"] = record.get("raw_data")
    built["source_sheet"] = record.get("source_sheet")
    built["source_row"] = record.get("source_row")
    return built, None


def _validate_record(record: Mapping, record_type: str) -> str | None:
    """必填字段、来源字段与 URL 验证（docs/DATA_MODEL.md §4）。

    任务 5 修复三：来源字段（source_sheet / source_row）与 raw_data 在
    **预览阶段**就验证，任何一项不符合都归入 invalid，**不得先判 new、
    再在 _insert_params() 中抛 TypeError**。

    返回失败原因或 None（None 表示验证通过）。
    """
    if record_type not in CONFIRMABLE_RECORD_TYPES:
        return f"record_type 不是 campaign/job：{record_type}"
    if not _nonempty(record.get("company_name")):
        return "缺少必填字段 company_name"
    if not _nonempty(record.get("display_title")):
        return "缺少必填字段 display_title"
    if record_type == "job" and not _nonempty(record.get("job_title")):
        return "job 记录缺少必填字段 job_title"

    # 来源字段验证（任务 5 修复三）
    source_sheet = record.get("source_sheet")
    if not isinstance(source_sheet, str) or not source_sheet.strip():
        return "source_sheet 必须是非空字符串"
    if not _is_valid_source_row(record.get("source_row")):
        return "source_row 必须是有效正整数（数据行不小于 2）"

    # raw_data 验证（任务 5 修复三）
    raw_data = record.get("raw_data")
    if not isinstance(raw_data, Mapping):
        return "raw_data 必须是 Mapping/dict"
    try:
        json.dumps(raw_data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return f"raw_data 无法序列化为合法 JSON：{exc}"

    # URL 验证
    announcement_url = record.get("announcement_url")
    application_url = record.get("application_url")
    for name, value in (
        ("announcement_url", announcement_url),
        ("application_url", application_url),
    ):
        if _nonempty(value) and not is_valid_url(value):
            return f"链接格式无效：{name}（必须为 http/https）"
    if not (_nonempty(announcement_url) or _nonempty(application_url)):
        return "缺少有效的公告/投递链接（两者均缺失）"
    return None


# ---------------------------------------------------------------------------
# 预览分类（数据库零写入）
# ---------------------------------------------------------------------------


def classify_records(
    records: Sequence[Mapping],
    conn: sqlite3.Connection,
    confirmations: Mapping[str, Mapping] | None = None,
) -> dict[str, Any]:
    """预览阶段分类：pending / invalid / duplicate / new（互斥，总数闭合）。

    **数据库零写入**：对数据库只做 SELECT 查重，页面刷新、切换工作表或
    预览时调用本方法是安全的。

    Args:
        records: 解析记录列表（opportunity_importer 的输出）。
        conn: SQLite 连接（仅用于查重 SELECT）。
        confirmations: ``{preview_id: 确认条目}``；preview_id 见
            `dedup_service.preview_identifier`（即 source_sheet+source_row）。

    Returns:
        ``{"total", "counts", "items"}``；``items[category]`` 为分类明细
        列表，每项含 ``record`` / ``preview_id`` / ``reason`` /
        ``dedupe_key`` / ``prepared``（仅 new 有值，为写入准备记录）。
    """
    confirmations = confirmations or {}
    items: dict[str, list[dict[str, Any]]] = {cat: [] for cat in CATEGORIES}
    batch_keys: set[str] = set()
    total = 0

    for record in records:
        if record is None:
            continue
        total += 1
        preview_id = preview_identifier(record)
        record_type = str(record.get("record_type") or "")

        if record_type == "unknown":
            confirmation = confirmations.get(preview_id)
            if not confirmation:
                items[CATEGORY_PENDING].append(
                    _item(record, preview_id, "unknown 未确认，保持待确认")
                )
                continue
            built, error = _build_from_confirmation(record, confirmation)
            if error is not None:
                category, reason = error
                items[category].append(_item(record, preview_id, reason))
                continue
            work = built
        else:
            # 可靠布局（campaign/job）：复制后统一验证（防御性；
            # 解析器已保证大部分字段，但缺公司/无效 URL 等仍需拦截）
            work = dict(record)

        record_type = str(work.get("record_type") or "")
        reason = _validate_record(work, record_type)
        if reason is not None:
            items[CATEGORY_INVALID].append(_item(record, preview_id, reason))
            continue

        dedupe_key = compute_dedupe_key(work)
        if dedupe_key is None:
            items[CATEGORY_INVALID].append(
                _item(record, preview_id, "无法计算 dedupe_key（record_type 非法）")
            )
            continue

        # 批次内重复优先（同一批先前记录已出现相同 key）
        if dedupe_key in batch_keys:
            items[CATEGORY_DUPLICATE].append(
                _item(record, preview_id, "与本批次先前记录重复", dedupe_key)
            )
            continue
        # 数据库已有重复
        if find_by_dedupe_key(conn, dedupe_key) is not None:
            items[CATEGORY_DUPLICATE].append(
                _item(record, preview_id, "与数据库已有记录重复", dedupe_key)
            )
            continue

        batch_keys.add(dedupe_key)
        work["dedupe_key"] = dedupe_key
        items[CATEGORY_NEW].append(
            _item(record, preview_id, "验证通过，可导入", dedupe_key, prepared=work)
        )

    counts = {cat: len(items[cat]) for cat in CATEGORIES}
    return {"total": total, "counts": counts, "items": items}


# ---------------------------------------------------------------------------
# 正式导入（事务写入）
# ---------------------------------------------------------------------------

# 参数化 INSERT（docs/DATA_MODEL.md §1）。不含 id/created_at/updated_at
# （数据库自增/默认），不含 notes/opened_at/applied_at（默认 NULL）。
_INSERT_FIELDS: tuple[str, ...] = (
    "record_type",
    "display_title",
    "job_title",
    "job_categories",
    "company_name",
    "industry",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "deadline",
    "announcement_title",
    "announcement_url",
    "application_url",
)

_INSERT_SQL = """
INSERT INTO opportunities (
    record_type, display_title, job_title, job_categories, company_name,
    industry, recruitment_type, target_cohort, education_requirement,
    location, deadline, announcement_title, announcement_url, application_url,
    source_sheet, source_row, import_batch_id, dedupe_key, raw_data,
    status, priority
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_params(work: Mapping, batch_id: str) -> tuple:
    """构造 INSERT 参数（raw_data 序列化为合法 JSON 文本）。"""
    values: list[Any] = [work.get(field) for field in _INSERT_FIELDS]
    values.append(work.get("source_sheet"))
    values.append(int(work.get("source_row")))  # type: ignore[arg-type]
    values.append(batch_id)
    values.append(work["dedupe_key"])
    values.append(json.dumps(work.get("raw_data"), ensure_ascii=False))
    values.append("discovered")  # 默认状态（docs/DATA_MODEL.md §3.1）
    values.append("low")  # 默认优先级（docs/DATA_MODEL.md §3.3）
    return tuple(values)


def import_opportunities(
    records: Sequence[Mapping],
    conn: sqlite3.Connection,
    confirmations: Mapping[str, Mapping] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """分类并事务写入 new 类记录，返回**实际**导入报告。

    只有调用方（页面"确认导入"按钮）显式调用本方法才会写库；
    预览请使用 `classify_records()`（零写入）。

    写入策略：
    - 事务写入：全部 INSERT 在同一事务内，任何异常整体回滚并向上抛出；
    - 不使用 INSERT OR REPLACE；重复记录不写入、不覆盖、不修改原记录；
    - 每条写入前在事务内再次按 `dedupe_key` 查重（同连接可见本事务
      已写入的行，防御同批重复与竞态）。

    Args:
        records: 解析记录列表。
        conn: SQLite 连接。
        confirmations: unknown 人工确认条目（见模块 docstring）。
        batch_id: 导入批次 ID；缺省自动生成（uuid4.hex）。

    Returns:
        ``{"batch_id", "total", "counts", "inserted", "inserted_ids", "items"}``；
        ``counts`` / ``items`` 为实际分类与实际写入结果
        （写入时发现重复的记录会从 new 改判为 duplicate）。
    """
    batch = batch_id or uuid.uuid4().hex
    classification = classify_records(records, conn, confirmations)
    items: dict[str, list[dict[str, Any]]] = {
        cat: list(classification["items"][cat]) for cat in CATEGORIES
    }

    inserted_ids: list[int] = []
    written_dupes: list[dict[str, Any]] = []

    try:
        for item in items[CATEGORY_NEW]:
            work = item["prepared"]
            # 事务内二次查重：本连接可见同事务已写入的行
            if find_by_dedupe_key(conn, work["dedupe_key"]) is not None:
                written_dupes.append(item)
                continue
            cursor = conn.execute(_INSERT_SQL, _insert_params(work, batch))
            inserted_ids.append(int(cursor.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 报告使用实际写入结果：写入时发现重复的记录改判为 duplicate
    if written_dupes:
        dupe_ids = {id(item) for item in written_dupes}
        items[CATEGORY_NEW] = [
            item for item in items[CATEGORY_NEW] if id(item) not in dupe_ids
        ]
        for item in written_dupes:
            adjusted = dict(item)
            adjusted["reason"] = "写入时发现重复，未写入（不覆盖原记录）"
            items[CATEGORY_DUPLICATE].append(adjusted)

    counts = {cat: len(items[cat]) for cat in CATEGORIES}
    return {
        "batch_id": batch,
        "total": classification["total"],
        "counts": counts,
        "inserted": len(inserted_ids),
        "inserted_ids": inserted_ids,
        "items": items,
    }


# ---------------------------------------------------------------------------
# unknown 人工确认分页与确认收集（任务 5 修复四，纯函数，可独立测试）
# ---------------------------------------------------------------------------


def paginate_unknown(
    total: int, page_size: int, current_page: int
) -> dict[str, Any]:
    """unknown 人工确认分页计算（任务 5 修复四）。

    真实工作表可能包含数千到两万多条 unknown，禁止一次渲染全部控件。

    Args:
        total: unknown 记录总数。
        page_size: 每页条数（自动截断到 MAX_PAGE_SIZE）。
        current_page: 当前页码（1-based，自动夹紧到有效范围）。

    Returns:
        ``{"page_size", "current_page", "total_pages", "start", "end",
        "has_prev", "has_next"}``；``start`` / ``end`` 为切片索引 [start, end)。
    """
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    total = max(0, int(total))
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = max(1, min(int(current_page), total_pages))
    start = (current_page - 1) * page_size
    end = min(start + page_size, total)
    return {
        "page_size": page_size,
        "current_page": current_page,
        "total_pages": total_pages,
        "start": start,
        "end": end,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
    }


# ---------------------------------------------------------------------------
# 任务 8：状态流转服务（mark_as_opened / confirm_applied）
# ---------------------------------------------------------------------------

# docs/DATA_MODEL.md §3.2 状态流转规则
# mark_as_opened：仅 discovered / shortlisted 可变为 opened
_OPENABLE_STATUSES = frozenset(("discovered", "shortlisted"))

# confirm_applied：discovered / shortlisted / opened / applying 可变为 applied
# assessment / interview / offer / rejected / withdrawn 不得降回 applied
_APPLIABLE_STATUSES = frozenset(("discovered", "shortlisted", "opened", "applying"))

# 不得被 confirm_applied 降级的高/终态
_APPLIED_TERMINAL_STATUSES = frozenset(
    ("assessment", "interview", "offer", "rejected", "withdrawn")
)

# 手动状态更新白名单（docs 要求其他手动状态更新时使用显式白名单）
_MANUAL_STATUS_WHITELIST = frozenset(
    (
        "assessment",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
    )
)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _is_valid_http_url(url: object) -> bool:
    """判断 URL 是否为有效 http/https 链接。

    仅接受 ``http://`` 或 ``https://`` 且有 netloc 的 URL。
    """
    from urllib.parse import urlparse

    if not isinstance(url, str):
        return False
    text = url.strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _select_open_url(record: Mapping) -> str | None:
    """根据 record_type 选择要打开的 URL（任务 8）。

    - job：仅使用有效 application_url；
    - campaign：优先 application_url，不存在时回退 announcement_url；
    - 无有效 http/https 链接时返回 None。
    """
    record_type = str(record.get("record_type") or "")
    app_url = str(record.get("application_url") or "").strip()
    ann_url = str(record.get("announcement_url") or "").strip()

    if record_type == "job":
        return app_url if _is_valid_http_url(app_url) else None

    # campaign：优先 application_url，回退 announcement_url
    if _is_valid_http_url(app_url):
        return app_url
    if _is_valid_http_url(ann_url):
        return ann_url
    return None


def mark_as_opened(
    opp_id: int, conn: sqlite3.Connection
) -> dict[str, Any]:
    """打开投递链接并将状态置为 opened（任务 8）。

    docs/DATA_MODEL.md §3.2 自动流转规则：
    - 从数据库**重新读取**记录，不信任 UI 中的旧状态；
    - job：仅使用有效 application_url；
    - campaign：优先 application_url，回退 announcement_url；
    - 无有效 http/https 链接时**不得更新状态**；
    - 仅 ``discovered`` / ``shortlisted`` 可变为 ``opened``；
    - ``opened`` 及更高状态不得被降级为 ``opened``；
    - **高阶段记录仍可打开链接**，但 status / opened_at / applied_at
      均不得变化；
    - 首次变为 ``opened`` 时写入 ``opened_at``；
    - **服务层不直接打开浏览器**，仅返回 URL + ``should_open`` 供 UI 使用。

    Returns:
        ``{"action": str, "url": str|None, "should_open": bool,
        "status": str, "message": str}``

        - ``action="opened"``：discovered/shortlisted → opened，
          ``should_open=True``，``url`` 为待打开链接；
        - ``action="opened_without_status_change"``：高阶段状态，
          ``should_open=True``，``url`` 为待打开链接，但**不改状态**；
        - ``action="no_link"``：无可用 http/https 链接，
          ``should_open=False``，不改状态；
        - ``action="not_found"``：记录不存在，``should_open=False``。
    """
    cur = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
    row = cur.fetchone()
    if row is None:
        return {
            "action": "not_found",
            "url": None,
            "should_open": False,
            "status": "",
            "message": "记录不存在",
        }

    record = dict(row)
    current_status = str(record.get("status") or "")

    # 选择 URL（所有状态都尝试选择链接）
    url = _select_open_url(record)
    if url is None:
        return {
            "action": "no_link",
            "url": None,
            "should_open": False,
            "status": current_status,
            "message": "无可用 http/https 链接，未更新状态",
        }

    # 高阶段状态：仍可打开链接，但不改状态
    if current_status not in _OPENABLE_STATUSES:
        return {
            "action": "opened_without_status_change",
            "url": url,
            "should_open": True,
            "status": current_status,
            "message": f"链接已打开，状态保持为 {current_status}",
        }

    # discovered / shortlisted → opened，首次写入 opened_at
    now = _now_iso()
    existing_opened_at = record.get("opened_at")
    if existing_opened_at:
        conn.execute(
            "UPDATE opportunities SET status = ?, updated_at = ? WHERE id = ?",
            ("opened", now, opp_id),
        )
    else:
        conn.execute(
            "UPDATE opportunities "
            "SET status = ?, opened_at = ?, updated_at = ? "
            "WHERE id = ?",
            ("opened", now, now, opp_id),
        )
    conn.commit()

    return {
        "action": "opened",
        "url": url,
        "should_open": True,
        "status": "opened",
        "message": "已标记为 opened",
    }


def confirm_applied(
    opp_id: int, conn: sqlite3.Connection
) -> dict[str, Any]:
    """用户手动确认已投递，将状态置为 applied（任务 8）。

    docs/DATA_MODEL.md §3.2 手动流转规则：
    - **只能由用户明确点击"确认已投递"触发**；
    - 更新为 ``applied`` 并写入 ``applied_at``；
    - ``assessment`` / ``interview`` / ``offer`` / ``rejected`` /
      ``withdrawn`` 等更高或终态不得降回 ``applied``；
    - 不得因点击链接自动调用。

    Returns:
        ``{"action": str, "status": str, "message": str}``

        - ``action="applied"``：成功变为 applied；
        - ``action="no_change"``：状态不可降级或已是 applied；
        - ``action="not_found"``：记录不存在。
    """
    cur = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
    row = cur.fetchone()
    if row is None:
        return {
            "action": "not_found",
            "status": "",
            "message": "记录不存在",
        }

    record = dict(row)
    current_status = str(record.get("status") or "")

    # 已是 applied → 幂等，不重复写入
    if current_status == "applied":
        return {
            "action": "no_change",
            "status": current_status,
            "message": "已经是 applied 状态",
        }

    # 高/终态不得降回 applied
    if current_status in _APPLIED_TERMINAL_STATUSES:
        return {
            "action": "no_change",
            "status": current_status,
            f"message": f"当前状态 {current_status} 不可降级为 applied",
        }

    # discovered / shortlisted / opened / applying → applied
    now = _now_iso()
    conn.execute(
        "UPDATE opportunities "
        "SET status = ?, applied_at = ?, updated_at = ? "
        "WHERE id = ?",
        ("applied", now, now, opp_id),
    )
    conn.commit()

    return {
        "action": "applied",
        "status": "applied",
        "message": "已确认投递",
    }


def update_status(
    opp_id: int, new_status: str, conn: sqlite3.Connection
) -> dict[str, Any]:
    """手动更新状态（显式白名单，任务 8 docs 要求）。

    只允许 ``_MANUAL_STATUS_WHITELIST`` 中的状态（assessment / interview /
    offer / rejected / withdrawn）。``discovered`` / ``shortlisted`` /
    ``opened`` / ``applying`` / ``applied`` 不经此方法更新（由
    ``mark_as_opened`` / ``confirm_applied`` 专门处理）。

    所有 SQL 参数化，不接受任意字符串写入 status。
    """
    if new_status not in _MANUAL_STATUS_WHITELIST:
        return {
            "action": "rejected",
            "status": "",
            "message": f"status {new_status} 不在手动更新白名单中",
        }

    cur = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
    row = cur.fetchone()
    if row is None:
        return {
            "action": "not_found",
            "status": "",
            "message": "记录不存在",
        }

    record = dict(row)
    current_status = str(record.get("status") or "")
    now = _now_iso()
    conn.execute(
        "UPDATE opportunities SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, opp_id),
    )
    conn.commit()

    return {
        "action": "updated",
        "status": new_status,
        "message": f"状态已更新为 {new_status}",
    }


# ---------------------------------------------------------------------------
# 任务 9：优先级持久化
# ---------------------------------------------------------------------------

# 优先级白名单（docs/DATA_MODEL.md §3.3）
_PRIORITY_WHITELIST = frozenset(("high", "medium", "low"))


def set_priority(
    opp_id: int, priority: str, conn: sqlite3.Connection
) -> dict[str, Any]:
    """更新机会优先级（任务 9，参数化 SQL，白名单校验）。

    docs/DATA_MODEL.md §3.3：仅允许 ``high`` / ``medium`` / ``low``。
    不接受任意字符串写入 priority。

    Returns:
        ``{"action": str, "priority": str, "message": str}``

        - ``action="updated"``：优先级已更新；
        - ``action="rejected"``：priority 不在白名单中；
        - ``action="not_found"``：记录不存在。
    """
    if priority not in _PRIORITY_WHITELIST:
        return {
            "action": "rejected",
            "priority": "",
            "message": f"priority {priority} 不在允许列表（high/medium/low）",
        }

    cur = conn.execute("SELECT id FROM opportunities WHERE id = ?", (opp_id,))
    if cur.fetchone() is None:
        return {
            "action": "not_found",
            "priority": "",
            "message": "记录不存在",
        }

    now = _now_iso()
    conn.execute(
        "UPDATE opportunities SET priority = ?, updated_at = ? WHERE id = ?",
        (priority, now, opp_id),
    )
    conn.commit()

    return {
        "action": "updated",
        "priority": priority,
        "message": f"优先级已更新为 {priority}",
    }


def collect_confirmations(
    store: Mapping, pending_records: Sequence[Mapping]
) -> dict[str, Mapping]:
    """从 session_state 存储构建全部确认条目（任务 5 修复四）。

    ``store`` 键命名空间为 ``file_key:sheet_name``（由页面维护），
    切换页面/工作表/文件时不会串数据。本函数把 store 中所有**有效**
    确认（record_type 为 campaign/job）收集为 ``classify_records`` 可用
    的字典；当前页以外的确认结果也保留在 store 中。

    Returns:
        ``{preview_id: 确认条目}``，只包含 record_type 为 campaign/job 的条目。
    """
    confirmations: dict[str, Mapping] = {}
    for rec in pending_records:
        pid = preview_identifier(rec)
        entry = store.get(pid)
        if not isinstance(entry, Mapping):
            continue
        if entry.get("record_type") in CONFIRMABLE_RECORD_TYPES:
            confirmations[pid] = entry
    return confirmations


def count_confirmed(
    store: Mapping, pending_records: Sequence[Mapping]
) -> tuple[int, int]:
    """返回 (已确认数, 尚未确认数)，基于全部 unknown 而非当前页。

    已确认 = store 中存在 record_type 为 campaign/job 的条目；
    尚未确认 = unknown 总数 - 已确认。
    """
    confirmed = 0
    for rec in pending_records:
        pid = preview_identifier(rec)
        entry = store.get(pid)
        if isinstance(entry, Mapping) and entry.get(
            "record_type"
        ) in CONFIRMABLE_RECORD_TYPES:
            confirmed += 1
    return confirmed, len(pending_records) - confirmed


# ---------------------------------------------------------------------------
# 任务 7：只读机会查询与筛选（纯函数，不写数据库）
# ---------------------------------------------------------------------------

# 全量视图分页上限（任务 7 §六）
DASHBOARD_MAX_PAGE_SIZE = 100
DASHBOARD_DEFAULT_PAGE_SIZE = 20


class _FilterSentinel:
    """哨兵值，不与任何字符串或其它类型相等（仅与自身 identity 相等）。

    任务 7 修复三：用户可见标签（"全部" / "未填写"）不得直接作为内部
    筛选值，否则与真实公司名"全部"或真实地区"未填写"冲突。哨兵对象
    保证内部值唯一，``format_func`` 负责在 UI 显示中文标签。
    """

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<{self.label}>"


# 筛选器"全部"哨兵（内部值）与用户可见标签
FILTER_ALL = _FilterSentinel("FILTER_ALL")
FILTER_ALL_LABEL = "全部"

# 缺失地区哨兵（内部值）与用户可见标签
MISSING_LOCATION = _FilterSentinel("MISSING_LOCATION")
MISSING_LOCATION_LABEL = "未填写"

# 读取看板所需字段（一次 SELECT）
_DASHBOARD_COLUMNS: tuple[str, ...] = (
    "id",
    "record_type",
    "display_title",
    "job_title",
    "job_categories",
    "company_name",
    "industry",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "deadline",
    "announcement_title",
    "announcement_url",
    "application_url",
    "source_sheet",
    "source_row",
    "priority",
    "status",
)


def fetch_all_opportunities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """从数据库读取全部机会（普通 dict 列表，任务 7 只读）。

    **只读**：不执行 INSERT/UPDATE/DELETE/ALTER，不修改任何记录。
    返回普通 dict，不把 sqlite3.Row 暴露给 UI。
    """
    columns_sql = ", ".join(_DASHBOARD_COLUMNS)
    cur = conn.execute(f"SELECT {columns_sql} FROM opportunities")
    return [{key: row[key] for key in row.keys()} for row in cur.fetchall()]


def filter_opportunities(
    opportunities: Sequence[Mapping],
    *,
    company_name: str | _FilterSentinel | None = None,
    location: str | _FilterSentinel | None = None,
    record_type: str | _FilterSentinel | None = None,
    status: str | _FilterSentinel | None = None,
) -> list[dict[str, Any]]:
    """筛选机会（纯函数，任务 7 §四，修复三）。

    所有参数为 ``None`` 或 ``FILTER_ALL``（哨兵）时表示“不过滤该维度”。
    支持单项与多条件组合筛选；“全部”不得误过滤。

    - ``company_name``：精确匹配 strip 后的公司名（真实公司名"全部"
      不会与 ``FILTER_ALL`` 哨兵冲突，因 ``is`` 比较）；
    - ``location``：精确匹配 location；``MISSING_LOCATION``（哨兵）
      匹配空/缺失的 location；真实地区"未填写"是字符串，不与哨兵冲突；
    - ``record_type``：campaign / job；
    - ``status``：任意合法 status。

    返回新列表（不修改输入）。
    """
    result: list[dict[str, Any]] = []
    for opp in opportunities:
        if company_name is not None and company_name is not FILTER_ALL:
            if str(opp.get("company_name") or "").strip() != company_name:
                continue
        if location is not None and location is not FILTER_ALL:
            opp_loc = str(opp.get("location") or "").strip()
            if location is MISSING_LOCATION:
                # 哨兵：匹配空/缺失的 location（不匹配真实"未填写"字符串）
                if opp_loc != "":
                    continue
            elif opp_loc != location:
                continue
        if record_type is not None and record_type is not FILTER_ALL:
            if str(opp.get("record_type") or "") != record_type:
                continue
        if status is not None and status is not FILTER_ALL:
            if str(opp.get("status") or "") != status:
                continue
        result.append(dict(opp))
    return result


def get_filter_options(
    opportunities: Sequence[Mapping],
) -> dict[str, list[Any]]:
    """根据数据库实际值生成筛选选项（任务 7 §四，修复三）。

    返回 ``{"company_name": [...], "location": [...],
    "record_type": [...], "status": [...]}``。

    - 每个列表首项为 ``FILTER_ALL`` 哨兵（UI 显示"全部"）；
    - location 缺失值用 ``MISSING_LOCATION`` 哨兵表示（UI 显示"未填写"）；
    - 真实公司名"全部"和真实地区"未填写"作为普通字符串保留在选项中，
      与哨兵**不冲突**（``is`` 比较）；
    - 选项列表中不存在重复的"全部"字符串。
    """
    companies: set[str] = set()
    locations: set[str] = set()
    record_types: set[str] = set()
    statuses: set[str] = set()
    has_missing_location = False
    for opp in opportunities:
        companies.add(str(opp.get("company_name") or "").strip())
        loc = str(opp.get("location") or "").strip()
        if loc:
            locations.add(loc)
        else:
            has_missing_location = True
        record_types.add(str(opp.get("record_type") or ""))
        statuses.add(str(opp.get("status") or ""))
    # 移除空公司名（company_name NOT NULL，但 strip 后可能为空）
    companies.discard("")

    # location 选项：哨兵 + 缺失哨兵 + 真实地区字符串
    location_options: list[Any] = [FILTER_ALL]
    if has_missing_location:
        location_options.append(MISSING_LOCATION)
    location_options.extend(sorted(locations))

    return {
        "company_name": [FILTER_ALL] + sorted(companies),
        "location": location_options,
        "record_type": [FILTER_ALL] + sorted(record_types),
        "status": [FILTER_ALL] + sorted(statuses),
    }


def paginate_list(
    total: int, page_size: int, current_page: int
) -> dict[str, Any]:
    """看板分页计算（任务 7 §六，纯函数）。

    单页上限 ``DASHBOARD_MAX_PAGE_SIZE``（100）。
    """
    page_size = max(1, min(int(page_size), DASHBOARD_MAX_PAGE_SIZE))
    total = max(0, int(total))
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = max(1, min(int(current_page), total_pages))
    start = (current_page - 1) * page_size
    end = min(start + page_size, total)
    return {
        "page_size": page_size,
        "current_page": current_page,
        "total_pages": total_pages,
        "start": start,
        "end": end,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
    }
