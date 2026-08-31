"""机会看板页面（任务 7 + 任务 8）。

职责边界（docs/ARCHITECTURE.md §1/§2）：
- 本页面**只负责交互**：视图切换、侧栏筛选、分页、卡片渲染；
- SQL 查询、筛选计算和公司覆盖计算全部在 service 层
  （fetch_all_opportunities / filter_opportunities /
  build_company_coverage）；
- **浏览/筛选/分页只读**：不执行 INSERT/UPDATE/DELETE/ALTER，不修改
  status/priority 或其他字段；
- **任务 8 例外**：仅在用户明确点击"打开投递链接"或"确认已投递"按钮
  时，打开独立可写连接执行状态更新（mark_as_opened / confirm_applied），
  完成后刷新看板数据。

数据库零持久化（任务 7 §二）：
- 浏览阶段使用 ``get_preview_connection()``：数据库不存在时返回 :memory:
  内存库，打开看板**不得创建 data/careercopilot.db**；
- 按钮触发的状态更新使用独立可写连接（``get_connection()``），仅在
  真实数据库存在时才渲染按钮（``interactive`` 标记）；
- 连接可靠关闭（try/finally）。

任务 8 安全要求：
- "打开投递链接"按钮点击后调用 ``mark_as_opened``，服务返回有效 URL
  后才通过本地默认浏览器打开；仅接受 http/https；无链接时不打开浏览器、
  不更新状态；
- "确认已投递"是独立操作，不能在打开链接时自动触发；
- 不使用不安全 HTML 或 JavaScript。
"""

from __future__ import annotations

import webbrowser
from typing import Any

import streamlit as st

from components.filters import render_filter_summary, render_filters
from components.opportunity_card import render_opportunity_card
from config.settings import DB_PATH
from database.db_handler import get_connection, get_preview_connection, init_db
from services.candidate_service import build_company_coverage
from services.opportunity_service import (
    DASHBOARD_DEFAULT_PAGE_SIZE,
    DASHBOARD_MAX_PAGE_SIZE,
    confirm_applied,
    fetch_all_opportunities,
    filter_opportunities,
    get_filter_options,
    mark_as_opened,
    paginate_list,
)

st.set_page_config(page_title="机会看板", page_icon="📋", layout="wide")
st.title("机会看板")
st.caption(
    '浏览全部机会（campaign / job 视觉区分）→ 侧栏筛选 → 全量视图或'
    '候选清单视图。浏览只读；点击「打开投递链接」或「确认已投递」按钮'
    '才会更新状态。'
)


# ---------------------------------------------------------------------------
# 任务 8：按钮动作处理
# ---------------------------------------------------------------------------


def _handle_action(action: dict[str, Any]) -> None:
    """处理卡片按钮动作（任务 8）。

    打开独立可写连接执行状态更新，完成后刷新看板。
    - ``open_link``：调用 mark_as_opened，返回有效 URL 后打开浏览器；
    - ``confirm_applied``：调用 confirm_applied，更新为 applied。
    """
    write_conn = get_connection()
    init_db(write_conn)
    try:
        if action["type"] == "open_link":
            result = mark_as_opened(action["opp_id"], write_conn)
            if result.get("should_open") and result.get("url"):
                # 服务返回 should_open=True 且 URL 有效 → 打开浏览器
                webbrowser.open(result["url"])
                if result["action"] == "opened":
                    st.toast("已标记为 opened")
                else:
                    # 高阶段状态：链接已打开，状态不变
                    st.info(result["message"])
            elif result["action"] == "no_link":
                st.warning("无可用 http/https 链接，未更新状态。")
            else:
                st.warning(result["message"])
        elif action["type"] == "confirm_applied":
            result = confirm_applied(action["opp_id"], write_conn)
            if result["action"] == "applied":
                st.toast("已确认投递")
            else:
                st.info(result["message"])
    finally:
        write_conn.close()

    # 刷新看板数据
    st.rerun()


# ---------------------------------------------------------------------------
# 数据库连接（只读浏览，零持久化）
# ---------------------------------------------------------------------------

# 真实数据库是否存在（决定是否渲染任务 8 操作按钮）
real_db_exists = DB_PATH.exists()

conn = get_preview_connection()

