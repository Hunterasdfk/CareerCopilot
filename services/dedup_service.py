"""去重服务（任务 3）。

按 `record_type` 差异化生成 `dedupe_key`（docs/DATA_MODEL.md §7、docs/TASKS.md 任务 3）：

1. **campaign**：`company_name` + `recruitment_type` + `target_cohort`
   + `announcement_url`（无 URL 时回退 `announcement_title`）；
2. **job**：`company_name` + `job_title` + `location`
   + `application_url`（无 URL 时回退 `job_categories`）；
3. **unknown**：**不生成最终 `dedupe_key`**。`unknown` 只存在于解析结果与
   导入预览阶段（docs/ARCHITECTURE.md §6），仅用临时 `preview_id`（或
   `source_sheet` + `source_row`）做预览阶段识别；用户确认改为
   `campaign`/`job` 后才生成 `dedupe_key` 并入库。

实现细节：
- 各字段先做 strip 归一化，再以 ASCII 单元分隔符（\\x1f）拼接，
  避免业务取值本身包含分隔字符导致键冲突；
- 拼接结果做 sha256 摘要，并加 `record_type` 前缀（形如
  `campaign_<64位十六进制>`，总长 73 字符，满足 VARCHAR(100)）；
  前缀保证同一业务取值组合的 campaign 与 job 不会被误判为同一条记录。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping

# 字段拼接分隔符：ASCII 单元分隔符，不会出现在正常业务取值中
_SEP = "\x1f"

# 各记录类型参与 dedupe_key 的字段（最后一个是链接字段的回退字段）
_KEY_FIELDS: dict[str, tuple[str, str, str, str, str]] = {
    "campaign": (
        "company_name",
        "recruitment_type",
        "target_cohort",
        "announcement_url",
        "announcement_title",
    ),
    "job": (
        "company_name",
        "job_title",
        "location",
        "application_url",
        "job_categories",
    ),
}


def _clean(value: object) -> str:
    """把任意取值归一化为去除首尾空白的字符串（None 记为空串）。"""
    if value is None:
        return ""
    return str(value).strip()


def compute_dedupe_key(record: Mapping) -> str | None:
    """计算一条记录的 `dedupe_key`。

    Args:
        record: 含标准字段的记录（dict 或 Mapping），至少含 `record_type`。

    Returns:
        - `record_type` 为 `campaign` / `job`：返回形如
          `campaign_<hash>` / `job_<hash>` 的键；
        - `record_type` 为 `unknown` 或缺失：返回 None，
          **不生成最终数据库 `dedupe_key`**（预览阶段用
          preview_identifier 临时标识）。
    """
    record_type = _clean(record.get("record_type"))
    fields = _KEY_FIELDS.get(record_type)
    if fields is None:
        return None

    company_field, second_field, third_field, url_field, url_fallback_field = fields
    link = _clean(record.get(url_field)) or _clean(record.get(url_fallback_field))

    parts = (
        _clean(record.get(company_field)),
        _clean(record.get(second_field)),
        _clean(record.get(third_field)),
        link,
    )
    digest = hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{record_type}_{digest}"


def preview_identifier(record: Mapping) -> str:
    """为预览阶段记录（含 `unknown`）生成临时标识，不用于数据库。

    优先使用调用方提供的 `preview_id`；否则用 `source_sheet` + `source_row`
    组合（docs/ARCHITECTURE.md §6 预览阶段识别方式）。
    """
    explicit = _clean(record.get("preview_id"))
    if explicit:
        return explicit

    sheet = _clean(record.get("source_sheet")) or "?"
    row = record.get("source_row")
    row_part = str(row).strip() if row is not None else "?"
    return f"preview:{sheet}:{row_part}"


def find_by_dedupe_key(
    conn: sqlite3.Connection, dedupe_key: str
) -> sqlite3.Row | None:
    """按 `dedupe_key` 查询现有记录；命中即视为重复（docs/DATA_MODEL.md §7）。"""
    cur = conn.execute(
        "SELECT * FROM opportunities WHERE dedupe_key = ?",
        (dedupe_key,),
    )
    return cur.fetchone()
