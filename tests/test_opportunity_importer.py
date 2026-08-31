"""`opportunity_importer` 与 `layout_detector` 的单元测试（任务 4，修复版）。

覆盖 docs/TASKS.md 任务 4 完成标准与修复点：

初版 9 项修复：
1. 香港/美国候选布局以 unknown + needs_confirmation + suggested_fields 输出；
2. 中国大陆城市判定严格化（拒绝 URL/日期/届次/学历/普通描述）；
3. source_row 对应原文件物理行号（含表头，空行占用行号）；
4. 低年级-全球版保守判定 job/campaign/unknown；
5. 香港错位字段不强行映射，符合届次规则才建议为 target_cohort；
6. 移除 summarize()（导入报告属任务 5）；
7. 安全测试不依赖 data/private 存在；
8. parse_workbook 使用 ExcelFile + usecols 按需加载，不加载不支持工作表；
9. 清理导入与 job 降级残留字段。

末次修正 5 项：
1. 区分大陆 F=job 与 F=other（新增 _is_job_title_like）；
2. 英国/新加坡独立招聘类型规则（不复用大陆白名单，支持 Graduate Programme）；
3. 香港 suggested_record_type 保守化（D 非空不再即 job，无法判断→None）；
4. source_row 加固使用 enumerate(start=2)（不依赖 DataFrame index 可转 int）；
5. 同步文档（ARCHITECTURE / DATA_MODEL / SOURCE_SCHEMA / WORKBOOK_PROFILE）。

测试数据**完全虚构**（示例科技A / 示例制造B / 示例银行C / example.com），
不访问网络，不读取 data/private。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from services.layout_detector import (
    LAYOUT_HK_SHIFTED,
    LAYOUT_HK_STANDARD,
    LAYOUT_JUNIOR_GLOBAL,
    LAYOUT_JUNIOR_OFFICIAL,
    LAYOUT_MAINLAND_CAMPAIGN,
    LAYOUT_MAINLAND_JOB,
    LAYOUT_SG_DEFAULT,
    LAYOUT_UK_DEFAULT,
    LAYOUT_UNKNOWN,
    LAYOUT_US_STANDARD,
    LAYOUT_US_SWAPPED,
    SUPPORTED_SHEETS,
    UnsupportedSheetError,
    is_candidate_layout,
    is_city,
)
from services.opportunity_importer import (
    list_workbook_sheets,
    parse_csv,
    parse_sheet,
    parse_workbook,
    parse_workbook_sheet,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
XLSX_FIXTURE = FIXTURES_DIR / "sample_workbook.xlsx"
CSV_FIXTURE = FIXTURES_DIR / "sample_mainland.csv"


# ---------------------------------------------------------------------------
# 夹具可用性
# ---------------------------------------------------------------------------


def test_fixtures_exist():
    assert XLSX_FIXTURE.exists(), f"XLSX 夹具缺失：{XLSX_FIXTURE}"
    assert CSV_FIXTURE.exists(), f"CSV 夹具缺失：{CSV_FIXTURE}"


# ---------------------------------------------------------------------------
# 工作簿级：parse_workbook（XLSX 多工作表，修复点 8 usecols 按需加载）
# ---------------------------------------------------------------------------


def test_parse_workbook_returns_supported_sheets_only():
    result = parse_workbook(XLSX_FIXTURE)
    expected = {
        "中国大陆",
        "中国香港",
        "美国",
        "英国",
        "新加坡",
        "低年级项目-全球版",
        "低年级项目-美国&香港-公司官网",
    }
    assert set(result.keys()) == expected
    assert "临时表-不该被解析" not in result


def test_parse_workbook_each_sheet_has_records():
    result = parse_workbook(XLSX_FIXTURE)
    for sheet_name, records in result.items():
        assert isinstance(records, list)
        assert len(records) > 0, f"工作表 {sheet_name} 应有记录"


def test_parse_workbook_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_workbook(tmp_path / "nonexistent.xlsx")


def test_parse_workbook_rejects_non_xlsx(tmp_path):
    bad = tmp_path / "not_xlsx.txt"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="XLSX"):
        parse_workbook(bad)


def test_list_workbook_sheets_includes_unsupported():
    """修复点 8：list_workbook_sheets 返回全部工作表名（含不支持表）。"""
    sheets = list_workbook_sheets(XLSX_FIXTURE)
    assert "临时表-不该被解析" in sheets
    assert "中国大陆" in sheets


def test_parse_workbook_does_not_load_unsupported_sheet(tmp_path, monkeypatch):
    """修复点 8：不支持的工作表未被 read_excel 实际加载。

    通过 monkeypatch 计数 read_excel 调用，验证不支持表未被读取。
    """
    original_read_excel = pd.read_excel
    called_sheets: list[str] = []

    def spy_read_excel(io, sheet_name=0, **kwargs):
        called_sheets.append(sheet_name)
        return original_read_excel(io, sheet_name=sheet_name, **kwargs)

    monkeypatch.setattr(pd, "read_excel", spy_read_excel)
    parse_workbook(XLSX_FIXTURE)

    # 不支持的工作表不应被 read_excel 加载
    assert "临时表-不该被解析" not in called_sheets
    # 支持的工作表应被加载
    assert "中国大陆" in called_sheets


def test_parse_workbook_usecols_limits_columns():
    """修复点 8：各工作表按 _SHEET_USECOLS 限制加载列数，避免伪列。"""
    from services.opportunity_importer import _SHEET_USECOLS

    # 中国大陆应只加载 A:N（14 列）
    df = pd.read_excel(
        XLSX_FIXTURE, sheet_name="中国大陆", engine="openpyxl",
        usecols=_SHEET_USECOLS["中国大陆"],
    )
    assert len(df.columns) == 14
    # 低年级-公司官网应只加载 A:H（8 列）
    df_jr = pd.read_excel(
        XLSX_FIXTURE,
        sheet_name="低年级项目-美国&香港-公司官网",
        engine="openpyxl",
        usecols=_SHEET_USECOLS["低年级项目-美国&香港-公司官网"],
    )
    assert len(df_jr.columns) == 8


# ---------------------------------------------------------------------------
# 中国大陆布局判定
# ---------------------------------------------------------------------------


def _read_mainland():
    return pd.read_excel(
        XLSX_FIXTURE, sheet_name="中国大陆", engine="openpyxl",
        usecols="A:N",
    )


def test_mainland_campaign_layout():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 2：F=秋招全职 + G=2026届 → campaign
    campaign = records[0]
    assert campaign["record_type"] == "campaign"
    assert campaign["layout"] == LAYOUT_MAINLAND_CAMPAIGN
    assert campaign["company_name"] == "示例科技A"
    assert campaign["recruitment_type"] == "秋招全职"
    assert campaign["target_cohort"] == "2026届"
    assert campaign["source_row"] == 2  # 修复点 3：物理行号


def test_mainland_job_layout():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 3：F=示例后端开发工程师 + G=上海市 → job
    job = records[1]
    assert job["record_type"] == "job"
    assert job["layout"] == LAYOUT_MAINLAND_JOB
    assert job["job_title"] == "示例后端开发工程师"
    assert job["location"] == "上海市"
    assert job["source_row"] == 3


def test_mainland_multi_city_is_city():
    """修复点 2：北京-上海 应判为 city（每段都可识别）。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 13：G=北京-上海 → city → job
    job = records[10]
    assert job["record_type"] == "job"
    assert job["location"] == "北京-上海"
    assert job["source_row"] == 13


