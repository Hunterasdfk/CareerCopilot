"""侧栏筛选器组件（任务 7，修复三）。

职责：渲染侧栏筛选控件（company_name / location / record_type / status）。
- 选项根据数据库实际值生成（由 get_filter_options 提供）；
- 内部值使用哨兵对象（FILTER_ALL / MISSING_LOCATION），与任何真实字符串
  区分；``format_func`` 负责在 UI 显示中文标签（"全部" / "未填写"）；
- 不修改数据库；筛选逻辑在 service 层（filter_opportunities）。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from services.opportunity_service import (
    FILTER_ALL,
    FILTER_ALL_LABEL,
    MISSING_LOCATION,
    MISSING_LOCATION_LABEL,
)


def _filter_format_func(value: Any) -> str:
    """通用 format_func（用于 record_type / status）。

    record_type 和 status 的合法真实值不会是"全部"，因此无需歧义消解：
    - ``FILTER_ALL`` 哨兵 → "全部"；
    - 其他 → 原样返回。
    """
    if value is FILTER_ALL:
        return FILTER_ALL_LABEL
    return str(value)


def _company_format_func(value: Any) -> str:
    """公司字段 format_func（任务 7 最后修正）。

    - ``FILTER_ALL`` 哨兵 → "全部"；
    - 真实公司名为"全部" → "全部（实际公司名）"以与哨兵区分；
    - 其他真实公司名 → 原样返回。
    """
    if value is FILTER_ALL:
        return FILTER_ALL_LABEL
    if isinstance(value, str) and value == FILTER_ALL_LABEL:
        return f"{value}（实际公司名）"
    return str(value)


def _location_format_func(value: Any) -> str:
    """地区字段 format_func（任务 7 最后修正）。

    - ``FILTER_ALL`` 哨兵 → "全部"；
    - ``MISSING_LOCATION`` 哨兵 → "未填写（字段为空）"；
    - 真实地区为"未填写" → "未填写（原始文本）"以与哨兵区分；
    - 其他真实地区 → 原样返回。
    """
    if value is FILTER_ALL:
        return FILTER_ALL_LABEL
    if value is MISSING_LOCATION:
        return f"{MISSING_LOCATION_LABEL}（字段为空）"
    if isinstance(value, str) and value == MISSING_LOCATION_LABEL:
        return f"{value}（原始文本）"
    return str(value)


def render_filters(
    options: dict[str, list[Any]],
) -> dict[str, Any]:
    """渲染侧栏筛选器，返回当前筛选值（含哨兵内部值）。

    公司与地区使用字段感知的 ``format_func``，确保哨兵与真实同名值
    在 UI 上可区分；record_type 与 status 用通用函数。

    Args:
        options: get_filter_options 返回的选项字典（含哨兵）。

    Returns:
        ``{"company_name": Any, "location": Any,
        "record_type": Any, "status": Any}``（值为哨兵或真实字符串）。
    """
    st.sidebar.subheader("筛选")
    company = st.sidebar.selectbox(
        "公司", options["company_name"],
        key="dash_filter_company",
        format_func=_company_format_func,
    )
    location = st.sidebar.selectbox(
        "地区", options["location"],
        key="dash_filter_location",
        format_func=_location_format_func,
    )
    record_type = st.sidebar.selectbox(
        "记录类型", options["record_type"],
        key="dash_filter_type",
        format_func=_filter_format_func,
    )
    status = st.sidebar.selectbox(
        "状态", options["status"],
        key="dash_filter_status",
        format_func=_filter_format_func,
    )
    return {
        "company_name": company,
        "location": location,
        "record_type": record_type,
        "status": status,
    }


def render_filter_summary(
    total_before: int, total_after: int, page_info: dict
) -> None:
    """在主区域显示筛选统计与分页信息。"""
    st.caption(
        f"筛选前总数：{total_before}  |  筛选后总数：{total_after}  |  "
        f"当前页：第 {page_info['current_page']} / {page_info['total_pages']} 页"
        f"（本页 {page_info['end'] - page_info['start']} 条，"
        f"每页 {page_info['page_size']} 条）"
    )
    if total_after == 0:
        st.info("没有符合筛选条件的机会。")


def render_empty_state() -> None:
    """筛选结果为 0 时显示清晰空状态。"""
    st.info("没有符合筛选条件的机会。")
