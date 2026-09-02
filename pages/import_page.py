"""机会导入页面（任务 5）。

职责边界（docs/ARCHITECTURE.md §1/§2）：
- 本页面**只负责交互**：上传、工作表选择、布局识别结果展示、数据预览、
  unknown 人工确认 UI（分页）、导入前统计与"确认导入"按钮；
- 验证、去重、分类和写库全部由 services/opportunity_service.py 负责，
  页面不堆业务逻辑；
- **只有用户明确点击"确认导入"后才会写库**；页面刷新、切换工作表或
  预览时只调用 classify_records()（数据库零写入）。

预览零持久化（任务 5 修复一）：
- 预览阶段使用 ``get_preview_connection()``：若正式数据库已存在且含
  opportunities 表，返回只读连接；否则返回初始化后的 :memory: 内存库；
- **不得因上传、刷新、选择工作表或预览而创建正式数据库文件**；
- 正式导入阶段才用 ``get_connection()`` + ``init_db()`` 打开可写连接。

上传文件安全（任务 5 边界）：
- 上传内容只保存到**系统临时目录**（tempfile），更换文件或上传清空时
  立即清理，不等进程退出；
- 不保存到真实数据目录、匿名样本目录或仓库任何目录；不访问网络。

unknown 人工确认分页（任务 5 修复四）：
- 真实工作表可能含数千到两万多条 unknown，每页只渲染有限控件
  （默认 20，可选 20/50，上限 50）；
- 确认结果保存在 session_state，键含 file_key + sheet_name + preview_id，
  切换页面/工作表/文件时不丢数据、不串数据；
- suggested_record_type / suggested_fields 仅作建议展示，不得自动确认。
"""

from __future__ import annotations

import atexit
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from database.db_handler import get_connection, get_preview_connection, init_db
from services.dedup_service import preview_identifier
from services.layout_detector import (
    SUPPORTED_SHEETS,
    get_detection_reason_display,
    summarize_unknown_reasons,
)
from services.opportunity_importer import (
    list_workbook_sheets,
    parse_csv,
    parse_workbook_sheet,
)
from services.opportunity_service import (
    CATEGORY_DUPLICATE,
    CATEGORY_INVALID,
    CATEGORY_NEW,
    CATEGORY_PENDING,
    DEFAULT_PAGE_SIZE,
    MAPPABLE_FIELDS,
    PAGE_SIZE_OPTIONS,
    classify_records,
    collect_confirmations,
    count_confirmed,
    import_opportunities,
    paginate_unknown,
)

st.set_page_config(page_title="机会导入", page_icon="📥", layout="wide")
st.title("机会导入")
st.caption(
    "上传 XLSX/CSV 求职机会表 → 选择工作表 → 预览与人工确认 → "
    "点击“确认导入”写库。预览与刷新阶段不会写库。"
)

# ---------------------------------------------------------------------------
# 上传临时文件管理（只进系统临时目录，不进仓库）
# ---------------------------------------------------------------------------

_TEMP_FILES: set[str] = set()


def _cleanup_temp(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
    _TEMP_FILES.discard(path)


def _cleanup_all_temp() -> None:
    for path in list(_TEMP_FILES):
        _cleanup_temp(path)


atexit.register(_cleanup_all_temp)


def _save_upload_to_temp(uploaded) -> Path:
    """把上传内容保存到系统临时目录（绝不写入仓库目录）。"""
    suffix = Path(uploaded.name).suffix.lower()
    fd, tmp_name = tempfile.mkstemp(prefix="careercopilot_upload_", suffix=suffix)
    with os.fdopen(fd, "wb") as handle:
        handle.write(uploaded.getvalue())
    _TEMP_FILES.add(tmp_name)
    return Path(tmp_name)


def _clear_all_confirmation_stores() -> None:
    """清理所有确认状态存储（以 'import_confirmations:' 开头的 session_state 键）。"""
    for key in list(st.session_state):
        if key.startswith("import_confirmations:"):
            st.session_state.pop(key)


def _clear_confirmation_stores_for_file(file_key: str) -> None:
    """清理指定文件的确认状态存储。"""
    prefix = f"import_confirmations:{file_key}:"
    for key in list(st.session_state):
        if key.startswith(prefix):
            st.session_state.pop(key)


def _parse_records(tmp_path: Path, file_key: str, sheet_name: str) -> list[dict]:
    """按需解析用户选中的一张工作表（结果缓存在 session_state）。"""
    cache: dict = st.session_state.setdefault("import_parsed_cache", {})
    cache_key = f"{file_key}:{sheet_name}"
    if cache_key not in cache:
        if tmp_path.suffix.lower() == ".xlsx":
            cache[cache_key] = parse_workbook_sheet(tmp_path, sheet_name)
        else:
            cache[cache_key] = parse_csv(tmp_path, sheet_name=sheet_name)
    return cache[cache_key]


def _col_label(col: str, raw_data: dict) -> str:
    if col == "（不映射）":
        return col
    value = raw_data.get(col)
    text = "" if value is None else str(value)
    return f"{col}：{text[:20]}"


def _render_preview_rows(records: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "来源行": rec.get("source_row"),
            "记录类型": rec.get("record_type"),
            "布局": rec.get("layout"),
            "标题/公司": rec.get("display_title") or rec.get("company_name") or "",
            "待人工确认": "是" if rec.get("needs_confirmation") else "",
            "原因": get_detection_reason_display(rec.get("detection_reason")),
        }
        for rec in records
    ]
    return pd.DataFrame(rows)