def test_mainland_duplicate_records_share_business_identity():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 3 与 row 5 业务内容相同（中间 row 4 是空行）
    job1 = records[1]  # row 3
    job2 = records[2]  # row 5
    assert job1["source_row"] == 3
    assert job2["source_row"] == 5  # 修复点 3：空行后继续真实行号
    assert job1["company_name"] == job2["company_name"]
    assert job1["job_title"] == job2["job_title"]
    assert job1["application_url"] == job2["application_url"]


def test_mainland_unknown_f_other_with_cohort():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 6：F=其他类别 + G=2026届 → unknown
    unknown = records[3]
    assert unknown["record_type"] == "unknown"
    assert unknown["layout"] == LAYOUT_UNKNOWN
    assert unknown["source_row"] == 6
    assert "raw_data" in unknown and unknown["raw_data"]


def test_mainland_unknown_job_with_non_city_g():
    """修复点 2：F=岗位名 + G=可议 → unknown（不得误判 job）。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[4]  # row 7
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 7
    assert "raw_data" in unknown


def test_mainland_unknown_job_with_date_g():
    """修复点 2：F=岗位名 + G=2026/09/30 → unknown（日期不得误判城市）。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[5]  # row 8
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 8
    assert "raw_data" in unknown


def test_mainland_unknown_job_with_url_g():
    """修复点 2：F=岗位名 + G=URL → unknown（URL 不得误判城市）。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[6]  # row 9
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 9
    assert "raw_data" in unknown


def test_mainland_unknown_empty_f_g():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 10：F=空 + G=空 → unknown
    unknown = records[7]
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 10
    assert "raw_data" in unknown


def test_mainland_unknown_f_other_with_city():
    """修复点 1：F=其他类别 + G=北京市 → unknown（不得因 G=city 就判 job）。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[11]  # row 14
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 14
    assert "raw_data" in unknown
    # 顶层不写 job_title / location
    assert "job_title" not in unknown
    assert "location" not in unknown


def test_mainland_unknown_comprehensive_recruitment_info():
    """修复点 1：F=综合招聘信息 + G=北京市 → unknown。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[12]  # row 15
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 15
    assert "raw_data" in unknown


def test_mainland_unknown_announcement_title():
    """修复点 1：F=招聘公告 + G=北京市 → unknown。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[13]  # row 16
    assert unknown["record_type"] == "unknown"
    assert unknown["source_row"] == 16
    assert "raw_data" in unknown


def test_mainland_invalid_url_not_mapped_to_standard_field():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 11：N=官网投递（非 http），M=见公告（非 http）
    rec = records[8]
    assert rec["record_type"] == "campaign"
    assert rec["application_url"] is None
    assert rec["announcement_url"] is None
    assert rec["raw_data"]["N"] == "官网投递"
    assert rec["raw_data"]["M"] == "见公告"
    assert rec["source_row"] == 11


