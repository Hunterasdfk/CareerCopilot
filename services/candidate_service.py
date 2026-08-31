"""候选清单与公司机会数量检查（任务 6）。

职责（docs/TASKS.md 任务 6、docs/ARCHITECTURE.md §1/§2/§6、
docs/DATA_MODEL.md §3.3/§6）：

- `get_company_coverage(conn)`：对 `opportunities` 表中的**全部机会**
  （campaign + job，全部合法 status）进行公司覆盖检查，按公司返回
  全部已排序机会 + `total_count` / `coverage_gap` /
  `highlighted_top_three` / `campaign_only` / `campaign_only_message`。
  **不写数据库、不修改任何记录、不新增字段**。
- `mark_campaign_only_companies(coverage)`：纯转换函数，对
  `campaign_only=True` 的公司追加提示文字；**不修改调用方输入**，
  返回新结构。

确定性排序规则（docs/DATA_MODEL.md §6 + 任务 6 指令）：
1. ``priority``：high > medium > low（**不按字符串字母顺序**，用显式
   优先级映射）；
2. 同优先级：``job`` 优先于 ``campaign``；
3. 同优先级同类型：``id`` 升序（先导入的优先）。

公司间排序：按规范化（strip）后的 ``company_name`` 升序；
**大小写敏感**（稳定选择，写入测试固定）。

Top 3 突出但不截断：``highlighted_top_three`` 等于排序后的前 3 条
（不足 3 条返回实际数量），``opportunities`` 始终返回全部机会。

安全约束：不访问网络；不读取任何真实源数据目录；不引用默认数据库
路径；只通过调用方传入的 ``sqlite3.Connection`` 读取数据。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# docs/DATA_MODEL.md §3.3 优先级枚举 + 显式排序权重（high=0, medium=1, low=2）
_PRIORITY_WEIGHT: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# record_type 排序权重（job 优先于 campaign，docs/DATA_MODEL.md §6.1）
_TYPE_WEIGHT: dict[str, int] = {"job": 0, "campaign": 1}

# 公司机会少于 3 个时的缺口阈值（docs/PRODUCT_SPEC.md §4）
_COVERAGE_THRESHOLD = 3

# 仅有 campaign 的公司提示（docs/PRODUCT_SPEC.md §4、docs/TASKS.md 任务 6）
_CAMPAIGN_ONLY_MESSAGE = "需进入官网选择具体岗位"

# 读取所需字段（一次 SELECT，避免按公司重复查询）
_SELECT_COLUMNS: tuple[str, ...] = (
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
    "import_batch_id",
    "dedupe_key",
    "priority",
    "status",
    "notes",
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """把 sqlite3.Row 转换为普通 dict（不把 Row 暴露给 UI）。"""
    return {key: row[key] for key in row.keys()}


def _normalize_company(name: object) -> str:
    """公司名归一化（仅 strip，不修改数据库原值）。"""
    if name is None:
        return ""
    return str(name).strip()


def _sort_key(opp: Mapping) -> tuple[int, int, int]:
    """单公司内机会排序键（确定性）。

    1. priority 权重（high=0 < medium=1 < low=2）；
    2. record_type 权重（job=0 < campaign=1）；
    3. id 升序（先导入的优先）。
    """
    priority = str(opp.get("priority") or "low")
    record_type = str(opp.get("record_type") or "")
    return (
        _PRIORITY_WEIGHT.get(priority, 99),
        _TYPE_WEIGHT.get(record_type, 99),
        int(opp.get("id") or 0),
    )


# ---------------------------------------------------------------------------
# 公司覆盖检查（只读，不写库）
# ---------------------------------------------------------------------------


def get_company_coverage(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """按公司返回全部已排序机会与覆盖检查结果（任务 6）。

    **只读**：不写数据库、不修改任何记录、不新增字段。对数据库只做
    一次 SELECT 读取所需数据，避免按公司重复查询。

    数据范围（docs/TASKS.md 任务 6）：
    - 包含 campaign 和 job；
    - 包含**全部合法 status**，不得只筛选 status=shortlisted，不得
      因 opened/applied/interview 等状态把机会移出覆盖检查；
    - 这里的“全部机会”指数据库中的全部机会，不是仅 Top 3，也不是
      仅 shortlisted。

    Args:
        conn: 调用方传入的 sqlite3.Connection（本模块不引用默认数据库路径）。

    Returns:
        按公司名升序排列的列表，每家公司::

            {
                "company_name": str,
                "opportunities": list[dict],     # 全部已排序机会
                "total_count": int,               # == len(opportunities)
                "coverage_gap": int,             # max(0, 3 - total_count)
                "highlighted_top_three": list,   # 前 3 条（不截断）
                "campaign_only": bool,           # 全部为 campaign 时 True
                "campaign_only_message": str|None,
            }
    """
    columns_sql = ", ".join(_SELECT_COLUMNS)
    cur = conn.execute(f"SELECT {columns_sql} FROM opportunities")
    rows: list[sqlite3.Row] = cur.fetchall()
    # 转为普通 dict 后交给纯函数分组计算（不把 Row 暴露给 UI/排序键）
    opportunities = [_row_to_dict(row) for row in rows]
    return build_company_coverage(opportunities)


def build_company_coverage(
    opportunities: Sequence[Mapping],
) -> list[dict[str, Any]]:
    """对普通 dict 列表按公司分组计算覆盖检查结果（任务 7 纯函数）。

    任务 7 候选清单视图复用本函数：对**筛选后**的机会按公司重新分组，
    每家公司展示全部筛选后机会（不截断），并计算 total_count /
    coverage_gap / highlighted_top_three / campaign_only /
    campaign_only_message。

    公开返回结构与 ``get_company_coverage`` 完全一致，保持任务 6 测试
    继续通过。排序规则见模块 docstring。

    Args:
        opportunities: 普通 dict 列表（已从数据库读取并转换）。

    Returns:
        按公司名升序排列的覆盖检查结果列表。
    """
    # 按公司分组（规范化后的公司名作为分组键）
    grouped: dict[str, list[Mapping]] = {}
    for opp in opportunities:
        company = _normalize_company(opp.get("company_name"))
        grouped.setdefault(company, []).append(opp)

    coverage: list[dict[str, Any]] = []
    for company in sorted(grouped.keys()):
        company_opps = grouped[company]
        # 排序（确定性：priority > record_type > id）
        sorted_opps = sorted(company_opps, key=_sort_key)
        total_count = len(sorted_opps)
        coverage_gap = max(0, _COVERAGE_THRESHOLD - total_count)
        highlighted_top_three = sorted_opps[:3]
        # campaign_only：至少一条机会且全部为 campaign
        record_types = {opp["record_type"] for opp in sorted_opps}
        campaign_only = bool(record_types) and record_types == {"campaign"}

        coverage.append(
            {
                "company_name": company,
                "opportunities": sorted_opps,
                "total_count": total_count,
                "coverage_gap": coverage_gap,
                "highlighted_top_three": highlighted_top_three,
                "campaign_only": campaign_only,
                "campaign_only_message": (
                    _CAMPAIGN_ONLY_MESSAGE if campaign_only else None
                ),
            }
        )

    return coverage


# ---------------------------------------------------------------------------
# campaign_only 标注（纯转换，不修改调用方输入）
# ---------------------------------------------------------------------------


def mark_campaign_only_companies(
    coverage: Sequence[Mapping],
) -> list[dict[str, Any]]:
    """对 campaign_only 公司追加提示文字（任务 6）。

    纯转换函数，**不修改调用方原始输入**、不写数据库、不删除任何机会：
    - ``campaign_only=True``：设置 ``campaign_only_message`` 为
      ``"需进入官网选择具体岗位"``；
    - ``campaign_only=False``（混合 campaign/job 或只有 job）：
      ``campaign_only_message`` 为 None；
    - 不重复追加相同提示（幂等：对已标注的输入再调用仍保持单条提示）。

    返回**新结构**（深拷贝每家公司条目，列表顺序保持不变）。
    """
    result: list[dict[str, Any]] = []
    for company in coverage:
        entry = dict(company)  # 浅拷贝顶层，不修改原 Mapping
        entry["opportunities"] = list(company.get("opportunities") or [])
        entry["highlighted_top_three"] = list(
            company.get("highlighted_top_three") or []
        )
        if entry.get("campaign_only"):
            entry["campaign_only_message"] = _CAMPAIGN_ONLY_MESSAGE
        else:
            entry["campaign_only_message"] = None
        result.append(entry)
    return result
