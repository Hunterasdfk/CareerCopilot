"""通用机会导入器（任务 4）。

职责（docs/TASKS.md 任务 4、docs/ARCHITECTURE.md §2/§4）：
- `parse_workbook(path)`：读取 XLSX 多工作表，按工作表路由到 layout_detector；
  **使用 `pandas.ExcelFile` 先取工作表名，只读取支持的工作表，并按各表列范围
  用 `usecols` 限制加载**（避免伪列与不支持工作表造成内存浪费，修复点 8）；
- `parse_sheet(sheet)`：对单个工作表（pandas DataFrame）逐行解析；
- 支持 CSV 单表输入；
- 产出标准化机会列表 + 布局标签，保留 `source_sheet` / `source_row`
  （**source_row 对应原文件物理行号，含表头：第一条数据行 = 2，空行占用行号
  但不输出记录，修复点 3**），unknown 记录保留完整 `raw_data`；
- **只解析与分类，不向数据库写入，不访问网络**；
- **不做导入报告统计**（new/duplicate/invalid/pending 属任务 5，修复点 6）。

不得命名为 `zhilian_importer`（docs/SOURCE_SCHEMA.md §1）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from services.layout_detector import (
    SUPPORTED_SHEETS,
    UnsupportedSheetError,
    detect_row,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# CSV 解析时默认的工作表名（CSV 单表无工作表名概念，统一标"中国大陆"）
_DEFAULT_CSV_SHEET = "中国大陆"

# 列字母集合：A-Z，用于把 DataFrame 列名（0/1/2...）映射回列字母
_COLUMN_LETTERS = [chr(ord("A") + i) for i in range(26)]

# 各支持工作表的列范围（usecols，修复点 8）
# 取自 docs/SOURCE_SCHEMA.md §1 / docs/WORKBOOK_PROFILE.md §1 的列结构
_SHEET_USECOLS: dict[str, str] = {
    "中国大陆": "A:N",
    "中国香港": "A:J",
    "美国": "A:L",
    "英国": "A:K",
    "新加坡": "A:J",
    "低年级项目-全球版": "A:L",
    "低年级项目-美国&香港-公司官网": "A:H",
}


def _column_letter(index: int) -> str:
    """把 0-based 列索引映射为 Excel 列字母（支持 A-Z，超过 Z 用 AA/AB...）。"""
    if index < 26:
        return _COLUMN_LETTERS[index]
    # 简单的 26+ 列处理（如 26 -> AA）
    first = (index // 26) - 1
    second = index % 26
    return _COLUMN_LETTERS[first] + _COLUMN_LETTERS[second]


# ---------------------------------------------------------------------------
# 行号约定（修复点 3）
# ---------------------------------------------------------------------------

# source_row 对应原文件物理行号（1-based，含表头）：
# - 表头位于第 1 行；
# - 第一条数据行 source_row = 2；
# - 空行可以不输出记录，但必须占用物理行号；
# - 空行后的记录继续使用真实 Excel/CSV 行号，不得重新连续编号。


def _row_to_letter_dict(
    row_series: pd.Series, columns: list[str]
) -> dict[str, Any]:
    """把 pandas 一行（按列位置）转为 {列字母: 取值} 的字典。

    DataFrame 列名可能是真实表头文本或默认整数索引；本函数按**列位置**
    取值，键统一为列字母（A/B/C...），便于 layout_detector 按列字母路由。
    """
    result: dict[str, Any] = {}
    for i, col in enumerate(columns):
        value = row_series[col]
        # pandas NaN 视为缺失
        if pd.isna(value):
            value = None
        else:
            value = str(value)
        result[_column_letter(i)] = value
    return result


def _is_empty_row(row_dict: Mapping) -> bool:
    """判断是否为完全空行（所有列字母取值为 None 或空串）。"""
    return all(
        val is None or (isinstance(val, str) and val.strip() == "")
        for val in row_dict.values()
    )


# ---------------------------------------------------------------------------
# 单工作表解析
# ---------------------------------------------------------------------------


def parse_sheet(
    df: pd.DataFrame, sheet_name: str
) -> list[dict[str, Any]]:
    """解析单个工作表，返回标准化 Opportunity 记录列表。

    Args:
        df: 工作表数据（含表头行已被 pandas 读取为列名）。
        sheet_name: 工作表名（必须是 layout_detector.SUPPORTED_SHEETS 之一）。

    Returns:
        标准化记录列表，每条含 record_type / layout / source_sheet /
        source_row / raw_data 等。空行不输出记录，但**占用物理行号**
        （source_row 对应原文件物理行号，含表头，修复点 3）。

    Raises:
        UnsupportedSheetError: 工作表名不被支持。
    """
    if sheet_name not in SUPPORTED_SHEETS:
        raise UnsupportedSheetError(
            f"不支持的工作表：{sheet_name}（支持：{sorted(SUPPORTED_SHEETS)}）"
        )

    records: list[dict[str, Any]] = []
    columns = list(df.columns)

    # 使用 enumerate(start=2) 按行位置计算物理行号（修复点 5），
    # 不依赖 DataFrame index 一定能转换为 int（如自定义字符串 index）。
    # 表头占第 1 行，第一条数据行 physical_row=2，空行跳过但占用行号。
    for physical_row, (_, row_series) in enumerate(df.iterrows(), start=2):
        row_dict = _row_to_letter_dict(row_series, columns)
        if _is_empty_row(row_dict):
            continue  # 空行不输出记录，但占用行号
        record = detect_row(sheet_name, row_dict, physical_row)
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# 工作簿解析（修复点 8：ExcelFile + usecols 按需加载）
# ---------------------------------------------------------------------------


def parse_workbook(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """解析 XLSX 工作簿，返回 {工作表名: [记录列表]}。

    加载策略（修复点 8）：
    - 先用 `pandas.ExcelFile` 获取工作表名列表（不加载全部数据）；
    - 只读取 `SUPPORTED_SHEETS` 中的工作表；
    - 每张表单独读取并按 `_SHEET_USECOLS` 限制 `usecols`，避免伪列
      （如中国大陆 max_col=94）与不支持工作表造成内存浪费；
    - 不加载不支持的工作表。

    Args:
        path: XLSX 文件路径。

    Returns:
        工作表名→记录列表的字典。仅解析 SUPPORTED_SHEETS 中的工作表；
        不支持的工作表名被跳过（不计入结果）。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件不是 XLSX 或无法读取。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"工作簿不存在：{p}")
    if p.suffix.lower() != ".xlsx":
        raise ValueError(f"仅支持 XLSX 格式，得到：{p.suffix}")

    result: dict[str, list[dict[str, Any]]] = {}

    # 先取工作表名（不加载全部数据），只读取支持的工作表
    with pd.ExcelFile(p, engine="openpyxl") as excel_file:
        all_sheet_names = excel_file.sheet_names
        for sheet_name in all_sheet_names:
            if sheet_name not in SUPPORTED_SHEETS:
                # 不支持的工作表不加载（避免伪列与内存浪费）
                continue
            usecols = _SHEET_USECOLS.get(sheet_name)
            df = pd.read_excel(
                excel_file,
                sheet_name=sheet_name,
                engine="openpyxl",
                usecols=usecols,
            )
            try:
                records = parse_sheet(df, sheet_name)
            except UnsupportedSheetError:
                continue
            result[sheet_name] = records

    return result