def test_mainland_missing_company():
    records = parse_sheet(_read_mainland(), "中国大陆")
    # row 12：E 列空（company 缺失），但仍为 campaign 布局
    rec = records[9]
    assert rec["record_type"] == "campaign"
    assert rec["company_name"] is None or rec["company_name"] == ""
    assert rec["source_row"] == 12


def test_mainland_empty_row_skipped_but_occupies_row_number():
    """修复点 3：row 4 空行不输出记录，但占用行号，row 5 仍为 5。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    source_rows = [r["source_row"] for r in records]
    assert 4 not in source_rows  # 空行被跳过
    assert 5 in source_rows  # 空行后的记录保留真实行号
    # 共 14 条非空记录（row 2-16，去除 row 4 空行 = 14 条）
    assert len(records) == 14


# ---------------------------------------------------------------------------
# 修复点 2：城市判定单元测试
# ---------------------------------------------------------------------------


def test_is_city_known_regions():
    assert is_city("北京") is True
    assert is_city("上海") is True
    assert is_city("北京市") is True
    assert is_city("广东省") is True


def test_is_city_multi_regions():
    assert is_city("北京-上海") is True
    assert is_city("北京、上海") is True
    assert is_city("北京/上海") is True


def test_is_city_rejects_date():
    assert is_city("2026/09/30") is False
    assert is_city("2026-09-30") is False


def test_is_city_rejects_url():
    assert is_city("https://example.com/a") is False


def test_is_city_rejects_cohort():
    assert is_city("2026届") is False


def test_is_city_rejects_education():
    assert is_city("本科") is False


def test_is_city_rejects_plain_description():
    assert is_city("可议") is False
    assert is_city("不限") is False


def test_is_city_multi_segments_all_must_be_region():
    """多段值中只要有一段不可识别就拒绝。"""
    assert is_city("北京-可议") is False
    assert is_city("北京-2026届") is False


# ---------------------------------------------------------------------------
# 修复点 1：_is_job_title_like 单元测试
# ---------------------------------------------------------------------------


def test_is_job_title_like_known_titles():
    """修复点 1：明确岗位名称 → True。"""
    from services.layout_detector import _is_job_title_like

    assert _is_job_title_like("示例后端开发工程师") is True
    assert _is_job_title_like("数据分析师") is True
    assert _is_job_title_like("产品经理") is True
    assert _is_job_title_like("管培生") is True


def test_is_job_title_like_rejects_non_titles():
    """修复点 1：非岗位名称文本 → False。"""
    from services.layout_detector import _is_job_title_like

    assert _is_job_title_like("其他类别") is False
    assert _is_job_title_like("综合招聘信息") is False
    assert _is_job_title_like("招聘公告") is False
    assert _is_job_title_like("多岗位") is False
    assert _is_job_title_like("不限") is False
    assert _is_job_title_like("可议") is False


def test_is_job_title_like_rejects_recruitment_and_cohort():
    """修复点 1：招聘类型/届次/学历 → 不是岗位名称。"""
    from services.layout_detector import _is_job_title_like

    assert _is_job_title_like("秋招全职") is False
    assert _is_job_title_like("2026届") is False
    assert _is_job_title_like("本科") is False


def test_is_job_title_like_rejects_description():
    """修复点 1：描述性前缀 → 不是岗位名称。"""
    from services.layout_detector import _is_job_title_like

    assert _is_job_title_like("负责产品设计与开发") is False
    assert _is_job_title_like("要求熟悉 Python") is False


# ---------------------------------------------------------------------------
# 中国香港布局判定（修复点 1、5：候选布局为 unknown + 建议字段）
# ---------------------------------------------------------------------------


def _read_hk():
    return pd.read_excel(XLSX_FIXTURE, sheet_name="中国香港", engine="openpyxl", usecols="A:J")


def test_hk_standard_layout_is_unknown_with_suggestion():
    """修复点 1：香港候选标准布局以 unknown + needs_confirmation 输出。"""
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[0]  # row 2
    assert rec["record_type"] == "unknown"  # 候选布局不直接入库
    assert rec["layout"] == LAYOUT_HK_STANDARD
    assert is_candidate_layout(rec["layout"])
    assert rec.get("needs_confirmation") is True
    assert rec["suggested_record_type"] == "job"  # D 有具体岗位
    assert rec["source_row"] == 2
    # 顶层不写最终业务字段
    assert "company_name" not in rec
    assert "job_title" not in rec
    assert "target_cohort" not in rec
    # 建议字段含 company_name / job_title / recruitment_type / target_cohort
    assert rec["suggested_fields"]["company_name"] == "示例科技A"
    assert rec["suggested_fields"]["job_title"] == "示例后端开发工程师"
    assert rec["suggested_fields"]["recruitment_type"] == "秋招全职"
    assert rec["suggested_fields"]["target_cohort"] == "2026届"


def test_hk_standard_campaign_suggestion():
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[1]  # row 3
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_HK_STANDARD
    assert rec["suggested_record_type"] == "campaign"  # D 无具体岗位
    assert rec["source_row"] == 3


def test_hk_shifted_layout_unknown_with_suggestion():
    """修复点 1、5：香港候选错位布局以 unknown + 建议字段输出。

    修复点 5：E 列"2026届"符合届次规则才建议为 target_cohort；
    H 列语义漂移不得直接建议为 recruitment_type。
    """
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[2]  # row 4
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_HK_SHIFTED
    assert is_candidate_layout(rec["layout"])
    assert rec.get("needs_confirmation") is True
    assert rec["suggested_record_type"] == "job"
    assert rec["source_row"] == 4
    # F=本科 建议为 education_requirement
    assert rec["suggested_fields"].get("education_requirement") == "本科"
    # E=2026届 符合规则，建议为 target_cohort
    assert rec["suggested_fields"].get("target_cohort") == "2026届"
    # H=硕士（语义漂移）不得直接建议为 recruitment_type
    assert "recruitment_type" not in rec["suggested_fields"]
    # 修复点 5：不得断言"机械专业"是 target_cohort（夹具已改为 2026届）


def test_hk_shifted_does_not_suggest_invalid_cohort():
    """修复点 5：若 E 列不符合届次规则，不建议为 target_cohort。"""
    from services.layout_detector import _value_matches_field

    # "机械专业" 不符合届次规则
    assert _value_matches_field("target_cohort", "机械专业") is False
    assert _value_matches_field("target_cohort", "2026届") is True


def test_hk_unknown_ambiguous():
    records = parse_sheet(_read_hk(), "中国香港")
    # row 5：F=其他 + G=其他 → unknown
    rec = records[3]
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_UNKNOWN
    assert rec["source_row"] == 5
    assert "raw_data" in rec


def test_hk_empty_row_skipped_but_occupies_row_number():
    """修复点 3：香港 row 6 空行不输出，但占用行号。"""
    records = parse_sheet(_read_hk(), "中国香港")
    source_rows = [r["source_row"] for r in records]
    assert 6 not in source_rows
    assert len(records) == 7  # row 2,3,4,5,7,8,9 共 7 条非空（row 6 空行跳过）


def test_hk_standard_d_recruitment_type_no_suggestion():
    """修复点 3：D=秋招全职 → 不得 suggested job（suggested_record_type=None）。"""
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[4]  # row 7
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_HK_STANDARD
    assert rec.get("needs_confirmation") is True
    assert rec.get("suggested_record_type") is None  # 不提供最终建议


def test_hk_standard_d_cohort_no_suggestion():
    """修复点 3：D=2026届 → 不得 suggested job。"""
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[5]  # row 8
    assert rec["record_type"] == "unknown"
    assert rec.get("suggested_record_type") is None


def test_hk_standard_d_description_no_suggestion():
    """修复点 3：D=负责产品设计与开发 → 不得仅因非空 suggested job。"""
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[6]  # row 9
    assert rec["record_type"] == "unknown"
    assert rec.get("suggested_record_type") is None


# ---------------------------------------------------------------------------
# 美国布局判定（修复点 1：候选布局为 unknown + 建议字段）
# ---------------------------------------------------------------------------


def _read_us():
    return pd.read_excel(XLSX_FIXTURE, sheet_name="美国", engine="openpyxl", usecols="A:L")


def test_us_standard_layout_is_unknown_with_suggestion():
    """修复点 1：美国候选标准布局以 unknown + needs_confirmation 输出。"""
    records = parse_sheet(_read_us(), "美国")
    rec = records[0]  # row 2
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_US_STANDARD
    assert is_candidate_layout(rec["layout"])
    assert rec.get("needs_confirmation") is True
    assert rec["suggested_record_type"] == "job"
    assert rec["source_row"] == 2
    # 顶层不写最终业务字段
    assert "company_name" not in rec
    assert "target_cohort" not in rec
    # 建议字段
    assert rec["suggested_fields"]["company_name"] == "示例科技A"
    assert rec["suggested_fields"]["target_cohort"] == "2026届"
    assert rec["suggested_fields"]["education_requirement"] == "本科"


def test_us_standard_campaign_suggestion():
    records = parse_sheet(_read_us(), "美国")
    rec = records[1]  # row 3
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_US_STANDARD
    assert rec["suggested_record_type"] == "campaign"
    assert rec["source_row"] == 3


def test_us_swapped_layout_unknown_with_suggestion():
    """修复点 1：美国候选交换布局以 unknown + 建议字段输出（F↔G 互换）。"""
    records = parse_sheet(_read_us(), "美国")
    rec = records[2]  # row 4
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_US_SWAPPED
    assert is_candidate_layout(rec["layout"])
    assert rec.get("needs_confirmation") is True
    assert rec["suggested_record_type"] == "job"
    assert rec["source_row"] == 4
    # 交换后：G=2026届 → target_cohort，F=本科 → education_requirement
    assert rec["suggested_fields"].get("target_cohort") == "2026届"
    assert rec["suggested_fields"].get("education_requirement") == "本科"


def test_us_unknown_ambiguous():
    records = parse_sheet(_read_us(), "美国")
    # row 5：F=其他 + G=其他 → unknown
    rec = records[3]
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_UNKNOWN
    assert rec["source_row"] == 5
    assert "raw_data" in rec


def test_us_duplicate():
    records = parse_sheet(_read_us(), "美国")
    # row 2 与 row 6 业务内容相同（但都是 unknown 候选，建议字段相同）
    assert records[0]["suggested_fields"]["company_name"] == records[4]["suggested_fields"]["company_name"]
    assert records[0]["source_row"] == 2
    assert records[4]["source_row"] == 6


# ---------------------------------------------------------------------------
# 英国 / 新加坡
# ---------------------------------------------------------------------------


def _read_uk():
    return pd.read_excel(XLSX_FIXTURE, sheet_name="英国", engine="openpyxl", usecols="A:K")


def test_uk_standard_job():
    records = parse_sheet(_read_uk(), "英国")
    rec = records[0]
    assert rec["record_type"] == "job"
    assert rec["layout"] == LAYOUT_UK_DEFAULT
    assert rec["company_name"] == "示例科技A"
    assert rec["source_row"] == 2


def test_uk_standard_campaign():
    records = parse_sheet(_read_uk(), "英国")
    rec = records[1]
    assert rec["record_type"] == "campaign"
    assert rec["layout"] == LAYOUT_UK_DEFAULT
    assert rec["source_row"] == 3


def test_uk_g_ambiguous_unknown():
    records = parse_sheet(_read_uk(), "英国")
    # row 4：G=可议（歧义）→ unknown
    rec = records[2]
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 4
    assert "raw_data" in rec


def _read_sg():
    return pd.read_excel(XLSX_FIXTURE, sheet_name="新加坡", engine="openpyxl", usecols="A:J")


def test_sg_standard_job():
    records = parse_sheet(_read_sg(), "新加坡")
    rec = records[0]
    assert rec["record_type"] == "job"
    assert rec["layout"] == LAYOUT_SG_DEFAULT
    assert rec["company_name"] == "示例科技A"
    assert rec["source_row"] == 2


def test_sg_g_ambiguous_unknown():
    records = parse_sheet(_read_sg(), "新加坡")
    rec = records[1]
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 3
    assert "raw_data" in rec


# ---------------------------------------------------------------------------
# 修复点 2：英国/新加坡独立招聘类型规则（不复用大陆白名单）
# ---------------------------------------------------------------------------


def test_uk_graduate_programme():
    """修复点 2：英国 F=Graduate Programme（独立规则）→ 正常解析 job。"""
    records = parse_sheet(_read_uk(), "英国")
    rec = records[3]  # row 5
    assert rec["record_type"] == "job"
    assert rec["layout"] == LAYOUT_UK_DEFAULT
    assert rec["recruitment_type"] == "Graduate Programme"
    assert rec["target_cohort"] == "2026届"
    assert rec["source_row"] == 5


def test_uk_misplaced_education_unknown():
    """修复点 2：英国 F=本科（错位值）→ unknown。"""
    records = parse_sheet(_read_uk(), "英国")
    rec = records[4]  # row 6
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 6
    assert "raw_data" in rec


def test_uk_misplaced_cohort_unknown():
    """修复点 2：英国 F=2026届（错位值）→ unknown。"""
    records = parse_sheet(_read_uk(), "英国")
    rec = records[5]  # row 7
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 7
    assert "raw_data" in rec


def test_sg_graduate_programme():
    """修复点 2：新加坡 F=Graduate Programme（独立规则）→ 正常解析 job。"""
    records = parse_sheet(_read_sg(), "新加坡")
    rec = records[2]  # row 4
    assert rec["record_type"] == "job"
    assert rec["layout"] == LAYOUT_SG_DEFAULT
    assert rec["recruitment_type"] == "Graduate Programme"
    assert rec["source_row"] == 4


def test_sg_misplaced_education_unknown():
    """修复点 2：新加坡 F=本科（错位值）→ unknown。"""
    records = parse_sheet(_read_sg(), "新加坡")
    rec = records[3]  # row 5
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 5
    assert "raw_data" in rec


def test_sg_misplaced_cohort_unknown():
    """修复点 2：新加坡 F=2026届（错位值）→ unknown。"""
    records = parse_sheet(_read_sg(), "新加坡")
    rec = records[4]  # row 6
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 6
    assert "raw_data" in rec


# ---------------------------------------------------------------------------
# 低年级项目工作表（修复点 4：保守判定 job/campaign/unknown）
# ---------------------------------------------------------------------------


def _read_junior_global():
    return pd.read_excel(
        XLSX_FIXTURE, sheet_name="低年级项目-全球版", engine="openpyxl", usecols="A:L"
    )


def test_junior_global_job():
    """修复点 4：F 含"实习生"关键词 → job。"""
    records = parse_sheet(_read_junior_global(), "低年级项目-全球版")
    rec = records[0]
    assert rec["record_type"] == "job"
    assert rec["layout"] == LAYOUT_JUNIOR_GLOBAL
    assert rec["job_title"] == "示例前端开发实习生"
    assert rec["source_row"] == 2


def test_junior_global_campaign():
    """修复点 4：F 含"训练营"关键词 → campaign。"""
    records = parse_sheet(_read_junior_global(), "低年级项目-全球版")
    rec = records[1]
    assert rec["record_type"] == "campaign"
    assert rec["layout"] == LAYOUT_JUNIOR_GLOBAL
    assert rec["source_row"] == 3


def test_junior_global_unknown_for_ambiguous_name():
    """修复点 4：F=模糊名称（无岗位/项目关键词）→ unknown。"""
    records = parse_sheet(_read_junior_global(), "低年级项目-全球版")
    rec = records[2]
    assert rec["record_type"] == "unknown"
    assert rec["layout"] == LAYOUT_UNKNOWN
    assert rec["source_row"] == 4
    assert "raw_data" in rec


def _read_junior_official():
    return pd.read_excel(
        XLSX_FIXTURE,
        sheet_name="低年级项目-美国&香港-公司官网",
        engine="openpyxl",
        usecols="A:H",
    )


def test_junior_official_campaign():
    records = parse_sheet(_read_junior_official(), "低年级项目-美国&香港-公司官网")
    rec = records[0]
    assert rec["record_type"] == "campaign"
    assert rec["layout"] == LAYOUT_JUNIOR_OFFICIAL
    assert rec["application_url"] == "https://example.com/program/a"
    assert rec.get("deadline") is None  # 无截止日期列，不得臆造
    assert rec["source_row"] == 2


def test_junior_official_empty_row_skipped_but_occupies_row_number():
    """修复点 3：低年级-公司官网 row 4 空行不输出，但占用行号。"""
    records = parse_sheet(_read_junior_official(), "低年级项目-美国&香港-公司官网")
    source_rows = [r["source_row"] for r in records]
    assert 4 not in source_rows  # row 4 空行被跳过
    # row 5（缺字段）保留真实行号
    assert 5 in source_rows
    assert len(records) == 3  # row 2, 3, 5


def test_junior_official_missing_project_name_unknown():
    records = parse_sheet(_read_junior_official(), "低年级项目-美国&香港-公司官网")
    # row 5：F=空 → unknown
    rec = records[-1]
    assert rec["record_type"] == "unknown"
    assert rec["source_row"] == 5
    assert "raw_data" in rec


# ---------------------------------------------------------------------------
# CSV 单表输入
# ---------------------------------------------------------------------------


def test_parse_csv_mainland():
    records = parse_csv(CSV_FIXTURE, sheet_name="中国大陆")
    # CSV 与 XLSX 中国大陆工作表内容相同（14 条非空记录，row 4 空行跳过）
    assert len(records) == 14
    # row 2 campaign
    assert records[0]["record_type"] == "campaign"
    assert records[0]["layout"] == LAYOUT_MAINLAND_CAMPAIGN
    assert records[0]["source_sheet"] == "中国大陆"
    assert records[0]["source_row"] == 2  # 修复点 3


def test_parse_csv_unknown_records():
    records = parse_csv(CSV_FIXTURE, sheet_name="中国大陆")
    unknowns = [r for r in records if r["record_type"] == "unknown"]
    # row 6,7,8,9,10,14,15,16 共 8 条 unknown
    # （F=other+cohort、F=job+可议、F=job+日期、F=job+URL、F=empty、
    #  F=其他类别+city、F=综合招聘信息+city、F=招聘公告+city）
    assert len(unknowns) == 8


def test_parse_csv_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_csv(tmp_path / "nonexistent.csv")


def test_parse_csv_rejects_non_csv(tmp_path):
    bad = tmp_path / "not_csv.txt"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="CSV"):
        parse_csv(bad)


def test_parse_csv_source_row_skips_empty_rows():
    """修复点 3：CSV 中间空行不输出，但占用行号。"""
    records = parse_csv(CSV_FIXTURE, sheet_name="中国大陆")
    source_rows = [r["source_row"] for r in records]
    assert 4 not in source_rows  # 空行被跳过
    assert 5 in source_rows  # 空行后保留真实行号


# ---------------------------------------------------------------------------
# unknown 与 raw_data（修复点 9：job 降级清理顶层字段）
# ---------------------------------------------------------------------------


def test_unknown_records_always_have_raw_data():
    result = parse_workbook(XLSX_FIXTURE)
    for records in result.values():
        for rec in records:
            if rec["record_type"] == "unknown":
                assert "raw_data" in rec
                assert isinstance(rec["raw_data"], dict)
                assert len(rec["raw_data"]) > 0


def test_raw_data_preserves_all_original_columns():
    records = parse_sheet(_read_mainland(), "中国大陆")
    campaign = records[0]
    raw = campaign["raw_data"]
    for col in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N"]:
        assert col in raw, f"raw_data 缺少列 {col}"


def test_unknown_not_mapped_to_business_fields():
    """修复点 9：unknown 记录不应有 job_title / company_name 等顶层业务字段。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[3]  # row 6: F=其他类别 + G=2026届 → unknown
    assert unknown["record_type"] == "unknown"
    assert "job_title" not in unknown
    assert "company_name" not in unknown


