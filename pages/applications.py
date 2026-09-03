"""申请记录页面（任务 12B）。

申请记录属于登录用户，而不是全局机会目录。每条记录支持：公司、岗位、
统一状态、自定义流程步骤、下一步行动、备注和追加式时间线。流程步骤使用
自由文本，因此不同公司的“网申/笔试/技术一面/HR 面”等流程无需强行套用
同一枚举。页面没有自动检测招聘网站流程，也不会自动提交申请。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

import streamlit as st

from components.auth_ui import render_auth_controls
from services.supabase_service import (
    EVENT_TYPES,
    PRIORITY_VALUES,
    STATUS_VALUES,
    SupabaseDataError,
    SupabaseDataService,
)


STATUS_LABELS = {
    "discovered": "已保存",
    "shortlisted": "候选",
    "opened": "已打开链接",
    "applying": "填写中",
    "applied": "已投递",
    "assessment": "笔试/测评",
    "interview": "面试",
    "offer": "Offer",
    "rejected": "拒绝",
    "withdrawn": "已放弃",
}


def build_application_summary(applications: list[Mapping[str, Any]]) -> dict[str, Any]:
    """生成页面摘要，只包含公司/岗位/状态/流程步骤数量等非敏感字段。"""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for application in applications:
        company = str(application.get("company_name") or "未填写公司").strip()
        grouped[company].append(application)
    return {
        "company_count": len(grouped),
        "application_count": len(applications),
        "companies": {
            company: len(items) for company, items in sorted(grouped.items())
        },
    }


def _status_label(value: object) -> str:
    text = str(value or "")
    return f"{STATUS_LABELS.get(text, text)}（{text}）" if text else "未设置"


def _render_timeline(service: SupabaseDataService, application_id: str) -> None:
    try:
        events = service.list_application_events(application_id)
    except SupabaseDataError as exc:
        st.warning(f"时间线读取失败：{exc}")
        return
    if not events:
        st.caption("暂无时间线事件。")
        return
    for event in events:
        when = event.get("occurred_at") or event.get("created_at") or ""
        event_type = event.get("event_type") or "other"
        transition = ""
        if event.get("from_stage") or event.get("to_stage"):
            transition = (
                f"：{event.get('from_stage') or '—'} → "
                f"{event.get('to_stage') or '—'}"
            )
        note = f"；备注：{event['note']}" if event.get("note") else ""
        st.write(f"{when} · {event_type}{transition}{note}")


def main() -> None:
    st.set_page_config(page_title="申请记录", page_icon="🗂️", layout="wide")
    st.title("申请记录")
    st.caption(
        "按公司查看已申请岗位，手动维护每个岗位的实际流程步骤；"
        "不同公司的流程不需要统一，系统不会自动打开链接检测或提交申请。"
    )
    context = render_auth_controls()
    if context is None:
        st.info("请在 Streamlit Secrets 配置 Supabase 后使用申请记录。")
        return
    if context.user is None:
        st.info("请先在左侧登录 Supabase 账户。")
        return

    service = SupabaseDataService(context.client)
    try:
        applications = service.list_applications()
    except SupabaseDataError as exc:
        st.error(f"申请记录读取失败：{exc}")
        return

    summary = build_application_summary(applications)
    metric1, metric2 = st.columns(2)
    metric1.metric("公司数", summary["company_count"])
    metric2.metric("申请岗位数", summary["application_count"])

    with st.expander("新增申请记录", expanded=not applications):
        with st.form("create_application_form", clear_on_submit=True):
            company = st.text_input("公司名称 *")
            title = st.text_input("岗位名称 *")
            url = st.text_input("申请链接（可选）")
            stage = st.text_input("当前流程步骤", value="saved")
            priority = st.selectbox("优先级", sorted(PRIORITY_VALUES), index=1)
            next_action = st.text_input("下一步行动（可选）")
            next_action_at = st.text_input("下一步时间（可选，ISO 日期/时间）")
            notes = st.text_area("备注（可选）")
            submitted = st.form_submit_button("保存申请记录", type="primary")
        if submitted:
            try:
                service.create_application(
                    company_name=company,
                    job_title=title,
                    application_url=url or None,
                    current_stage=stage,
                    priority=priority,
                    next_action=next_action or None,
                    next_action_at=next_action_at or None,
                    notes=notes or None,
                )
            except SupabaseDataError as exc:
                st.error(f"保存失败：{exc}")
            else:
                st.success("申请记录已保存到云端。")
                st.rerun()

    if not applications:
        st.info("还没有申请记录。可以从上方手动新增，或在机会看板点击操作按钮自动建立。")
        return

    search = st.text_input("搜索公司或岗位", key="application_search")
    search_text = search.strip().casefold()
    visible = [
        app
        for app in applications
        if not search_text
        or search_text in str(app.get("company_name") or "").casefold()
        or search_text in str(app.get("job_title") or "").casefold()
    ]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for app in visible:
        grouped[str(app.get("company_name") or "未填写公司")].append(app)

    for company, company_apps in sorted(grouped.items()):
        st.subheader(f"{company}（{len(company_apps)} 个岗位）")
        for app in company_apps:
            app_id = str(app.get("id") or "")
            if not app_id:
                continue
            title = str(app.get("job_title") or "未填写岗位")
            with st.expander(f"{title} · {_status_label(app.get('status'))}"):
                st.caption(
                    f"当前流程步骤：{app.get('current_stage') or 'saved'}  | "
                    f"优先级：{app.get('priority') or 'medium'}"
                )
                if app.get("application_url"):
                    st.link_button("打开申请链接", app["application_url"])
                with st.form(f"update_application_{app_id}"):
                    status = st.selectbox(
                        "统一状态",
                        sorted(STATUS_VALUES),
                        index=(
                            sorted(STATUS_VALUES).index(app.get("status"))
                            if app.get("status") in STATUS_VALUES
                            else 0
                        ),
                    )
                    current_stage = st.text_input(
                        "当前流程步骤（可自定义）",
                        value=str(app.get("current_stage") or "saved"),
                    )
                    priority = st.selectbox(
                        "优先级",
                        sorted(PRIORITY_VALUES),
                        index=(
                            sorted(PRIORITY_VALUES).index(app.get("priority"))
                            if app.get("priority") in PRIORITY_VALUES
                            else 1
                        ),
                    )
                    next_action = st.text_input(
                        "下一步行动", value=str(app.get("next_action") or "")
                    )
                    next_action_at = st.text_input(
                        "下一步时间", value=str(app.get("next_action_at") or "")
                    )
                    notes = st.text_area("备注", value=str(app.get("notes") or ""))
                    save = st.form_submit_button("保存修改")
                if save:
                    try:
                        changed = service.update_application(
                            app_id,
                            status=status,
                            current_stage=current_stage,
                            priority=priority,
                            next_action=next_action or None,
                            next_action_at=next_action_at or None,
                            notes=notes or None,
                        )
                        if current_stage != str(app.get("current_stage") or "saved"):
                            service.append_application_event(
                                app_id,
                                event_type="stage_changed",
                                from_stage=str(app.get("current_stage") or "saved"),
                                to_stage=current_stage,
                            )
                    except SupabaseDataError as exc:
                        st.error(f"修改失败：{exc}")
                    else:
                        st.success("申请记录已更新。")
                        st.rerun()
                with st.expander("查看申请时间线"):
                    _render_timeline(service, app_id)
                    with st.form(f"event_form_{app_id}", clear_on_submit=True):
                        event_type = st.selectbox(
                            "事件类型", sorted(EVENT_TYPES), key=f"event_type_{app_id}"
                        )
                        event_note = st.text_area(
                            "事件备注", key=f"event_note_{app_id}"
                        )
                        add_event = st.form_submit_button("追加时间线事件")
                    if add_event:
                        try:
                            service.append_application_event(
                                app_id, event_type=event_type, note=event_note or None
                            )
                        except SupabaseDataError as exc:
                            st.error(f"追加失败：{exc}")
                        else:
                            st.success("时间线已追加。")
                            st.rerun()
                if st.button("删除此申请记录", key=f"delete_application_{app_id}"):
                    try:
                        service.delete_application(app_id)
                    except SupabaseDataError as exc:
                        st.error(f"删除失败：{exc}")
                    else:
                        st.success("申请记录已删除。")
                        st.rerun()


main()