def _build_unknown_reason_summary_df(records: list[dict]) -> pd.DataFrame:
    """构建 unknown 原因汇总表（任务 11A.2）。

    只包含原因代码、中文原因、归类与数量，不含公司名称、
    职位描述、链接或 raw_data（summarize_unknown_reasons 保证）。
    """
    rows = [
        {
            "原因代码": item["detection_reason"],
            "原因": item["reason_display"],
            "归类": item["category_display"],
            "数量": item["count"],
        }
        for item in summarize_unknown_reasons(records)
    ]
    return pd.DataFrame(rows, columns=["原因代码", "原因", "归类", "数量"])


# ---------------------------------------------------------------------------
# 1) 上传（保存到系统临时目录）
# ---------------------------------------------------------------------------

uploaded = st.file_uploader("上传求职机会表（XLSX / CSV）", type=["xlsx", "csv"])
if uploaded is None:
    # 上传控件被清空：立即清理旧临时文件、解析缓存和确认状态
    _cleanup_temp(st.session_state.get("import_upload_tmp"))
    st.session_state.pop("import_upload_tmp", None)
    st.session_state.pop("import_upload_key", None)
    st.session_state.pop("import_parsed_cache", None)
    _clear_all_confirmation_stores()
    st.info("请先上传文件（XLSX 多工作表或 CSV 单表）。")
    st.stop()

file_key = hashlib.sha256(uploaded.getvalue()).hexdigest()[:16]
if st.session_state.get("import_upload_key") != file_key:
    # 更换文件：清理旧临时文件、解析缓存和旧文件确认状态
    _cleanup_temp(st.session_state.get("import_upload_tmp"))
    old_key = st.session_state.get("import_upload_key")
    if old_key:
        _clear_confirmation_stores_for_file(old_key)
    saved_path = _save_upload_to_temp(uploaded)
    st.session_state["import_upload_tmp"] = str(saved_path)
    st.session_state["import_upload_key"] = file_key
    st.session_state["import_parsed_cache"] = {}
tmp_path = Path(st.session_state["import_upload_tmp"])

# ---------------------------------------------------------------------------
# 2) 工作表选择（XLSX 只解析选中的一张；CSV 单表）
# ---------------------------------------------------------------------------

st.subheader("选择工作表")
suffix = tmp_path.suffix.lower()
if suffix == ".xlsx":
    sheet_names = list_workbook_sheets(tmp_path)
    supported = [s for s in sheet_names if s in SUPPORTED_SHEETS]
    unsupported = [s for s in sheet_names if s not in SUPPORTED_SHEETS]
    if unsupported:
        st.caption(f"不受支持、将被跳过的工作表：{'、'.join(unsupported)}")
    if not supported:
        st.warning("工作簿中没有受支持的工作表。")
        st.stop()
    sheet_name: str = st.selectbox(
        "工作表（只解析选中的一张，避免整簿加载）", supported
    )
elif suffix == ".csv":
    sheet_name = "中国大陆"
    st.caption("CSV 为单表输入，按“中国大陆”布局解析。")
else:
    st.error("仅支持 XLSX / CSV 文件。")
    st.stop()

# 切换工作表时重置分页（不同工作表使用不同确认存储，天然隔离）
if st.session_state.get("import_selected_sheet") != sheet_name:
    st.session_state["import_selected_sheet"] = sheet_name
    st.session_state["import_unknown_page"] = 1

records: list[dict[str, Any]] = _parse_records(tmp_path, file_key, sheet_name)
if not records:
    st.warning("所选工作表没有可解析的非空记录。")
    st.stop()

# ---------------------------------------------------------------------------
# 3) 布局识别结果 + 数据预览（只读，不写库）
# ---------------------------------------------------------------------------