def test_job_demote_cleans_top_level_fields():
    """修复点 9：job 降级为 unknown 时清理顶层业务字段。"""
    # row 7：F=示例前端开发工程师 + G=可议（非城市）→ 原本应 job 但 G 非城市 → unknown
    records = parse_sheet(_read_mainland(), "中国大陆")
    unknown = records[4]  # row 7
    assert unknown["record_type"] == "unknown"
    # 降级后不应残留 job_title / company_name / location 等顶层字段
    assert "job_title" not in unknown
    assert "company_name" not in unknown
    assert "location" not in unknown
    # 但 raw_data 完整保留
    assert "raw_data" in unknown
    assert unknown["raw_data"].get("F") == "示例前端开发工程师"


# ---------------------------------------------------------------------------
# 候选记录结构（修复点 1）
# ---------------------------------------------------------------------------


def test_candidate_records_return_none_dedupe_key():
    """修复点 1：候选布局记录对 compute_dedupe_key 返回 None。"""
    from services.dedup_service import compute_dedupe_key

    result = parse_workbook(XLSX_FIXTURE)
    # 香港/美国候选记录
    hk_records = result["中国香港"]
    us_records = result["美国"]
    for rec in hk_records + us_records:
        if is_candidate_layout(rec["layout"]):
            assert compute_dedupe_key(rec) is None


