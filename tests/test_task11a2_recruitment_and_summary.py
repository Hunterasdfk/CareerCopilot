"""任务 11A.2：招聘类型补招/补录识别 + unknown 原因汇总测试。

修复"中国大陆第 287 行"（春招补招 + 2026/2027届）被误判
unknown / unrecognized_recruitment_type 的问题。

注意：JS Advisory 是"新加坡工作表第 287 行"，与"中国大陆第 287 行"
是不同工作表的独立行号，本任务只处理中国大陆场景。

测试数据完全虚构（示例显示公司 / 示例科技公司 / 示例职位 /
example.com），不访问网络、不读取 data/private。

覆盖（对应用户任务书第六节 1-17）：
1-4    四个补招/补录原子类型 → campaign；
5-8    组合分隔符 / , ， 、；
9      组合中一个非法部分 → unknown；
10     普通描述含"补招"不得视为招聘类型；
11     URL、日期、学历、届次不得视为招聘类型；
12     表头明确 company_name + job_title 时仍优先 job；
13-14  unknown 原因汇总数量正确、四类闭合；
15     页面汇总不展示 raw_data、公司名称或链接；
16     现有全部测试继续通过（由完整 pytest 保证）；
17     不读取或引用 data/private。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from database.db_handler import get_connection, init_db
from services.layout_detector import (
    LAYOUT_MAINLAND_CAMPAIGN,
    LAYOUT_MAINLAND_HEADER_JOB,
    LAYOUT_MAINLAND_JOB,
    LAYOUT_UNKNOWN,
    REASON_CATEGORY_DISPLAY,
    _is_recruitment_keyword,
    detect_mainland,
    summarize_unknown_reasons,
)
from services.opportunity_service import (
    CATEGORY_PENDING,
    classify_records,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 中国大陆第 287 类（虚构数据，旧模板固定列布局）
# ---------------------------------------------------------------------------

ROW_MAINLAND_287: dict[str, str] = {
    "E": "示例显示公司",
    "F": "春招补招",
    "G": "2026/2027届",
    "H": "本科及以上",
    "I": "示例岗位类别",
    "J": "成都",
    "L": "示例公司2026届校招补录计划",
    "M": "https://example.com/announcement",
    "N": "https://example.com/apply",
}

# 夹具式表头（与现有 fixtures 同构；I 列"招聘类别"值非法 → 可靠性失败
# → 回退固定列规则）
LEGACY_HEADERS: dict[str, str] = {
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


# ---------------------------------------------------------------------------
# 1-4：补招/补录原子类型 → campaign
# ---------------------------------------------------------------------------


class TestSupplementRecruitmentCampaign:
    def test_mainland_row287_chun_zhao_bu_zhao(self):
        """1. 中国大陆第 287 类：春招补招 + 2026/2027届 → campaign。"""
        rec = detect_mainland(ROW_MAINLAND_287, 287)
        assert rec["record_type"] == "campaign"
        assert rec["layout"] == LAYOUT_MAINLAND_CAMPAIGN
        assert rec["recruitment_type"] == "春招补招"
        assert rec["target_cohort"] == "2026/2027届"
        assert rec["company_name"] == "示例显示公司"
        assert rec["announcement_url"] == "https://example.com/announcement"
        assert rec["application_url"] == "https://example.com/apply"

    def test_mainland_row287_with_legacy_headers(self):
        """第 287 类带夹具式表头：表头数据错位回退固定列，仍判 campaign。"""
        rec = detect_mainland(ROW_MAINLAND_287, 287, headers=LEGACY_HEADERS)
        assert rec["record_type"] == "campaign"
        assert rec["layout"] == LAYOUT_MAINLAND_CAMPAIGN
        assert rec["recruitment_type"] == "春招补招"

    def test_mainland_row287_not_pending(self):
        """第 287 类预期 pending = 0（不再进入待人工确认）。"""
        conn = get_connection(":memory:")
        try:
            init_db(conn)
            records = [detect_mainland(ROW_MAINLAND_287, 287)]
            result = classify_records(records, conn)
            assert result["counts"][CATEGORY_PENDING] == 0
        finally:
            conn.close()

    def test_qiu_zhao_bu_zhao_campaign(self):
        """2. 秋招补招 + 2027届 → campaign。"""
        row = {"E": "示例科技公司", "F": "秋招补招", "G": "2027届"}
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "campaign"

    def test_chun_zhao_bu_lu_campaign(self):
        """3. 春招补录 + 2026届 → campaign。"""
        row = {"E": "示例科技公司", "F": "春招补录", "G": "2026届"}
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "campaign"

    def test_qiu_zhao_bu_lu_campaign(self):
        """4. 秋招补录 + 2026/2027届 → campaign。"""
        row = {"E": "示例显示公司", "F": "秋招补录", "G": "2026/2027届"}
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "campaign"

    def test_supplement_types_are_atomic_keywords(self):
        """四个原子类型均命中白名单（不含"补招"即真的宽泛判断）。"""
        assert _is_recruitment_keyword("春招补招")
        assert _is_recruitment_keyword("秋招补招")
        assert _is_recruitment_keyword("春招补录")
        assert _is_recruitment_keyword("秋招补录")


# ---------------------------------------------------------------------------
# 5-8：组合分隔符
# ---------------------------------------------------------------------------


class TestComboSeparators:
    def test_combo_slash(self):
        """5. 使用 / 的合法组合类型。"""
        assert _is_recruitment_keyword("日常实习/秋招全职")

    def test_combo_english_comma(self):
        """6. 使用英文逗号的合法组合类型。"""
        assert _is_recruitment_keyword("日常实习,秋招全职")

    def test_combo_chinese_comma(self):
        """7. 使用中文逗号的合法组合类型。"""
        assert _is_recruitment_keyword("暑期实习，秋招提前批")

    def test_combo_dunhao(self):
        """8. 使用顿号的合法组合类型。"""
        assert _is_recruitment_keyword("秋招全职、日常实习")

    def test_combo_with_spaces_around_parts(self):
        """切分后各部分去除首尾空格。"""
        assert _is_recruitment_keyword("日常实习 / 秋招全职")
        assert _is_recruitment_keyword("日常实习 ， 秋招提前批")

    def test_combo_with_supplement_type(self):
        """补招类型也可参与组合。"""
        assert _is_recruitment_keyword("春招补招/日常实习")


# ---------------------------------------------------------------------------
# 9-11：边界拒绝
# ---------------------------------------------------------------------------


class TestRecruitmentBoundaries:
    def test_combo_with_illegal_part(self):
        """9. 组合中有一个非法部分 → 不是招聘类型。"""
        assert not _is_recruitment_keyword("日常实习/任意文本")
        assert not _is_recruitment_keyword("春招补招/应届毕业生")
        # 端到端：F=组合含非法部分 + G=届次 → unknown
        row = {"E": "示例科技公司", "F": "日常实习/任意文本", "G": "2026届"}
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "unknown"
        assert rec["layout"] == LAYOUT_UNKNOWN

    def test_description_with_buzhao_not_recruitment(self):
        """10. 普通职位描述中含"补招"不得视为招聘类型（精确匹配）。"""
        assert not _is_recruitment_keyword("示例公司补招计划")
        assert not _is_recruitment_keyword("春招补招计划")
        assert not _is_recruitment_keyword("负责补招工作")
        # 端到端：F=含"补招"的普通描述 → other → unknown
        row = {"E": "示例科技公司", "F": "示例公司补招计划", "G": "2026届"}
        rec = detect_mainland(row, 2)
        assert rec["record_type"] == "unknown"
        assert rec.get("detection_reason") == "unrecognized_recruitment_type"

    def test_url_date_education_cohort_rejected(self):
        """11. URL、日期、学历、届次不得视为招聘类型。"""
        assert not _is_recruitment_keyword("https://example.com/apply")
        assert not _is_recruitment_keyword("2026-09-30")
        assert not _is_recruitment_keyword("2026/09/30")
        assert not _is_recruitment_keyword("本科")
        assert not _is_recruitment_keyword("本科及以上")
        assert not _is_recruitment_keyword("2026届")
        # 多届次含 "/" 分隔符，仍必须是 False
        assert not _is_recruitment_keyword("2026/2027届")

    def test_arbitrary_text_rejected(self):
        """任意非空文本不得当作招聘类型（不放宽白名单）。"""
        assert not _is_recruitment_keyword("任意非空文本")
        assert not _is_recruitment_keyword("招")


# ---------------------------------------------------------------------------
# 12：job 优先级不破坏
# ---------------------------------------------------------------------------


class TestJobPriorityPreserved:
    HEADER_287_STYLE = {
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

    def test_explicit_job_title_beats_recruitment_and_cohort(self):
        """12. 表头明确 company_name + job_title 时仍优先 job。"""
        row = {
            "A": "2026-08-01",
            "B": "互联网",
            "C": "示例科技公司",
            "D": "示例职位",
            "E": "负责示例业务",
            "F": "春招补招",
            "G": "2026/2027届",
            "H": "本科及以上",
            "I": "2026-09-30",
            "J": "https://example.com/jobs/123",
        }
        rec = detect_mainland(row, 2, headers=self.HEADER_287_STYLE)
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        assert rec["job_title"] == "示例职位"
        assert rec["recruitment_type"] == "春招补招"
        assert rec["target_cohort"] == "2026/2027届"

    def test_fixed_layout_job_unchanged(self):
        """固定列 job 布局不受补招关键词影响。"""
        rec = detect_mainland(
            {"F": "示例后端开发工程师", "G": "上海市"}, 3
        )
        assert rec["record_type"] == "job"
        assert rec["layout"] == LAYOUT_MAINLAND_JOB

    def test_supplement_type_is_invalid_job_title(self):
        """"春招补招"不能充当职位名称（旧模板 campaign 不被抢判 job）。"""
        from services.layout_detector import _is_valid_job_title_value

        assert not _is_valid_job_title_value("春招补招")
        assert not _is_valid_job_title_value("秋招补录")


# ---------------------------------------------------------------------------
# 13-14：unknown 原因汇总
# ---------------------------------------------------------------------------


def _unknown(reason: str | None) -> dict:
    rec: dict = {"record_type": "unknown", "layout": LAYOUT_UNKNOWN}
    if reason:
        rec["detection_reason"] = reason
    return rec


class TestSummarizeUnknownReasons:
    def test_counts_correct(self):
        """13. 汇总数量正确。"""
        records = [
            _unknown("unrecognized_recruitment_type"),
            _unknown("unrecognized_recruitment_type"),
            _unknown("unrecognized_recruitment_type"),
            _unknown("missing_required_source_fields"),
            _unknown("missing_required_source_fields"),
            _unknown("ambiguous_source_headers"),
            _unknown(None),
        ]
        summary = summarize_unknown_reasons(records)
        by_reason = {item["detection_reason"]: item["count"] for item in summary}
        assert by_reason["unrecognized_recruitment_type"] == 3
        assert by_reason["missing_required_source_fields"] == 2
        assert by_reason["ambiguous_source_headers"] == 1
        assert by_reason["unspecified"] == 1
        # 排序：count 降序
        counts = [item["count"] for item in summary]
        assert counts == sorted(counts, reverse=True)

    def test_categories_closed_and_total_matches(self):
        """14. 汇总四类闭合且总数等于 unknown 总数。"""
        records = [
            _unknown("unrecognized_recruitment_type"),
            _unknown("unrecognized_cohort"),
            _unknown("unrecognized_location"),
            _unknown("missing_required_source_fields"),
            _unknown("missing_required_job_values"),
            _unknown("missing_required_headers"),
            _unknown("incomplete_layout_signature"),
            _unknown("incomplete_header_signature"),
            _unknown("ambiguous_source_headers"),
            _unknown("conflicting_header_mapping"),
            _unknown(None),
            _unknown("some_future_reason"),
            {"record_type": "job"},  # 非 unknown 不参与
        ]
        summary = summarize_unknown_reasons(records)
        total_unknown = sum(
            1 for r in records if r.get("record_type") == "unknown"
        )
        assert sum(item["count"] for item in summary) == total_unknown
        # 四类闭合
        valid_categories = set(REASON_CATEGORY_DISPLAY)
        for item in summary:
            assert item["category"] in valid_categories
            assert item["category_display"] == (
                REASON_CATEGORY_DISPLAY[item["category"]]
            )
            assert item["reason_display"]  # 中文原因非空
        categories_seen = {item["category"] for item in summary}
        assert categories_seen == valid_categories

    def test_empty_and_none_input(self):
        assert summarize_unknown_reasons([]) == []
        assert summarize_unknown_reasons(None) == []

    def test_no_sensitive_data_in_summary(self):
        """汇总结果不携带公司名称、链接或 raw_data。"""
        sensitive_unknown = {
            "record_type": "unknown",
            "layout": LAYOUT_UNKNOWN,
            "detection_reason": "unrecognized_recruitment_type",
            "display_title": "示例显示公司",
            "company_name": "示例显示公司",
            "raw_data": {"E": "示例显示公司", "N": "https://example.com/apply"},
        }
        summary = summarize_unknown_reasons([sensitive_unknown])
        assert len(summary) == 1
        item = summary[0]
        allowed_keys = {
            "detection_reason",
            "reason_display",
            "category",
            "category_display",
            "count",
        }
        assert set(item) == allowed_keys
        text = str(item)
        assert "示例显示公司" not in text
        assert "example.com" not in text
        assert "raw_data" not in text


# ---------------------------------------------------------------------------
# 15：页面汇总面板（源码静态校验，现有测试同模式）
# ---------------------------------------------------------------------------


class TestPageSummaryPanel:
    def _page_source(self) -> str:
        return (PROJECT_ROOT / "pages" / "import_page.py").read_text(
            encoding="utf-8"
        )

    def _extract_function_source(self, source: str, func_name: str) -> str:
        """使用 ast 提取指定函数的源码（安全处理文件末尾函数）。"""
        tree = ast.parse(source, filename=str(PROJECT_ROOT / "pages" / "import_page.py"))
        lines = source.splitlines(keepends=True)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                start = node.lineno - 1
                end = node.end_lineno  # 切片右开
                return "".join(lines[start:end])
        raise AssertionError(f"未找到函数：{func_name}")

    def _strip_docstring(self, body: str) -> str:
        """移除函数体中 docstring 行（避免注释性说明触发敏感字段断言）。"""
        stripped_lines: list[str] = []
        in_doc = False
        triple_quote = ""
        for line in body.splitlines(keepends=True):
            if not in_doc:
                stripped = line.lstrip()
                # 单行或多行 docstring 起始（支持 """ 或 '''）
                for q in ('"""', "'''"):
                    if stripped.startswith(q):
                        if stripped.count(q) == 2 or (
                            stripped.endswith(q) and stripped != q
                        ):
                            in_doc = False
                            triple_quote = ""
                            break
                        in_doc = True
                        triple_quote = q
                        break
                else:
                    stripped_lines.append(line)
                    continue
                continue
            if in_doc and triple_quote in line:
                in_doc = False
                triple_quote = ""
        return "".join(stripped_lines)

    def test_expander_exists(self):
        """页面 unknown 区域含"查看待确认原因汇总"折叠面板。"""
        source = self._page_source()
        assert "查看待确认原因汇总" in source
        assert "summarize_unknown_reasons" in source

    def test_summary_builder_has_no_sensitive_fields(self):
        """汇总构建函数不引用 raw_data、公司或链接字段（排除 docstring 说明）。"""
        source = self._page_source()
        body = self._strip_docstring(
            self._extract_function_source(source, "_build_unknown_reason_summary_df")
        )
        for forbidden in (
            "raw_data",
            "company_name",
            "application_url",
            "announcement_url",
            "display_title",
            "job_title",
        ):
            assert forbidden not in body, forbidden

    def test_summary_only_reason_and_count(self):
        """面板只显示原因与数量（列集合静态校验）。"""
        source = self._page_source()
        body = self._extract_function_source(source, "_build_unknown_reason_summary_df")
        for col in ("原因代码", "原因", "归类", "数量"):
            assert col in body


# ---------------------------------------------------------------------------
# 17：不读取或引用 data/private
# ---------------------------------------------------------------------------


class TestNoPrivateDataAccess:
    def test_no_data_private_reference(self):
        sources = [
            PROJECT_ROOT / "services" / "layout_detector.py",
            PROJECT_ROOT / "pages" / "import_page.py",
        ]
        for path in sources:
            source = path.read_text(encoding="utf-8")
            assert "data/private" not in source, path
            assert "智联" not in source, path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