st.subheader("布局识别结果")
summary: dict[tuple[str, str], int] = {}
for rec in records:
    key = (str(rec.get("record_type")), str(rec.get("layout")))
    summary[key] = summary.get(key, 0) + 1
summary_df = pd.DataFrame(
    [
        {"record_type": record_type, "layout": layout, "行数": count}
        for (record_type, layout), count in sorted(summary.items())
    ]
)
st.dataframe(summary_df)

st.subheader("数据预览")
st.caption(f"共 {len(records)} 条非空记录（空行已占用物理行号但不输出）。")
st.dataframe(_render_preview_rows(records))

# ---------------------------------------------------------------------------
# 4) unknown 人工确认（分页，建议仅供展示，不得自动确认）
# ---------------------------------------------------------------------------

st.subheader("待人工确认（unknown 记录）")
pending_records = [r for r in records if r.get("record_type") == "unknown"]

# 确认存储：键含 file_key + sheet_name，切换文件/工作表时天然隔离
store_key = f"import_confirmations:{file_key}:{sheet_name}"
store: dict[str, dict[str, Any]] = st.session_state.setdefault(store_key, {})

if not pending_records:
    st.info("没有需要人工确认的 unknown 记录。")
else:
    confirmed, unconfirmed = count_confirmed(store, pending_records)
    st.caption(
        f"共 {len(pending_records)} 条 unknown；已选择类型 {confirmed}；"
        f"未选择类型 {unconfirmed}。"
    )

    # 任务 11A.2：unknown 原因汇总（只显示原因与数量，不含公司/描述/链接）
    with st.expander("查看待确认原因汇总"):
        st.dataframe(
            _build_unknown_reason_summary_df(records),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "汇总仅统计原因与数量：规则疑似漏识别可反馈补充规则；"
            "缺少必要字段或表头冲突需检查源数据；其余记录需人工判断。"
        )

    # 页大小选择（默认 20，可选 20/50，上限 50）
    page_size = st.selectbox(
        "每页显示条数",
        list(PAGE_SIZE_OPTIONS),
        index=0,
        key="import_page_size_select",
    )
    st.session_state["import_page_size"] = page_size

    total = len(pending_records)
    current_page = st.session_state.get("import_unknown_page", 1)
    page_info = paginate_unknown(total, page_size, current_page)

    # 页码选择（selectbox 自带状态，切换时自动重渲染）
    page_options = list(range(1, page_info["total_pages"] + 1))
    selected_page = st.selectbox(
        f"页码（共 {page_info['total_pages']} 页）",
        page_options,
        index=page_info["current_page"] - 1,
        key="import_page_select",
    )
    st.session_state["import_unknown_page"] = selected_page
    # 重新计算以使用实际选中页
    page_info = paginate_unknown(total, page_size, selected_page)

    st.caption(
        f"当前第 {page_info['current_page']} / {page_info['total_pages']} 页"
        f"（每页 {page_info['page_size']} 条，本页 "
        f"{page_info['end'] - page_info['start']} 条）"
    )

    st.caption(
        "系统建议（suggested_record_type / suggested_fields）仅供参考，"
        "不会自动确认。你必须主动选择记录类型并完成字段映射；"
        "未确认或映射不完整的记录保持待确认，不会入库。"
    )

    # 只渲染当前页的 unknown 记录
    page_records = pending_records[page_info["start"] : page_info["end"]]
    for rec in page_records:
        pid = preview_identifier(rec)
        suggestion = rec.get("suggested_record_type")
        with st.expander(
            f"{rec.get('source_sheet')} 第 {rec.get('source_row')} 行"
            f"（布局：{rec.get('layout')}）"
        ):
            reason = get_detection_reason_display(rec.get("detection_reason"))
            if reason:
                st.write(f"识别原因：{reason}")
            suggestion_text = (
                str(suggestion) if suggestion else "无（不提供最终建议）"
            )
            st.write(f"系统建议类型：{suggestion_text}")
            if rec.get("suggested_fields"):
                suggested_text = "、".join(
                    f"{field}={value}"
                    for field, value in rec["suggested_fields"].items()
                )
                st.write(f"建议字段（仅供映射参考）：{suggested_text}")

            choice = st.radio(
                "确认记录类型",
                ["待确认（暂不导入）", "campaign", "job"],
                index=0,  # 默认不确认；不得因存在建议而自动确认
                key=f"rt:{file_key}:{pid}",
                horizontal=True,
            )
            if choice not in ("campaign", "job"):
                store.pop(pid, None)  # 取消确认 → 从存储移除
                continue

            raw_data: dict = rec.get("raw_data") or {}
            col_options = ["（不映射）"] + [c for c in raw_data]
            mapping: dict[str, str] = {}
            st.markdown("字段映射（标准字段 ← 原始列）：")
            mapping_cols = st.columns(3)
            for i, field in enumerate(sorted(MAPPABLE_FIELDS)):
                selected = mapping_cols[i % 3].selectbox(
                    field,
                    col_options,
                    key=f"map:{file_key}:{pid}:{field}",
                    format_func=lambda c, rd=raw_data: _col_label(c, rd),
                )
                if selected != "（不映射）":
                    mapping[field] = selected
            # 保存确认到 session_state 存储（切换页面后不丢失）
            store[pid] = {
                "record_type": choice,
                "field_mapping": mapping,
            }