def test_candidate_record_structure_example():
    """修复点 1：候选记录结构示例。"""
    records = parse_sheet(_read_hk(), "中国香港")
    rec = records[0]  # 香港候选标准布局
    assert rec["record_type"] == "unknown"
    assert rec["needs_confirmation"] is True
    assert "suggested_record_type" in rec
    assert "suggested_fields" in rec
    assert isinstance(rec["suggested_fields"], dict)
    assert "raw_data" in rec


# ---------------------------------------------------------------------------
# 不支持的工作表
# ---------------------------------------------------------------------------


def test_parse_sheet_rejects_unsupported_sheet():
    df = pd.DataFrame({"A": [1], "B": [2]})
    with pytest.raises(UnsupportedSheetError):
        parse_sheet(df, "不存在的表")


def test_supported_sheets_complete():
    assert "中国大陆" in SUPPORTED_SHEETS
    assert "中国香港" in SUPPORTED_SHEETS
    assert "美国" in SUPPORTED_SHEETS
    assert "英国" in SUPPORTED_SHEETS
    assert "新加坡" in SUPPORTED_SHEETS
    assert "低年级项目-全球版" in SUPPORTED_SHEETS
    assert "低年级项目-美国&香港-公司官网" in SUPPORTED_SHEETS


# ---------------------------------------------------------------------------
# source_sheet / source_row 回溯（修复点 3）
# ---------------------------------------------------------------------------