def parse_workbook_sheet(
    path: str | Path, sheet_name: str
) -> list[dict[str, Any]]:
    """只解析指定的一张工作表（任务 5：工作表选择）。

    与 parse_workbook 的区别：**不为了一次解析一张表而加载整份工作簿的
    全部受支持工作表**；只按需读取用户选中的这一张（仍按 `_SHEET_USECOLS`
    限制列范围）。

    Args:
        path: XLSX 文件路径。
        sheet_name: 用户选择的工作表名（必须是 SUPPORTED_SHEETS 之一）。

    Returns:
        该工作表的标准化记录列表。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件不是 XLSX。
        UnsupportedSheetError: sheet_name 不被支持，或工作簿中不存在该表。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"工作簿不存在：{p}")
    if p.suffix.lower() != ".xlsx":
        raise ValueError(f"仅支持 XLSX 格式，得到：{p.suffix}")
    if sheet_name not in SUPPORTED_SHEETS:
        raise UnsupportedSheetError(
            f"不支持的工作表：{sheet_name}（支持：{sorted(SUPPORTED_SHEETS)}）"
        )

    with pd.ExcelFile(p, engine="openpyxl") as excel_file:
        if sheet_name not in excel_file.sheet_names:
            raise UnsupportedSheetError(f"工作簿中不存在工作表：{sheet_name}")
        df = pd.read_excel(
            excel_file,
            sheet_name=sheet_name,
            engine="openpyxl",
            usecols=_SHEET_USECOLS.get(sheet_name),
        )
    return parse_sheet(df, sheet_name)


def list_workbook_sheets(path: str | Path) -> list[str]:
    """列出 XLSX 工作簿的全部工作表名（只读元信息，不加载数据）。

    用于测试验证不支持工作表未被实际加载（修复点 8）。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"工作簿不存在：{p}")
    if p.suffix.lower() != ".xlsx":
        raise ValueError(f"仅支持 XLSX 格式，得到：{p.suffix}")
    with pd.ExcelFile(p, engine="openpyxl") as excel_file:
        return list(excel_file.sheet_names)


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------


def parse_csv(path: str | Path, sheet_name: str = _DEFAULT_CSV_SHEET) -> list[dict[str, Any]]:
    """解析 CSV 单表输入，返回标准化记录列表。

    Args:
        path: CSV 文件路径。
        sheet_name: 视作的工作表名（默认"中国大陆"）。CSV 无工作表概念，
            调用方可显式指定其他工作表名以测试不同布局。

    Returns:
        标准化记录列表。

    Raises:
        FileNotFoundError: 文件不存在。
        UnsupportedSheetError: sheet_name 不被支持。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV 不存在：{p}")
    if p.suffix.lower() != ".csv":
        raise ValueError(f"仅支持 CSV 格式，得到：{p.suffix}")

    df = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""])
    return parse_sheet(df, sheet_name)
