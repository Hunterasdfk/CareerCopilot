"""任务 11A：中国大陆布局识别误判修复测试。

测试数据完全虚构（示例科技A / 示例公司B / example.com），
不访问网络，不读取 data/private。

覆盖：
- 届次识别扩展（多届次格式）；
- 招聘类型识别扩展（新关键词 + 组合类型）；
- 地区识别扩展（全国/全国多地/海外/远程等）；
- 中国大陆 job 多字段签名判定；
- unknown detection_reason 字段；
- unknown 预览优先级修复（L>E>F，不显示 C 列"民企"）；
- detection_reason 中文显示。
"""

from __future__ import annotations

from services.layout_detector import (
    LAYOUT_MAINLAND_CAMPAIGN,
    LAYOUT_MAINLAND_JOB,
    LAYOUT_UNKNOWN,
    _is_cohort,
    _is_recruitment_keyword,
    detect_mainland,
    get_detection_reason_display,
    is_city,
)


# ---------------------------------------------------------------------------
# 一、届次识别扩展
# ---------------------------------------------------------------------------


class TestCohortExtended:
    def test_single_cohort(self):
        """单届次：2027届、2026 届。"""
        assert _is_cohort("2027届")
        assert _is_cohort("2026 届")

    def test_multi_cohort_slash(self):
        """2026/2027届：多年份用 / 分隔，最后一个有"届"。"""
        assert _is_cohort("2026/2027届")

    def test_multi_cohort_three_years(self):
        """2024/2025/2026届：多年份用 / 分隔。"""
        assert _is_cohort("2024/2025/2026届")

    def test_multi_cohort_jie_slash(self):
        """2026届/2027届：每个"届"用 / 分隔。"""
        assert _is_cohort("2026届/2027届")

    def test_multi_cohort_comma(self):
        """2026届,2027届：逗号分隔。"""
        assert _is_cohort("2026届,2027届")

    def test_multi_cohort_dunhao(self):
        """2026届、2027届：顿号分隔。"""
        assert _is_cohort("2026届、2027届")

    def test_multi_cohort_dash(self):
        """2026-2027届：短横线分隔。"""
        assert _is_cohort("2026-2027届")

    def test_reject_date(self):
        """必须继续拒绝日期。"""
        assert not _is_cohort("2026-09-30")
        assert not _is_cohort("2026/09/30")
        assert not _is_cohort("2026年09月30日")

    def test_reject_url(self):
        """必须继续拒绝 URL。"""
        assert not _is_cohort("https://example.com")

    def test_reject_plain_description(self):
        """必须继续拒绝普通描述。"""
        assert not _is_cohort("普通描述")
        assert not _is_cohort("2026")  # 纯数字无"届"
        assert not _is_cohort("应届毕业生")

    def test_reject_empty(self):
        assert not _is_cohort(None)
        assert not _is_cohort("")


# ---------------------------------------------------------------------------
# 二、招聘类型识别扩展
# ---------------------------------------------------------------------------


class TestRecruitmentKeywordExtended:
    def test_new_keywords(self):
        """新增关键词：秋招提前批、暑假实习、社招全职。"""
        assert _is_recruitment_keyword("秋招提前批")
        assert _is_recruitment_keyword("暑假实习")
        assert _is_recruitment_keyword("社招全职")

    def test_combo_type_slash(self):
        """组合类型：先切分，各部分都在白名单中才视为招聘类型。"""
        assert _is_recruitment_keyword("日常实习/暑期实习")
        assert _is_recruitment_keyword("秋招全职/日常实习")
        assert _is_recruitment_keyword("春招全职/日常实习")
        assert _is_recruitment_keyword("春招全职/社招全职")

    def test_reject_arbitrary_text(self):
        """不得把任意非空文本都当作招聘类型。"""
        assert not _is_recruitment_keyword("任意非空文本")
        assert not _is_recruitment_keyword("秋招全职/任意文本")

    def test_reject_date_url_cohort_education(self):
        """拒绝日期、URL、届次、学历。"""
        assert not _is_recruitment_keyword("2026-09-30")
        assert not _is_recruitment_keyword("https://example.com")
        assert not _is_recruitment_keyword("2026届")
        assert not _is_recruitment_keyword("本科")


# ---------------------------------------------------------------------------
# 三、地区识别扩展
# ---------------------------------------------------------------------------