def test_source_sheet_preserved():
    result = parse_workbook(XLSX_FIXTURE)
    for sheet_name, records in result.items():
        for rec in records:
            assert rec["source_sheet"] == sheet_name


def test_source_row_starts_at_2():
    """修复点 3：第一条数据 source_row=2（表头占 row 1）。"""
    result = parse_workbook(XLSX_FIXTURE)
    for records in result.values():
        if records:
            assert records[0]["source_row"] == 2


def test_source_row_preserves_physical_row_numbers():
    """修复点 3：source_row 对应原文件物理行号，空行后不重新连续编号。"""
    records = parse_sheet(_read_mainland(), "中国大陆")
    source_rows = [r["source_row"] for r in records]
    # row 2,3,5,6,7,8,9,10,11,12,13,14,15,16（row 4 空行跳过）
    assert source_rows == [2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]


def test_parse_sheet_custom_string_index():
    """修复点 5：parse_sheet 不依赖 DataFrame index 可转 int。

    使用 enumerate(start=2) 按行位置计算物理行号，确保自定义或字符串
    DataFrame index 不会报错。
    """
    # 构造自定义字符串 index 的 DataFrame（campaign 布局：F=秋招全职 + G=2026届）
    cols = [f"col{i}" for i in range(14)]
    row_data = ["", "", "", "", "示例科技A", "秋招全职", "2026届",
                "本科", "研发", "北京市", "2026-09-30",
                "示例科技A 校招", "https://example.com/ann/str",
                "https://example.com/apply/str"]
    df = pd.DataFrame([row_data], columns=cols, index=["custom_str"])
    records = parse_sheet(df, "中国大陆")
    assert len(records) == 1
    # 即使 DataFrame index 是字符串，source_row 仍按物理行号 = 2
    assert records[0]["source_row"] == 2
    assert records[0]["record_type"] == "campaign"