try:
    # ------------------------------------------------------------------
    # 读取全部机会 + 生成筛选选项
    # ------------------------------------------------------------------

    all_opps = fetch_all_opportunities(conn)
    options = get_filter_options(all_opps)

    # ------------------------------------------------------------------
    # 侧栏筛选器
    # ------------------------------------------------------------------

    filters = render_filters(options)
    page_size_options = [20, 50, 100]
    page_size = st.sidebar.selectbox(
        "每页显示条数", page_size_options, index=0, key="dash_page_size"
    )

    # ------------------------------------------------------------------
    # 筛选（纯函数，service 层）
    # ------------------------------------------------------------------

    filtered = filter_opportunities(
        all_opps,
        company_name=filters["company_name"],
        location=filters["location"],
        record_type=filters["record_type"],
        status=filters["status"],
    )
    total_before = len(all_opps)
    total_after = len(filtered)

    # ------------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------------

    view_mode = st.radio(
        "视图", ["全量视图", "候选清单视图"], horizontal=True, key="dash_view"
    )

    if view_mode == "全量视图":
        # --------------------------------------------------------------
        # 全量视图：分页渲染筛选后的全部机会
        # --------------------------------------------------------------

        current_page = st.session_state.get("dash_full_page", 1)
        page_info = paginate_list(total_after, page_size, current_page)
        st.session_state["dash_full_page"] = page_info["current_page"]

        render_filter_summary(total_before, total_after, page_info)

        if total_after > 0:
            # 页码选择器
            page_options = list(range(1, page_info["total_pages"] + 1))
            selected_page = st.selectbox(
                "页码",
                page_options,
                index=page_info["current_page"] - 1,
                key="dash_full_page_select",
            )
            st.session_state["dash_full_page"] = selected_page
            page_info = paginate_list(total_after, page_size, selected_page)

            page_opps = filtered[page_info["start"] : page_info["end"]]
            for opp in page_opps:
                action = render_opportunity_card(
                    opp, interactive=real_db_exists
                )
                if action:
                    _handle_action(action)
                    break  # st.rerun() 后不再渲染

    else:
        # --------------------------------------------------------------
        # 候选清单视图
        # 公司摘要按页展示 → 用户选择一家公司 → 该公司内部机会独立分页
        # --------------------------------------------------------------

        coverage = build_company_coverage(filtered)
        total_companies = len(coverage)

        st.caption(
            f"筛选前总数：{total_before}  |  筛选后总数：{total_after}"
            f"  |  公司数：{total_companies}"
        )

        if total_companies == 0:
            st.info("没有符合筛选条件的公司。")
        else:
            # --- 公司分页 ---
            company_page_size = min(page_size, 50)
            company_page_info = paginate_list(
                total_companies,
                company_page_size,
                st.session_state.get("dash_company_page", 1),
            )
            st.session_state["dash_company_page"] = company_page_info[
                "current_page"
            ]

            # 公司页码选择器
            company_page_options = list(
                range(1, company_page_info["total_pages"] + 1)
            )
            selected_company_page = st.selectbox(
                "公司页码",
                company_page_options,
                index=company_page_info["current_page"] - 1,
                key="dash_company_page_select",
            )
            st.session_state["dash_company_page"] = selected_company_page
            company_page_info = paginate_list(
                total_companies, company_page_size, selected_company_page
            )

            st.caption(
                f"公司分页：第 {company_page_info['current_page']} / "
                f"{company_page_info['total_pages']} 页"
                f"（本页 {company_page_info['end'] - company_page_info['start']} 家公司）"
            )

            page_companies = coverage[
                company_page_info["start"] : company_page_info["end"]
            ]

            # --- 公司摘要列表 ---
            company_names = [c["company_name"] for c in page_companies]
            selected_company_name = st.selectbox(
                "选择公司查看详情",
                company_names,
                index=0,
                key="dash_selected_company",
            )

            # 切换公司时重置该公司内机会分页
            prev_company = st.session_state.get("dash_prev_company")
            if prev_company != selected_company_name:
                st.session_state["dash_company_opp_page"] = 1
                st.session_state["dash_prev_company"] = selected_company_name

            selected = next(
                c
                for c in page_companies
                if c["company_name"] == selected_company_name
            )

            # --- 公司摘要信息 ---
            with st.container(border=True):
                st.subheader(selected["company_name"])
                gap = selected["coverage_gap"]
                if gap > 0:
                    st.warning(f"还需补充 {gap} 个机会")
                if selected["campaign_only"]:
                    st.warning(selected["campaign_only_message"])
                st.caption(
                    f"总机会：{selected['total_count']}  |  "
                    f"缺口：{gap}  |  "
                    f"重点展示：{len(selected['highlighted_top_three'])} 条"
                )

            # --- 公司内机会独立分页 ---
            company_opps = selected["opportunities"]
            opp_page_info = paginate_list(
                len(company_opps),
                page_size,
                st.session_state.get("dash_company_opp_page", 1),
            )
            st.session_state["dash_company_opp_page"] = opp_page_info[
                "current_page"
            ]

            # 公司内机会页码选择器
            opp_page_options = list(
                range(1, opp_page_info["total_pages"] + 1)
            )
            selected_opp_page = st.selectbox(
                "机会页码",
                opp_page_options,
                index=opp_page_info["current_page"] - 1,
                key="dash_company_opp_page_select",
            )
            st.session_state["dash_company_opp_page"] = selected_opp_page
            opp_page_info = paginate_list(
                len(company_opps), page_size, selected_opp_page
            )

            st.caption(
                f"机会分页：第 {opp_page_info['current_page']} / "
                f"{opp_page_info['total_pages']} 页"
                f"（每页 {opp_page_info['page_size']} 条）"
            )

            # 前 3 个"重点展示"，但不截断
            top_ids = {
                opp["id"] for opp in selected["highlighted_top_three"]
            }
            page_opps = company_opps[
                opp_page_info["start"] : opp_page_info["end"]
            ]
            for opp in page_opps:
                action = render_opportunity_card(
                    opp,
                    highlight=opp["id"] in top_ids,
                    interactive=real_db_exists,
                )
                if action:
                    _handle_action(action)
                    break  # st.rerun() 后不再渲染

finally:
    conn.close()
