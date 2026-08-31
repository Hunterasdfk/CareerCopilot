"""机会卡片组件（任务 7 + 任务 8）。

职责：把单条机会（普通 dict）渲染为 Streamlit 原生安全组件。
- campaign 与 job 有清晰且不同的视觉标识；
- priority / status 使用文字标签；
- 不使用不安全的未转义 HTML（Streamlit 原生组件自动转义）；
- 任务 8：增加"打开投递链接"和"确认已投递"两个独立按钮；
  - 点击"打开投递链接"后调用 mark_as_opened，服务返回有效 URL
    后才通过本地默认浏览器打开；无链接时显示提示，不打开浏览器、
    不更新状态；
  - "确认已投递"是独立操作，不能在打开链接时自动触发；
  - 服务层不直接打开浏览器，UI 层负责调用 webbrowser。
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st


def _record_type_badge(record_type: str) -> str:
    """返回 record_type 的视觉标签文本。"""
    if record_type == "job":
        return "具体岗位 / JOB"
    if record_type == "campaign":
        return "招聘项目入口 / CAMPAIGN"
    return str(record_type or "未知")


def render_opportunity_card(
    opp: Mapping, *, highlight: bool = False, interactive: bool = False
) -> dict[str, Any] | None:
    """渲染单条机会卡片（Streamlit 原生组件，安全转义）。

    Args:
        opp: 机会 dict（来自 fetch_all_opportunities / build_company_coverage）。
        highlight: 是否为 highlighted_top_three（显示"重点展示"标识）。
        interactive: 是否渲染任务 8 的操作按钮（打开投递链接 / 确认已投递）。
            仅在真实数据库存在时由 dashboard 传入 True。

    Returns:
        当 ``interactive=True`` 且按钮被点击时，返回操作字典::

            {"type": "open_link" | "confirm_applied", "opp_id": int}

        否则返回 None。
    """
    record_type = str(opp.get("record_type") or "")
    title = str(opp.get("display_title") or "(无标题)")
    company = str(opp.get("company_name") or "(无公司)")
    priority = str(opp.get("priority") or "low")
    status = str(opp.get("status") or "discovered")
    location = str(opp.get("location") or "").strip() or "未填写"
    deadline = str(opp.get("deadline") or "未填写")

    # 链接存在情况
    has_app = bool(str(opp.get("application_url") or "").strip())
    has_ann = bool(str(opp.get("announcement_url") or "").strip())
    if has_app and has_ann:
        link_info = "投递链接 + 公告链接"
    elif has_app:
        link_info = "仅投递链接"
    elif has_ann:
        link_info = "仅公告链接"
    else:
        link_info = "无链接"

    badge = _record_type_badge(record_type)
    highlight_tag = "  ⭐ 重点展示" if highlight else ""

    with st.container(border=True):
        st.markdown(f"**{title}**{highlight_tag}")
        st.caption(
            f"{badge}  |  公司：{company}  |  优先级：{priority}"
            f"  |  状态：{status}"
        )
        st.caption(
            f"地区：{location}  |  截止：{deadline}  |  链接：{link_info}"
        )
        with st.expander("来源详情"):
            st.write(f"source_sheet：{opp.get('source_sheet', '未填写')}")
            st.write(f"source_row：{opp.get('source_row', '未填写')}")
            if opp.get("job_title"):
                st.write(f"岗位名称：{opp['job_title']}")
            if opp.get("job_categories"):
                st.write(f"岗位类别：{opp['job_categories']}")
            if opp.get("industry"):
                st.write(f"行业：{opp['industry']}")
            if opp.get("recruitment_type"):
                st.write(f"招聘类型：{opp['recruitment_type']}")
            if opp.get("target_cohort"):
                st.write(f"届次：{opp['target_cohort']}")
            if opp.get("education_requirement"):
                st.write(f"学历要求：{opp['education_requirement']}")

        # 任务 8：操作按钮（仅在 interactive=True 时渲染）
        if interactive:
            opp_id = opp.get("id")
            if opp_id is not None:
                col1, col2 = st.columns(2)
                action: dict[str, Any] | None = None
                if col1.button(
                    "打开投递链接", key=f"open_link_{opp_id}"
                ):
                    action = {"type": "open_link", "opp_id": int(opp_id)}
                if col2.button(
                    "确认已投递", key=f"confirm_applied_{opp_id}"
                ):
                    action = {
                        "type": "confirm_applied",
                        "opp_id": int(opp_id),
                    }
                return action
    return None