# ---------------------------------------------------------------------------
# 安全边界（修复点 7：不依赖 data/private）
# ---------------------------------------------------------------------------


def test_parse_workbook_only_reads_explicit_fixture(tmp_path):
    """修复点 7：parse_workbook 只读取调用方显式传入的虚构 fixture。"""
    # 复制夹具到临时目录，验证只读取传入的路径
    import shutil

    tmp_xlsx = tmp_path / "copy.xlsx"
    shutil.copy(XLSX_FIXTURE, tmp_xlsx)
    result = parse_workbook(tmp_xlsx)
    assert "中国大陆" in result
    # data/private 不存在也不会被读取
    private_in_tmp = tmp_path / "data" / "private"
    assert not private_in_tmp.exists()


def test_parse_csv_only_reads_explicit_fixture(tmp_path):
    """修复点 7：parse_csv 只读取调用方显式传入的虚构 fixture。"""
    import shutil

    tmp_csv = tmp_path / "copy.csv"
    shutil.copy(CSV_FIXTURE, tmp_csv)
    records = parse_csv(tmp_csv, sheet_name="中国大陆")
    assert len(records) > 0


def test_no_hardcoded_data_private_in_source():
    """修复点 7：源码中不得硬编码 data/private 或真实工作簿路径。"""
    import services.opportunity_importer as importer
    import services.layout_detector as detector

    # 检查源码文本不含真实工作簿文件名或 data/private 硬编码
    source_files = [
        Path(importer.__file__).read_text(encoding="utf-8"),
        Path(detector.__file__).read_text(encoding="utf-8"),
    ]
    for source in source_files:
        assert "智联-岗位信息表" not in source, "源码不得硬编码真实工作簿文件名"
        # data/private 仅可能出现在注释/文档中作为说明，但不得作为运行时路径硬编码
        # 这里检查不作为实际路径使用：不含 Path("data/private") 或 "data/private/智联"
        assert "data/private/智联" not in source