class TestLocationExtended:
    def test_special_regions(self):
        """特殊地区表达：全国、全国多地、多地、海外、远程。"""
        assert is_city("全国")
        assert is_city("全国多地")
        assert is_city("多地")
        assert is_city("海外")
        assert is_city("远程")

    def test_multi_city_with_overseas(self):
        """多城市字符串中包含"海外"时不应导致验证失败。"""
        assert is_city("北京,上海,海外")
        assert is_city("北京/上海/海外")
        assert is_city("北京、海外")

    def test_reject_date_url(self):
        """继续拒绝日期、URL。"""
        assert not is_city("2026-09-30")
        assert not is_city("https://example.com")

    def test_reject_cohort_education(self):
        """继续拒绝届次、学历。"""
        assert not is_city("2026届")
        assert not is_city("本科")

    def test_reject_plain_description(self):
        """继续拒绝普通描述。"""
        assert not is_city("可议")
        assert not is_city("普通描述")


# ---------------------------------------------------------------------------
# 四、detect_mainland：campaign 判定（多届次格式）
# ---------------------------------------------------------------------------


class TestMainlandCampaignExtended:
    def test_cohort_slash(self):
        """秋招全职 + 2026/2027届 → campaign。"""
        row = {
            "E": "示例科技A",
            "F": "秋招全职",
            "G": "2026/2027届",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "campaign"
        assert result["layout"] == LAYOUT_MAINLAND_CAMPAIGN

    def test_cohort_three_years(self):
        """春招全职 + 2024/2025/2026届 → campaign。"""
        row = {
            "E": "示例公司B",
            "F": "春招全职",
            "G": "2024/2025/2026届",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "campaign"

    def test_new_keyword_early_batch(self):
        """秋招提前批 + 2027届 → campaign。"""
        row = {
            "E": "示例科技A",
            "F": "秋招提前批",
            "G": "2027届",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "campaign"

    def test_combo_recruitment_type(self):
        """组合招聘类型 + 届次 → campaign。"""
        row = {
            "E": "示例公司B",
            "F": "秋招全职/日常实习",
            "G": "2026届",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "campaign"

    def test_summer_intern_keyword(self):
        """暑假实习 + 2026届/2027届 → campaign。"""
        row = {
            "E": "示例科技A",
            "F": "暑假实习",
            "G": "2026届/2027届",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "campaign"


# ---------------------------------------------------------------------------
# 五、detect_mainland：job 多字段签名判定
# ---------------------------------------------------------------------------


class TestMainlandJobSignature:
    def test_job_with_signature_nationwide(self):
        """虚构岗位 + 全国多地 + H/I/J 标准签名 → job。"""
        row = {
            "E": "示例科技A",
            "F": "商务拓展",
            "G": "全国多地",
            "H": "秋招全职",
            "I": "2026届",
            "J": "本科",
            "M": "https://example.com/announce",
            "N": "https://example.com/apply",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "job"
        assert result["layout"] == LAYOUT_MAINLAND_JOB

    def test_job_with_signature_overseas(self):
        """虚构岗位 + 北京,上海,海外 + H/I/J 标准签名 → job。"""
        row = {
            "E": "示例公司B",
            "F": "市场策划",
            "G": "北京,上海,海外",
            "H": "春招全职",
            "I": "2027届",
            "J": "硕士",
            "M": "https://example.com/announce",
            "N": "https://example.com/apply",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "job"

    def test_job_without_engineer_keyword(self):
        """不含"工程师"关键词但具有完整 H/I/J 签名 → job。"""
        row = {
            "E": "示例科技A",
            "F": "企划",
            "G": "北京",
            "H": "秋招全职",
            "I": "2026届",
            "J": "本科",
            "N": "https://example.com/apply",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "job"

    def test_job_with_remote_location(self):
        """远程 + H/I/J 签名 → job。"""
        row = {
            "E": "示例公司B",
            "F": "商务拓展",
            "G": "远程",
            "H": "日常实习",
            "I": "2026届",
            "J": "本科",
            "N": "https://example.com/apply",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "job"

    def test_job_signature_missing_h(self):
        """缺少 H（招聘类型）→ 不是多字段签名 job → unknown。"""
        row = {
            "E": "示例科技A",
            "F": "商务拓展",
            "G": "北京",
            "I": "2026届",
            "J": "本科",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "unknown"

    def test_no_job_with_only_f_g_non_empty(self):
        """不得仅凭 F、G 两个普通非空字段强行判 job。"""
        row = {
            "E": "示例科技A",
            "F": "商务拓展",
            "G": "北京",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "unknown"


# ---------------------------------------------------------------------------
# 六、detect_mainland：unknown 判定与 detection_reason
# ---------------------------------------------------------------------------


class TestMainlandUnknownExtended:
    def test_plain_description_unknown(self):
        """普通描述且缺少完整布局签名 → unknown。"""
        row = {
            "E": "示例科技A",
            "F": "综合招聘信息",
            "G": "北京",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "unknown"
        assert result["layout"] == LAYOUT_UNKNOWN

    def test_empty_f_g_unknown(self):
        """F/G 为空 → unknown。"""
        row = {"E": "示例科技A", "F": "", "G": ""}
        result = detect_mainland(row, 2)
        assert result["record_type"] == "unknown"

    def test_unknown_has_detection_reason(self):
        """unknown 记录有 detection_reason 字段。"""
        row = {"E": "示例科技A", "F": "综合招聘信息", "G": "北京"}
        result = detect_mainland(row, 2)
        assert "detection_reason" in result
        assert result["detection_reason"]  # 非空

    def test_unknown_missing_fields_reason(self):
        """缺少必填源字段 → missing_required_source_fields。"""
        row = {"E": "", "F": "", "G": ""}
        result = detect_mainland(row, 2)
        assert result.get("detection_reason") == "missing_required_source_fields"

    def test_unknown_recruitment_type_reason(self):
        """F 为 other → unrecognized_recruitment_type。"""
        row = {"E": "示例科技A", "F": "综合招聘信息", "G": "北京"}
        result = detect_mainland(row, 2)
        assert result.get("detection_reason") == "unrecognized_recruitment_type"

    def test_unknown_cohort_reason(self):
        """F=kw 但 G 不是届次 → unrecognized_cohort。"""
        row = {"E": "示例科技A", "F": "秋招全职", "G": "北京"}
        result = detect_mainland(row, 2)
        assert result.get("detection_reason") == "unrecognized_cohort"


# ---------------------------------------------------------------------------
# 七、unknown 预览优先级修复
# ---------------------------------------------------------------------------


class TestUnknownPreviewPriority:
    def test_mainland_unknown_not_show_c_column(self):
        """中国大陆 unknown 预览不得把"民企"作为企业标题。"""
        row = {
            "C": "民企",
            "E": "示例科技A",
            "F": "综合招聘信息",
            "G": "北京",
        }
        result = detect_mainland(row, 2)
        assert result["record_type"] == "unknown"
        assert "民企" not in result["display_title"]

    def test_mainland_unknown_priority_l_column(self):
        """中国大陆 unknown 预览优先 L 列公告标题。"""
        row = {
            "C": "民企",
            "E": "示例科技A",
            "F": "综合招聘信息",
            "G": "北京",
            "L": "示例公告标题",
        }
        result = detect_mainland(row, 2)
        assert result["display_title"] == "示例公告标题"

    def test_mainland_unknown_priority_e_column(self):
        """无 L 列时优先 E 列企业名称。"""
        row = {
            "C": "民企",
            "E": "示例科技A",
            "F": "综合招聘信息",
            "G": "北京",
        }
        result = detect_mainland(row, 2)
        assert result["display_title"] == "示例科技A"

    def test_mainland_unknown_priority_f_column(self):
        """无 L/E 时用 F 列。"""
        row = {
            "C": "民企",
            "E": "",
            "F": "秋招全职",
            "G": "北京",
        }
        result = detect_mainland(row, 2)
        assert result["display_title"] == "秋招全职"


# ---------------------------------------------------------------------------
# 八、detection_reason 中文显示
# ---------------------------------------------------------------------------


class TestDetectionReasonDisplay:
    def test_display_mapping(self):
        """detection_reason 正确映射为中文。"""
        assert (
            get_detection_reason_display("unrecognized_cohort")
            == "届次格式无法识别"
        )
        assert (
            get_detection_reason_display("unrecognized_recruitment_type")
            == "招聘类型无法识别"
        )
        assert (
            get_detection_reason_display("unrecognized_location")
            == "工作地点无法识别"
        )
        assert (
            get_detection_reason_display("incomplete_layout_signature")
            == "布局签名不完整"
        )
        assert (
            get_detection_reason_display("missing_required_source_fields")
            == "缺少必填源字段"
        )

    def test_display_none(self):
        """None 或空字符串返回空。"""
        assert get_detection_reason_display(None) == ""
        assert get_detection_reason_display("") == ""

    def test_display_unknown_reason(self):
        """未知原因原样返回。"""
        assert get_detection_reason_display("some_new_reason") == "some_new_reason"