# ---------------------------------------------------------------------------
# 5) 导入前统计（classify_records 零写入，预览只读连接）
# ---------------------------------------------------------------------------

# 收集全部确认（当前页以外也保留在 store 中）
confirmations = collect_confirmations(store, pending_records)

# 预览阶段：只读连接或内存库，不创建持久数据库
preview_conn = get_preview_connection()

try:
    st.subheader("导入前统计")
    classification = classify_records(records, preview_conn, confirmations)
    counts = classification["counts"]
    metric_new, metric_dup, metric_inv, metric_pen = st.columns(4)
    metric_new.metric("新增（可导入）", counts[CATEGORY_NEW])
    metric_dup.metric("重复", counts[CATEGORY_DUPLICATE])
    metric_inv.metric("无效", counts[CATEGORY_INVALID])
    metric_pen.metric("待人工确认", counts[CATEGORY_PENDING])
    st.caption(
        f"四类合计 {sum(counts.values())} 条 = 本次参与预览的非空记录总数 "
        f"{classification['total']} 条（互斥且闭合，针对整个工作表）。"
    )

    with st.expander("查看分类明细"):
        tab_new, tab_dup, tab_inv, tab_pen = st.tabs(
            ["新增", "重复", "无效", "待人工确认"]
        )
        for tab, category in (
            (tab_new, CATEGORY_NEW),
            (tab_dup, CATEGORY_DUPLICATE),
            (tab_inv, CATEGORY_INVALID),
            (tab_pen, CATEGORY_PENDING),
        ):
            with tab:
                for item in classification["items"][category]:
                    rec = item["record"]
                    st.write(
                        f"- 第 {rec.get('source_row')} 行：{item['reason']}"
                    )

    # -----------------------------------------------------------------------
    # 6) 确认导入（唯一写库入口）
    # -----------------------------------------------------------------------

    st.subheader("确认导入")
    st.caption(
        "只有点击下面的按钮才会写库；刷新、切换工作表或预览都不会写库。"
        "重复、无效与待人工确认记录一律不入库。"
    )
    if st.button("确认导入", type="primary"):
        # 正式导入阶段：打开独立可写连接 + init_db（此时才允许创建/修改数据库）
        write_conn = get_connection()
        try:
            init_db(write_conn)
            report = import_opportunities(records, write_conn, confirmations)
        except Exception as exc:  # 事务已回滚（服务层负责），这里只展示
            st.error(f"导入失败，事务已回滚，未写入任何数据：{exc}")
        else:
            st.success(
                f"导入完成：实际新增 {report['inserted']} 条；"
                f"重复 {report['counts'][CATEGORY_DUPLICATE]} 条；"
                f"无效 {report['counts'][CATEGORY_INVALID]} 条；"
                f"待人工确认 {report['counts'][CATEGORY_PENDING]} 条。"
            )
            st.caption(
                f"导入批次 ID：{report['batch_id']}"
                f"（实际分类合计 {sum(report['counts'].values())}"
                f"/{report['total']} 条）"
            )
            with st.expander("导入报告明细（实际写入结果）"):
                tab_new2, tab_dup2, tab_inv2, tab_pen2 = st.tabs(
                    ["新增", "重复", "无效", "待人工确认"]
                )
                for tab, category in (
                    (tab_new2, CATEGORY_NEW),
                    (tab_dup2, CATEGORY_DUPLICATE),
                    (tab_inv2, CATEGORY_INVALID),
                    (tab_pen2, CATEGORY_PENDING),
                ):
                    with tab:
                        for item in report["items"][category]:
                            rec = item["record"]
                            st.write(
                                f"- 第 {rec.get('source_row')} 行："
                                f"{item['reason']}"
                            )
        finally:
            write_conn.close()
finally:
    preview_conn.close()