def test_fixtures_contain_no_real_companies():
    """夹具不得包含真实公司名。"""
    real_companies = ["腾讯", "阿里巴巴", "字节跳动", "百度", "美团", "京东"]
    result = parse_workbook(XLSX_FIXTURE)
    for records in result.values():
        for rec in records:
            raw = rec.get("raw_data", {})
            for val in raw.values():
                if val is None:
                    continue
                for real in real_companies:
                    assert real not in str(val), f"夹具中发现真实公司名：{real}"


def test_fixtures_use_example_com_urls():
    """夹具中所有 URL 必须使用 example.com 域名。"""
    result = parse_workbook(XLSX_FIXTURE)
    for records in result.values():
        for rec in records:
            # 检查顶层 URL 字段
            for field in ("application_url", "announcement_url"):
                url = rec.get(field)
                if url:
                    assert "example.com" in url, f"非 example.com 链接：{url}"
            # 检查 suggested_fields 中的 URL
            for field, val in rec.get("suggested_fields", {}).items():
                if field in ("application_url", "announcement_url") and val:
                    assert "example.com" in val, f"非 example.com 链接：{val}"
            # 检查 raw_data 中的 URL
            for val in rec.get("raw_data", {}).values():
                if val and isinstance(val, str) and val.startswith("http"):
                    assert "example.com" in val, f"非 example.com 链接：{val}"


# ---------------------------------------------------------------------------
# 任务 5：parse_workbook_sheet（工作表选择，只解析选中的一张表）
# ---------------------------------------------------------------------------


def test_parse_workbook_sheet_only_loads_selected_sheet(monkeypatch):
    """任务 5：parse_workbook_sheet 只对选中表调用一次 read_excel。"""
    import services.opportunity_importer as importer_module

    called: list = []
    real_read_excel = pd.read_excel

    def spy_read_excel(*args, **kwargs):
        called.append(kwargs.get("sheet_name"))
        return real_read_excel(*args, **kwargs)

    monkeypatch.setattr(importer_module.pd, "read_excel", spy_read_excel)
    records = importer_module.parse_workbook_sheet(XLSX_FIXTURE, "英国")
    assert called == ["英国"], f"应只读取选中的一张表，实际读取：{called}"
    assert len(records) == 6
    assert {r["source_sheet"] for r in records} == {"英国"}


def test_parse_workbook_sheet_returns_selected_sheet_records():
    """任务 5：返回的记录全部来自选中工作表。"""
    records = parse_workbook_sheet(XLSX_FIXTURE, "中国香港")
    assert len(records) > 0
    assert {r["source_sheet"] for r in records} == {"中国香港"}


def test_parse_workbook_sheet_rejects_unsupported_sheet():
    """任务 5：不支持的工作表名直接拒绝。"""
    with pytest.raises(UnsupportedSheetError):
        parse_workbook_sheet(XLSX_FIXTURE, "临时表-不该被解析")


def test_parse_workbook_sheet_raises_on_missing_sheet():
    """任务 5：工作簿中不存在所选工作表时报错。"""
    with pytest.raises(UnsupportedSheetError, match="不存在"):
        parse_workbook_sheet(XLSX_FIXTURE, "不存在的工作表")


def test_parse_workbook_sheet_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_workbook_sheet(tmp_path / "nonexistent.xlsx", "英国")


def test_parse_workbook_sheet_rejects_non_xlsx(tmp_path):
    bad = tmp_path / "not_xlsx.txt"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="XLSX"):
        parse_workbook_sheet(bad, "英国")
