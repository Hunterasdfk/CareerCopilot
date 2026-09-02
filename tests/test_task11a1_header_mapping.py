"""任务 11A.1：表头驱动识别测试。

修复"第 287 类结构化职位"（明确公司名称 + 职位名称 + 招聘类型 + 届次
+ 学历 + 截止时间 + 职位链接）被误判 unknown / campaign 的问题。

测试数据完全虚构（示例顾问公司 / 示例科技公司 / 示例职位 /
https://example.com/...），不访问网络、不读取 data/private。

覆盖（对应用户任务书第八节 1—18）：
1-4   表头驱动 job 判定与 campaign 优先级；
5     列顺序变化后分类与字段完全一致；
6-8   中文别名 / 英文别名 / 换行空格全角括号表头；
9     职位简介不映射为公司、职位或地点；
10-11 generic_url 安全映射与非法 URL 不映射；
12    重复公司表头 → unknown + 表头冲突原因；
13    无可靠表头 → 不得强行判 job；
14-15 现有 campaign 表头数据与旧版固定列布局继续通过；
16    结构化职位不再进入待人工确认（pending）；
17    不引用 data/private；
18    直接导入（防云端 ImportError）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database.db_handler import get_connection, init_db
from services.layout_detector import (
    LAYOUT_MAINLAND_CAMPAIGN,
    LAYOUT_MAINLAND_HEADER_CAMPAIGN,
    LAYOUT_MAINLAND_HEADER_JOB,
    LAYOUT_MAINLAND_JOB,
    LAYOUT_UNKNOWN,
    SUPPORTED_SHEETS,
    HeaderMapping,
    _is_valid_job_title_value,
    detect_mainland,
    get_detection_reason_display,
    normalize_header,
    resolve_header_mapping,
)
from services.opportunity_service import (
    CATEGORY_NEW,
    CATEGORY_PENDING,
    classify_records,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 第 287 类结构化职位（用户场景）：更新日期/行业/公司名称/职位名称/
# 职位简介/招聘类型/招聘届次/学历/截止时间/职位链接
# ---------------------------------------------------------------------------

HEADER_287: dict[str, str] = {
    "A": "更新日期",
    "B": "行业",
    "C": "公司名称",
    "D": "职位名称",
    "E": "职位简介",
    "F": "招聘类型",
    "G": "招聘届次",
    "H": "学历",
    "I": "截止时间",
    "J": "职位链接",
}

ROW_287: dict[str, str] = {
    "A": "2026-08-01",
    "B": "咨询",
    "C": "示例顾问公司",
    "D": "示例职位",
    "E": "负责示例业务",
    "F": "社招全职",
    "G": "2025/2026届",
    "H": "本科及以上",
    "I": "2026-09-30",
    "J": "https://example.com/jobs/123",
}


# ---------------------------------------------------------------------------
# 1-4：表头驱动 job 判定
# ---------------------------------------------------------------------------


class TestHeaderDrivenJob:
    def test_structured_job_shezhao_multi_cohort(self):
        """1. 公司名称 + 明确职位名称 + 社招全职 + 2025/2026届 → job。"""
        rec = detect_mainland(ROW_287, 2, headers=HEADER_287)
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        assert rec["company_name"] == "示例顾问公司"
        assert rec["job_title"] == "示例职位"
        assert rec["recruitment_type"] == "社招全职"
        assert rec["target_cohort"] == "2025/2026届"
        assert rec["education_requirement"] == "本科及以上"
        assert rec["deadline"] == "2026-09-30"
        assert rec["application_url"] == "https://example.com/jobs/123"
        assert rec["display_title"] == "示例职位"

    def test_job_title_without_keywords(self):
        """2. 职位名称不含"工程师、开发、岗位"等关键词仍为 job。"""
        row = dict(ROW_287, D="示例商务代表")
        rec = detect_mainland(row, 2, headers=HEADER_287)
        assert rec["record_type"] == "job"
        assert rec["job_title"] == "示例商务代表"

    def test_job_without_location_column(self):
        """3. 没有独立 location 列仍为 job，location 留空。"""
        headers = {k: v for k, v in HEADER_287.items() if k != "J"}
        row = {k: v for k, v in ROW_287.items() if k != "J"}
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "job"
        assert rec.get("location") is None

    def test_shezhao_with_job_title_not_campaign(self):
        """4. 社招全职 + 2025/2026届 且存在明确职位表头，不得判 campaign。"""
        rec = detect_mainland(ROW_287, 2, headers=HEADER_287)
        assert rec["record_type"] == "job"
        assert rec["layout"] != LAYOUT_MAINLAND_CAMPAIGN
        assert rec["layout"] != LAYOUT_MAINLAND_HEADER_CAMPAIGN


# ---------------------------------------------------------------------------
# 5：列顺序变化
# ---------------------------------------------------------------------------


class TestColumnReorder:
    def test_reordered_columns_same_result(self):
        """5. 相同字段改变列顺序后分类和标准字段完全相同。"""
        reordered_headers = {
            "A": "职位名称",
            "B": "公司名称",
            "C": "招聘届次",
            "D": "更新日期",
            "E": "行业",
            "F": "职位简介",
            "G": "招聘类型",
            "H": "学历",
            "I": "职位链接",
            "J": "截止时间",
        }
        reordered_row = {
            "A": "示例职位",
            "B": "示例顾问公司",
            "C": "2025/2026届",
            "D": "2026-08-01",
            "E": "咨询",
            "F": "负责示例业务",
            "G": "社招全职",
            "H": "本科及以上",
            "I": "https://example.com/jobs/123",
            "J": "2026-09-30",
        }
        rec1 = detect_mainland(ROW_287, 2, headers=HEADER_287)
        rec2 = detect_mainland(reordered_row, 2, headers=reordered_headers)
        assert rec1["record_type"] == rec2["record_type"] == "job"
        assert rec1["layout"] == rec2["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        for field_name in (
            "company_name",
            "job_title",
            "display_title",
            "industry",
            "recruitment_type",
            "target_cohort",
            "education_requirement",
            "location",
            "deadline",
            "application_url",
        ):
            assert rec1.get(field_name) == rec2.get(field_name), field_name


# ---------------------------------------------------------------------------
# 6-8：表头别名与规范化
# ---------------------------------------------------------------------------


class TestHeaderAliases:
    def test_chinese_alias_headers(self):
        """6. 中文表头别名可以识别。"""
        headers = {
            "A": "雇主",
            "B": "岗位",
            "C": "用工类型",
            "D": "目标届次",
            "E": "学历要求",
            "F": "工作城市",
            "G": "申请链接",
        }
        row = {
            "A": "示例科技公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
            "E": "本科",
            "F": "北京市",
            "G": "https://example.com/jobs/456",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "job"
        assert rec["company_name"] == "示例科技公司"
        assert rec["job_title"] == "示例职位"
        assert rec["recruitment_type"] == "社招全职"
        assert rec["target_cohort"] == "2025/2026届"
        assert rec["location"] == "北京市"
        assert rec["application_url"] == "https://example.com/jobs/456"

    def test_english_alias_headers(self):
        """7. 英文表头别名可以识别。"""
        headers = {
            "A": "Company",
            "B": "Position",
            "C": "Employment Type",
            "D": "Target Cohort",
            "E": "Education",
            "F": "Location",
            "G": "Application URL",
        }
        row = {
            "A": "示例科技公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
            "E": "本科",
            "F": "远程",
            "G": "https://example.com/jobs/789",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "job"
        assert rec["company_name"] == "示例科技公司"
        assert rec["job_title"] == "示例职位"
        assert rec["recruitment_type"] == "社招全职"
        assert rec["target_cohort"] == "2025/2026届"
        assert rec["location"] == "远程"
        assert rec["application_url"] == "https://example.com/jobs/789"

    def test_headers_with_newline_space_fullwidth(self):
        """8. 表头含换行、空格和全角括号可以识别。"""
        headers = {
            "A": "公司\n名称 ",
            "B": "职 位 名 称",
            "C": "招聘类型",
            "D": "招聘对象（届次）",
            "E": "学历要求",
            "F": "申请链接",
        }
        row = {
            "A": "示例科技公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
            "E": "本科",
            "F": "https://example.com/jobs/999",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "job"
        assert rec["company_name"] == "示例科技公司"
        assert rec["job_title"] == "示例职位"
        assert rec["target_cohort"] == "2025/2026届"
        assert rec["application_url"] == "https://example.com/jobs/999"


# ---------------------------------------------------------------------------
# 9：职位简介不得映射到其他字段
# ---------------------------------------------------------------------------


class TestDescriptionIsolation:
    def test_description_not_mapped_to_other_fields(self):
        """9. 职位简介不能被映射为公司、职位或地点。"""
        rec = detect_mainland(ROW_287, 2, headers=HEADER_287)
        assert rec["record_type"] == "job"
        # 不写入任何顶层标准字段
        assert "job_description" not in rec
        # 不得替换公司 / 职位 / 地点
        assert rec["company_name"] == "示例顾问公司"
        assert rec["job_title"] == "示例职位"
        assert rec.get("location") is None
        # 原文完整保留在 raw_data
        assert rec["raw_data"]["E"] == "负责示例业务"


# ---------------------------------------------------------------------------
# 10-11：generic_url 映射
# ---------------------------------------------------------------------------


class TestGenericUrl:
    def test_generic_url_mapped_to_application_url(self):
        """10. 一个 job 的 generic URL 可以安全映射为 application_url。"""
        headers = {
            "A": "公司名称",
            "B": "职位名称",
            "C": "招聘类型",
            "D": "招聘届次",
            "E": "学历",
            "F": "链接",
        }
        row = {
            "A": "示例科技公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
            "E": "本科",
            "F": "https://example.com/jobs/123",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "job"
        assert rec["application_url"] == "https://example.com/jobs/123"

    def test_non_http_url_not_mapped(self):
        """11. 非 HTTP/HTTPS 链接不得映射。"""
        headers = {
            "A": "公司名称",
            "B": "职位名称",
            "C": "招聘类型",
            "D": "招聘届次",
            "E": "学历",
            "F": "链接",
        }
        row = {
            "A": "示例科技公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
            "E": "本科",
            "F": "ftp://example.com/file",
        }
        rec = detect_mainland(row, 2, headers=headers)
        # 记录仍按表头结构判 job（公司/职位/招聘类型等信号完整）
        assert rec["record_type"] == "job"
        # 但非法 URL 不映射到 application_url
        assert rec.get("application_url") is None
        # 原始值保留在 raw_data
        assert rec["raw_data"]["F"] == "ftp://example.com/file"


# ---------------------------------------------------------------------------
# 12-13：表头冲突与不可靠表头
# ---------------------------------------------------------------------------


class TestHeaderConflicts:
    def test_duplicate_company_header_unknown(self):
        """12. 重复公司表头 → unknown，并给出表头冲突原因。"""
        headers = {
            "A": "公司名称",
            "B": "公司名称",
            "C": "职位名称",
            "D": "招聘类型",
            "E": "招聘届次",
        }
        row = {
            "A": "示例顾问公司",
            "B": "示例科技公司",
            "C": "示例职位",
            "D": "社招全职",
            "E": "2025/2026届",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "unknown"
        assert rec["layout"] == LAYOUT_UNKNOWN
        assert rec["detection_reason"] == "ambiguous_source_headers"
        # 不得静默选择第一个公司列
        assert "company_name" not in rec

    def test_conflict_fallback_keeps_fixed_layout(self):
        """冲突表头回退固定列：固定列可靠判定不受影响。"""
        headers = {"F": "公司名称", "G": "公司名称"}
        row = {"F": "秋招全职", "G": "2026届"}
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "campaign"
        assert rec["layout"] == LAYOUT_MAINLAND_CAMPAIGN

    def test_unreliable_headers_not_forced_job(self):
        """13. 普通非空字段但没有可靠表头 → 不得强行判 job。"""
        headers = {"A": "列1", "B": "列2", "C": "字段甲", "D": "备注"}
        row = {
            "A": "示例顾问公司",
            "B": "示例职位",
            "C": "社招全职",
            "D": "2025/2026届",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "unknown"
        assert "company_name" not in rec
        assert "job_title" not in rec


# ---------------------------------------------------------------------------
# 14-15：兼容现有规则
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    FIXTURE_HEADERS = {
        "A": "更新日期",
        "B": "开岗日期",
        "C": "企业性质",
        "D": "行业",
        "E": "企业名称",
        "F": "招聘岗位",
        "G": "招聘对象（届次）",
        "H": "学历要求",
        "I": "招聘类别",
        "J": "工作城市",
        "K": "截止时间",
        "L": "公告名称",
        "M": "公告链接",
        "N": "投递链接",
    }

    def test_existing_campaign_header_data_still_campaign(self):
        """14. 现有 campaign 表头数据继续判 campaign（mainland_campaign_v2）。"""
        # 旧模板：F=招聘类型关键词 + G=届次；I 列"研发"非招聘类型 →
        # 表头与数据语义不一致 → 回退固定列规则
        row = {
            "A": "2026-08-01",
            "B": "2026-07-20",
            "C": "民营",
            "D": "互联网",
            "E": "示例科技A",
            "F": "秋招全职",
            "G": "2026届",
            "H": "本科",
            "I": "研发",
            "J": "北京市",
            "K": "2026-09-30",
            "L": "示例科技A 2026秋季校园招聘",
            "M": "https://example.com/ann/a-2026",
            "N": "https://example.com/apply/a-2026",
        }
        rec = detect_mainland(row, 2, headers=self.FIXTURE_HEADERS)
        assert rec["record_type"] == "campaign"
        assert rec["layout"] == LAYOUT_MAINLAND_CAMPAIGN
        assert rec["recruitment_type"] == "秋招全职"
        assert rec["target_cohort"] == "2026届"

    def test_existing_job_header_data_still_job(self):
        """旧模板 job 行（表头与数据错位）继续走固定列 job_v1。"""
        row = {
            "A": "2026-08-02",
            "B": "2026-07-21",
            "C": "外资",
            "D": "金融",
            "E": "示例银行C",
            "F": "示例后端开发工程师",
            "G": "上海市",
            "H": "秋招全职",
            "I": "2026届",
            "J": "本科",
            "K": "2026-09-15",
            "L": "示例银行C 2026校招",
            "M": "https://example.com/ann/c-2026",
            "N": "https://example.com/apply/c-2026-4567",
        }
        rec = detect_mainland(row, 2, headers=self.FIXTURE_HEADERS)
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_JOB
        assert rec["job_title"] == "示例后端开发工程师"
        assert rec["location"] == "上海市"

    def test_fixed_layout_fallback_without_headers(self):
        """15. 旧版固定列布局继续通过（headers=None 回退）。"""
        campaign = detect_mainland({"F": "秋招全职", "G": "2026届"}, 2)
        assert campaign["record_type"] == "campaign"
        assert campaign["layout"] == LAYOUT_MAINLAND_CAMPAIGN

        job = detect_mainland(
            {"F": "示例后端开发工程师", "G": "上海市"}, 3
        )
        assert job["record_type"] == "job"
        assert job["layout"] == LAYOUT_MAINLAND_JOB

    def test_task11a_signature_still_works(self):
        """任务 11A 多字段签名（E/F/G + H/I/J）继续生效。"""
        row = {
            "E": "示例科技A",
            "F": "商务拓展",
            "G": "全国多地",
            "H": "秋招全职",
            "I": "2026届",
            "J": "本科",
            "N": "https://example.com/apply/x",
        }
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_JOB


# ---------------------------------------------------------------------------
# 表头驱动 campaign
# ---------------------------------------------------------------------------


class TestHeaderDrivenCampaign:
    def test_header_driven_campaign(self):
        """公司 + 招聘类型 + 届次（无职位名称表头）→ header_mapped_campaign_v1。"""
        headers = {
            "A": "公司名称",
            "B": "招聘类型",
            "C": "招聘届次",
            "D": "学历",
            "E": "公告标题",
            "F": "公告链接",
        }
        row = {
            "A": "示例科技公司",
            "B": "秋招全职",
            "C": "2026届",
            "D": "本科",
            "E": "示例科技公司 2026 校招",
            "F": "https://example.com/ann/x",
        }
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "campaign"
        assert rec["layout"] == LAYOUT_MAINLAND_HEADER_CAMPAIGN
        assert rec["company_name"] == "示例科技公司"
        assert rec["recruitment_type"] == "秋招全职"
        assert rec["target_cohort"] == "2026届"
        assert rec["announcement_title"] == "示例科技公司 2026 校招"
        assert rec["announcement_url"] == "https://example.com/ann/x"


# ---------------------------------------------------------------------------
# unknown 原因（表头相关）
# ---------------------------------------------------------------------------


class TestHeaderUnknownReasons:
    def test_missing_required_job_values(self):
        """职位名称明确但公司值为空 → missing_required_job_values。"""
        row = dict(ROW_287, C="")
        rec = detect_mainland(row, 2, headers=HEADER_287)
        assert rec["record_type"] == "unknown"
        assert rec["detection_reason"] == "missing_required_job_values"

    def test_incomplete_header_signature(self):
        """有公司 + 职位但无任何支持字段 → incomplete_header_signature。"""
        headers = {"A": "公司名称", "B": "职位名称"}
        row = {"A": "示例科技公司", "B": "示例职位"}
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "unknown"
        assert rec["detection_reason"] == "incomplete_header_signature"

    def test_missing_required_headers(self):
        """表头可靠但缺公司/职位表头 → 回退后 unknown 用表头视角原因。"""
        headers = {"A": "招聘类型", "B": "招聘届次", "C": "学历"}
        row = {"A": "社招全职", "B": "2025/2026届", "C": "本科"}
        rec = detect_mainland(row, 2, headers=headers)
        assert rec["record_type"] == "unknown"
        assert rec["detection_reason"] == "missing_required_headers"

    def test_new_reasons_have_chinese_display(self):
        """新原因均有中文显示。"""
        for reason in (
            "missing_required_headers",
            "ambiguous_source_headers",
            "conflicting_header_mapping",
            "missing_required_job_values",
            "incomplete_header_signature",
        ):
            display = get_detection_reason_display(reason)
            assert display and display != reason, reason


# ---------------------------------------------------------------------------
# 16：预览分类（结构化职位不再进入待人工确认）
# ---------------------------------------------------------------------------


class TestPreviewClassification:
    def test_structured_job_not_pending_in_preview(self):
        """16. 结构化职位不再出现在"待人工确认"（pending）。"""
        conn = get_connection(":memory:")
        try:
            init_db(conn)
            records = [detect_mainland(ROW_287, 2, headers=HEADER_287)]
            result = classify_records(records, conn)
            assert result["counts"][CATEGORY_NEW] == 1
            assert result["counts"][CATEGORY_PENDING] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 17：不引用 data/private
# ---------------------------------------------------------------------------


class TestNoPrivateDataAccess:
    def test_no_data_private_reference(self):
        """17. 识别与解析源码不引用、不扫描、不读取 data/private。"""
        sources = [
            PROJECT_ROOT / "services" / "layout_detector.py",
            PROJECT_ROOT / "services" / "opportunity_importer.py",
        ]
        for path in sources:
            source = path.read_text(encoding="utf-8")
            assert "data/private" not in source, path
            assert "智联" not in source, path


# ---------------------------------------------------------------------------
# 18：直接导入（防云端 ImportError）
# ---------------------------------------------------------------------------


class TestDirectImport:
    def test_direct_import_public_symbols(self):
        """18. from services.layout_detector import ... 不抛 ImportError。"""
        from services.layout_detector import (  # noqa: F401
            SUPPORTED_SHEETS as sheets,
        )
        from services.layout_detector import (  # noqa: F401
            get_detection_reason_display as display,
        )

        assert "中国大陆" in sheets
        assert display("unrecognized_cohort") == "届次格式无法识别"


# ---------------------------------------------------------------------------
# 表头解析函数独立测试
# ---------------------------------------------------------------------------


class TestNormalizeHeader:
    def test_strip_and_whitespace(self):
        assert normalize_header("  公司 名称  ") == "公司名称"
        assert normalize_header("公司\n名称") == "公司名称"
        assert normalize_header("公司\t名称") == "公司名称"
        assert normalize_header("公司  名称") == "公司名称"

    def test_case_fold(self):
        assert normalize_header("Company") == "company"
        assert normalize_header("APPLICATION URL") == "applicationurl"

    def test_fullwidth_to_halfwidth(self):
        assert normalize_header("招聘对象（届次）") == "招聘对象(届次)"
        assert normalize_header("ＵＲＬ") == "url"

    def test_empty_headers(self):
        assert normalize_header(None) == ""
        assert normalize_header("") == ""
        assert normalize_header("   ") == ""
        assert normalize_header("\n\t") == ""


class TestResolveHeaderMapping:
    def test_basic_mapping(self):
        hm = resolve_header_mapping(HEADER_287)
        assert hm.conflicts is False
        assert hm.has_mapping is True
        assert hm.field_to_col["company_name"] == "C"
        assert hm.field_to_col["job_title"] == "D"
        assert hm.field_to_col["job_description"] == "E"
        assert hm.field_to_col["recruitment_type"] == "F"
        assert hm.field_to_col["target_cohort"] == "G"
        assert hm.field_to_col["education_requirement"] == "H"
        assert hm.field_to_col["deadline"] == "I"
        assert hm.field_to_col["application_url"] == "J"
        # 未命中别名的表头不参与映射
        assert "industry" not in hm.field_to_col or \
            hm.field_to_col["industry"] == "B"
        assert "A" not in hm.col_to_field  # 更新日期未映射

    def test_duplicate_headers_not_mapped(self):
        hm = resolve_header_mapping(
            {"A": "公司名称", "B": "公司名称", "C": "职位名称"}
        )
        assert hm.conflicts is True
        assert "company_name" not in hm.field_to_col
        assert set(hm.duplicates) == {"A", "B"}
        assert hm.field_to_col.get("job_title") == "C"

    def test_two_cols_same_field_conflict(self):
        hm = resolve_header_mapping({"A": "公司名称", "B": "雇主"})
        assert hm.conflicts is True
        assert "company_name" not in hm.field_to_col

    def test_no_alias_hit(self):
        hm = resolve_header_mapping({"A": "列1", "B": "字段甲"})
        assert hm.has_mapping is False
        assert hm.conflicts is False

    def test_empty_headers_skipped(self):
        hm = resolve_header_mapping({"A": None, "B": "", "C": "  ", "D": "公司名称"})
        assert hm.field_to_col == {"company_name": "D"}

    def test_mapping_result_is_independent(self):
        """表头映射结果可独立测试：不依赖行数据。"""
        hm1 = resolve_header_mapping(HEADER_287)
        hm2 = resolve_header_mapping(HEADER_287)
        assert hm1.field_to_col == hm2.field_to_col
        assert isinstance(hm1, HeaderMapping)


class TestJobTitleValueValidation:
    def test_plain_text_is_valid(self):
        """不含岗位关键词的普通文本也可作为职位名称。"""
        assert _is_valid_job_title_value("示例职位")
        assert _is_valid_job_title_value("示例商务代表")

    def test_recruitment_keyword_invalid(self):
        """招聘类型关键词不是有效职位名称（旧模板错位保护）。"""
        assert not _is_valid_job_title_value("秋招全职")
        assert not _is_valid_job_title_value("社招全职")

    def test_cohort_education_url_date_invalid(self):
        assert not _is_valid_job_title_value("2026届")
        assert not _is_valid_job_title_value("本科")
        assert not _is_valid_job_title_value("https://example.com")
        assert not _is_valid_job_title_value("2026-09-30")

    def test_description_prefix_invalid(self):
        assert not _is_valid_job_title_value("负责示例业务")

    def test_empty_invalid(self):
        assert not _is_valid_job_title_value(None)
        assert not _is_valid_job_title_value("")
        assert not _is_valid_job_title_value("  ")


# ---------------------------------------------------------------------------
# 解析层全链路（parse_sheet 传入表头）
# ---------------------------------------------------------------------------


class TestParseSheetHeaderPropagation:
    def test_parse_sheet_with_structured_headers(self):
        """parse_sheet 从 DataFrame 列名建立表头映射并传给 detector。"""
        import pandas as pd

        from services.opportunity_importer import parse_sheet

        # 列名 = 中文表头（按 A→J 顺序），行值按列名对应
        df = pd.DataFrame(
            [{HEADER_287[k]: ROW_287[k] for k in HEADER_287}]
        )
        records = parse_sheet(df, "中国大陆")
        assert len(records) == 1
        rec = records[0]
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        assert rec["company_name"] == "示例顾问公司"
        assert rec["source_row"] == 2

    def test_parse_sheet_legacy_fixture_still_fixed_layout(self):
        """现有夹具（表头与数据错位的旧模板）仍走固定列布局。"""
        import pandas as pd

        from services.opportunity_importer import parse_sheet

        df = pd.DataFrame(
            [
                {
                    "更新日期": "2026-08-01", "开岗日期": "2026-07-20",
                    "企业性质": "民营", "行业": "互联网",
                    "企业名称": "示例科技A", "招聘岗位": "秋招全职",
                    "招聘对象（届次）": "2026届", "学历要求": "本科",
                    "招聘类别": "研发", "工作城市": "北京市",
                    "截止时间": "2026-09-30",
                    "公告名称": "示例科技A 2026秋季校园招聘",
                    "公告链接": "https://example.com/ann/a",
                    "投递链接": "https://example.com/apply/a",
                }
            ],
            columns=[
                "更新日期", "开岗日期", "企业性质", "行业", "企业名称",
                "招聘岗位", "招聘对象（届次）", "学历要求", "招聘类别",
                "工作城市", "截止时间", "公告名称", "公告链接", "投递链接",
            ],
        )
        records = parse_sheet(df, "中国大陆")
        assert len(records) == 1
        assert records[0]["record_type"] == "campaign"
        assert records[0]["layout"] == LAYOUT_MAINLAND_CAMPAIGN

    def test_reimport_header_mapped_job_dedupes(self):
        """表头驱动 job 生成标准 dedupe 字段，重复导入可去重。"""
        from services.dedup_service import compute_dedupe_key

        rec = detect_mainland(ROW_287, 2, headers=HEADER_287)
        key1 = compute_dedupe_key(rec)
        rec2 = detect_mainland(ROW_287, 5, headers=HEADER_287)
        key2 = compute_dedupe_key(rec2)
        assert key1 == key2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
