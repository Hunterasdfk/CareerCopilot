"""任务 11A.3：真实数据识别规则补全测试。

覆盖任务 11A.3 要求的全部场景：
1. 届次格式及反例（不限届/无限制/届届/混合/尾部逗号）；
2. 招聘类型原子值、组合值及描述正文反例；
3. 地点格式及 URL/日期/学历反例；
4. "暂无说明"不破坏表头可靠性；
5. 长岗位列表可以识别；
6. "详见附件"不能生成 job_title；
7. 非法 URL、邮箱文本、非法日期不阻塞分类；
8. 列顺序重排后结果保持一致；
9. 旧固定布局和任务 11A/11A.1/11A.2 测试保持通过；
10. 不允许在生产代码中出现上述源行号或公司名；
11. 不读取或引用 data/private。

测试数据**完全虚构**（示例科技A / 示例制造B / example.com），
不访问网络，不读取 data/private。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.layout_detector import (
    LAYOUT_MAINLAND_CAMPAIGN,
    LAYOUT_MAINLAND_HEADER_CAMPAIGN,
    LAYOUT_MAINLAND_HEADER_JOB,
    LAYOUT_MAINLAND_JOB,
    _is_cohort,
    _is_recruitment_keyword,
    _is_valid_job_title_value,
    _is_valid_location_value,
    _is_placeholder,
    detect_mainland,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 中国大陆表头映射（任务 11A.3 真实布局：F=岗位名称, G=工作地点, H=招聘类型,
# I=目标届次, J=学历要求）
# ---------------------------------------------------------------------------

MAINLAND_HEADERS_STANDARD: dict[str, str | None] = {
    "A": None,
    "B": None,
    "C": None,
    "D": "行业",
    "E": "企业名称",
    "F": "岗位名称",
    "G": "工作城市",
    "H": "招聘类型",
    "I": "招聘对象（届次）",
    "J": "学历要求",
    "K": "截止时间",
    "L": "公告名称",
    "M": "公告链接",
    "N": "投递链接",
}


def _make_mainland_row(
    company: str = "示例科技A",
    job_title: str | None = None,
    location: str = "北京",
    recruitment_type: str = "秋招全职",
    cohort: str = "2026届",
    education: str = "本科及以上",
    announcement: str | None = None,
    announcement_url: str | None = None,
    application_url: str | None = None,
    deadline: str | None = None,
    industry: str = "互联网",
) -> dict[str, str | None]:
    """构造中国大陆行数据（标准表头布局）。"""
    return {
        "A": None, "B": None, "C": None,
        "D": industry,
        "E": company,
        "F": job_title or "",
        "G": location,
        "H": recruitment_type,
        "I": cohort,
        "J": education,
        "K": deadline or "",
        "L": announcement or "",
        "M": announcement_url or "",
        "N": application_url or "",
    }


# ---------------------------------------------------------------------------
# 1. 届次格式
# ---------------------------------------------------------------------------


class TestCohortFormats:
    """届次合法格式与反例。"""

    @pytest.mark.parametrize("value,expected", [
        # 基本格式
        ("2026届", True),
        ("2027届", True),
        ("2026 届", True),
        # 多届次
        ("2026/2027届", True),
        ("2024/2025/2026届", True),
        ("2026届/2027届", True),
        ("2026届,2027届", True),
        ("2026届、2027届", True),
        ("2026-2027届", True),
        # 任务 11A.3 新增
        ("不限届", True),
        ("无限制", True),
        ("2026/不限届", True),
        ("2025/2026/不限届", True),
        ("2027/2028届届", True),
        ("2026/2027届届", True),
        ("2026届,", True),       # 尾部逗号
        ("2026届，", True),      # 尾部中文逗号
        ("2026届 ", True),       # 尾部空格
    ])
    def test_cohort_valid(self, value, expected):
        assert _is_cohort(value) is expected

    @pytest.mark.parametrize("value", [
        "https://example.com",
        "2026-09-30",
        "本科",
        "秋招全职",
        "负责产品设计",
        "2026",           # 纯年份无届
        "2026/2027",      # 多年份无届
        "",
        None,
    ])
    def test_cohort_invalid(self, value):
        assert _is_cohort(value) is False


# ---------------------------------------------------------------------------
# 2. 招聘类型
# ---------------------------------------------------------------------------


class TestRecruitmentTypes:
    """招聘类型原子值、组合值及反例。"""

    @pytest.mark.parametrize("value", [
        "秋招全职",
        "春招全职",
        "校招全职",
        "社招全职",
        "社招",
        "日常实习",
        "暑期实习",
        "寒假实习",
        "实习",
        "春招补录全职",
        "秋招全职补录",
        "秋招全职补招",
        "春招专场",
        "春招补招",
        "秋招补录",
    ])
    def test_atomic_recruitment_type(self, value):
        assert _is_recruitment_keyword(value)

    @pytest.mark.parametrize("value,expected", [
        # 分隔符 /
        ("日常实习/秋招全职", True),
        # 英文逗号
        ("日常实习,秋招全职", True),
        # 中文逗号
        ("日常实习，秋招全职", True),
        # 顿号
        ("秋招全职、日常实习", True),
        # 分号
        ("暑期实习;秋招提前批", True),
        # 中文分号
        ("暑期实习；秋招提前批", True),
        # 分隔符附近空格
        ("日常实习 / 秋招全职", True),
        # 多组合
        ("日常实习/秋招全职/暑期实习", True),
    ])
    def test_combo_recruitment_type(self, value, expected):
        assert _is_recruitment_keyword(value) is expected

    @pytest.mark.parametrize("value", [
        "https://example.com",
        "2026-09-30",
        "本科",
        "2026届",
        "负责产品设计",
        "示例公司补招计划",      # 描述中含"补招"
        "春招补招计划",          # 含原子类型 + 额外文字
        "负责补招工作",
        "日常实习/秋招补招计划",  # 组合中有一个非法部分
        "",
        None,
    ])
    def test_recruitment_type_invalid(self, value):
        assert _is_recruitment_keyword(value) is False


# ---------------------------------------------------------------------------
# 3. 工作地点
# ---------------------------------------------------------------------------


class TestLocationFormats:
    """地点格式及反例。"""

    @pytest.mark.parametrize("value", [
        "全国",
        "全国多地",
        "海外",
        "全球",
        "远程",
        "北京",
        "上海",
        "北京市",
        "广东省",
        "北京上海苏州宁波广州深圳",   # 无分隔符城市串
        "北京/上海/苏州",
        "北京,上海,苏州",
        "北京，上海",
        "北京、上海",
        "北京;上海",
        "北京 上海",
    ])
    def test_location_valid(self, value):
        assert _is_valid_location_value(value)

    @pytest.mark.parametrize("value", [
        "https://example.com",
        "2026-09-30",
        "本科",
        "2026届",
        "秋招全职",
        "负责产品设计",
        "",
        None,
    ])
    def test_location_invalid(self, value):
        assert _is_valid_location_value(value) is False


# ---------------------------------------------------------------------------
# 4. 占位值不破坏表头可靠性
# ---------------------------------------------------------------------------


class TestPlaceholderValues:
    """占位值（暂无说明/不限等）不阻塞分类。"""

    @pytest.mark.parametrize("value", [
        "暂无说明",
        "未说明",
        "不限",
        "空值",
        "无",
    ])
    def test_is_placeholder(self, value):
        assert _is_placeholder(value)

    def test_placeholder_not_education(self):
        """占位值不是合法学历，但不应阻塞表头可靠性。"""
        assert not _is_placeholder("本科及以上")

    def test_education_placeholder_does_not_break_header(self):
        """学历为占位值时，表头仍可靠，记录可分类。"""
        row = _make_mainland_row(
            job_title="示例前端工程师",
            education="暂无说明",
        )
        result = detect_mainland(row, 100, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        assert result.get("education_requirement") is None

    def test_cohort_placeholder_does_not_break_header(self):
        """届次为占位值时，表头仍可靠，记录可分类。"""
        row = _make_mainland_row(
            job_title="示例后端工程师",
            cohort="不限",
        )
        result = detect_mainland(row, 101, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result.get("target_cohort") is None


# ---------------------------------------------------------------------------
# 5. 长岗位列表可以识别
# ---------------------------------------------------------------------------


class TestLongJobTitleList:
    """长岗位名称集合应识别为有效 job_title。"""

    def test_long_job_title_list(self):
        title = "前端工程师/后端工程师/算法工程师/数据分析师/产品经理/测试工程师"
        assert _is_valid_job_title_value(title)

    def test_job_categories_with_separators(self):
        title = "研发类、产品类、设计类、运营类"
        assert _is_valid_job_title_value(title)

    def test_job_title_with_parentheses(self):
        title = "前端工程师（北京）"
        assert _is_valid_job_title_value(title)

    def test_job_title_with_colon(self):
        title = "技术类：前端开发工程师"
        assert _is_valid_job_title_value(title)

    def test_long_job_title_list_classified_as_job(self):
        row = _make_mainland_row(
            job_title="前端工程师/后端工程师/算法工程师/数据分析师",
        )
        result = detect_mainland(row, 102, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result["layout"] == LAYOUT_MAINLAND_HEADER_JOB


# ---------------------------------------------------------------------------
# 6. "详见附件"不能生成 job_title
# ---------------------------------------------------------------------------


class TestAttachmentReference:
    """"详见附件"类值不能作为 job_title，应判 campaign。"""

    @pytest.mark.parametrize("value", [
        "具体岗位详见附件",
        "招聘岗位信息详见附件",
        "岗位信息详见附件",
        "职位信息详见附件",
        "详见招聘附件",
    ])
    def test_attachment_reference_not_job_title(self, value):
        assert not _is_valid_job_title_value(value)

    def test_attachment_reference_classified_as_campaign(self):
        """有招聘类型 + 届次 + 公告名称时，附件引用 job_title → campaign。"""
        row = _make_mainland_row(
            job_title="具体岗位详见附件",
            announcement="示例公司2026届校招公告",
        )
        result = detect_mainland(row, 103, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "campaign"
        assert result["layout"] == LAYOUT_MAINLAND_HEADER_CAMPAIGN


# ---------------------------------------------------------------------------
# 7. 非法 URL、邮箱文本、非法日期不阻塞分类
# ---------------------------------------------------------------------------


class TestInvalidFieldsNoBlock:
    """无效字段值不阻塞分类，保留在 raw_data。"""

    def test_application_url_is_header_name(self):
        """application_url 值为"投递链接"（表头名误入数据）→ 标准字段置空。"""
        row = _make_mainland_row(
            job_title="示例前端工程师",
            application_url="投递链接",
        )
        result = detect_mainland(row, 104, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result.get("application_url") is None
        assert result["raw_data"]["N"] == "投递链接"

    def test_application_url_is_email_text(self):
        """application_url 值为"投递邮箱：xxx@example.com"→ 标准字段置空。"""
        row = _make_mainland_row(
            job_title="示例后端工程师",
            application_url="投递邮箱：test@example.com",
        )
        result = detect_mainland(row, 105, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result.get("application_url") is None

    def test_invalid_deadline_does_not_block(self):
        """deadline 值为非法日期"20260-3-30"→ 标准字段置空，记录仍分类。"""
        row = _make_mainland_row(
            job_title="示例算法工程师",
            deadline="20260-3-30",
        )
        result = detect_mainland(row, 106, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result.get("deadline") is None
        assert result["raw_data"]["K"] == "20260-3-30"

    def test_invalid_url_does_not_block(self):
        """application_url 值为非 URL 文本 → 标准字段置空，记录仍分类。"""
        row = _make_mainland_row(
            job_title="示例测试工程师",
            application_url="不是链接",
        )
        result = detect_mainland(row, 107, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "job"
        assert result.get("application_url") is None


# ---------------------------------------------------------------------------
# 8. 列顺序重排后结果保持一致
# ---------------------------------------------------------------------------


class TestColumnRearrangement:
    """表头驱动下，列顺序变化不影响分类结果。"""

    # 重排列顺序：E=企业名称, H=招聘类型, F=岗位名称, J=学历, I=届次, G=工作城市
    HEADERS_REARRANGED: dict[str, str | None] = {
        "A": None, "B": None, "C": None,
        "D": "行业",
        "E": "企业名称",
        "F": "招聘类型",       # F 和 H 交换
        "G": "工作城市",
        "H": "岗位名称",       # H 和 F 交换
        "I": "学历要求",       # I 和 J 交换
        "J": "招聘对象（届次）", # J 和 I 交换
        "K": "截止时间",
        "L": "公告名称",
        "M": "公告链接",
        "N": "投递链接",
    }

    def test_rearranged_columns_job(self):
        """列重排后，有效 job 仍判 job。"""
        row = {
            "A": None, "B": None, "C": None,
            "D": "互联网",
            "E": "示例科技A",
            "F": "秋招全职",       # 招聘类型（原 H 列内容）
            "G": "北京",
            "H": "示例前端工程师",  # 岗位名称（原 F 列内容）
            "I": "本科及以上",      # 学历（原 J 列内容）
            "J": "2026届",         # 届次（原 I 列内容）
            "K": "",
            "L": "",
            "M": "",
            "N": "",
        }
        result = detect_mainland(row, 108, headers=self.HEADERS_REARRANGED)
        assert result["record_type"] == "job"
        assert result["layout"] == LAYOUT_MAINLAND_HEADER_JOB
        assert result.get("job_title") == "示例前端工程师"
        assert result.get("recruitment_type") == "秋招全职"
        assert result.get("target_cohort") == "2026届"
        assert result.get("education_requirement") == "本科及以上"

    def test_rearranged_columns_campaign(self):
        """列重排后，campaign 仍判 campaign。"""
        row = {
            "A": None, "B": None, "C": None,
            "D": "金融",
            "E": "示例银行C",
            "F": "春招补招",         # 招聘类型（招聘类型 header 在 F 列）
            "G": "全国",
            "H": "",                 # 无岗位名称（岗位名称 header 在 H 列）
            "I": "本科及以上",        # 学历（学历要求 header 在 I 列）
            "J": "2026/2027届",      # 届次（招聘对象 header 在 J 列）
            "K": "",
            "L": "示例银行2026春招补录公告",
            "M": "https://example.com/announcement",
            "N": "https://example.com/apply",
        }
        result = detect_mainland(row, 109, headers=self.HEADERS_REARRANGED)
        assert result["record_type"] == "campaign"
        assert result.get("recruitment_type") == "春招补招"
        assert result.get("target_cohort") == "2026/2027届"


# ---------------------------------------------------------------------------
# 9. 中国大陆第 287 类回归（任务 11A.2 保持兼容）
# ---------------------------------------------------------------------------


class TestMainlandRow287Compatible:
    """任务 11A.2 场景在 11A.3 修改后仍通过。"""

    def test_chun_zhao_bu_zhao_campaign(self):
        """春招补招 + 2026/2027届 → campaign。"""
        row = _make_mainland_row(
            company="示例显示公司",
            job_title="",
            location="成都",
            recruitment_type="春招补招",
            cohort="2026/2027届",
            education="本科及以上",
            announcement="示例公司2026届校招补录计划",
            announcement_url="https://example.com/announcement",
            application_url="https://example.com/apply",
        )
        result = detect_mainland(row, 287, headers=MAINLAND_HEADERS_STANDARD)
        assert result["record_type"] == "campaign"
        assert result["recruitment_type"] == "春招补招"
        assert result["target_cohort"] == "2026/2027届"


# ---------------------------------------------------------------------------
# 10. 固定列回退仍正常工作（无表头时）
# ---------------------------------------------------------------------------


class TestFixedColumnFallback:
    """无表头时固定列规则仍正常工作。"""

    def test_fixed_column_campaign(self):
        """F=招聘类型 + G=届次 → campaign（固定列）。"""
        row = {
            "A": None, "B": None, "C": None, "D": "互联网",
            "E": "示例科技A",
            "F": "秋招全职",
            "G": "2026届",
            "H": "本科及以上",
            "I": "研发类",
            "J": "北京",
            "K": "", "L": "", "M": "", "N": "",
        }
        result = detect_mainland(row, 200, headers=None)
        assert result["record_type"] == "campaign"
        assert result["layout"] == LAYOUT_MAINLAND_CAMPAIGN

    def test_fixed_column_job(self):
        """F=岗位名称(含工程师) + G=城市 → job（固定列）。"""
        row = {
            "A": None, "B": None, "C": None, "D": "互联网",
            "E": "示例科技A",
            "F": "前端工程师",
            "G": "北京",
            "H": "秋招全职",
            "I": "2026届",
            "J": "本科及以上",
            "K": "", "L": "", "M": "", "N": "",
        }
        result = detect_mainland(row, 201, headers=None)
        assert result["record_type"] == "job"
        assert result["layout"] == LAYOUT_MAINLAND_JOB


# ---------------------------------------------------------------------------
# 11. 生产代码不含源行号或真实公司名
# ---------------------------------------------------------------------------


class TestNoHardcodedSourceData:
    """生产代码中不得硬编码真实行号或公司名。"""

    def _layout_source(self) -> str:
        return (PROJECT_ROOT / "services" / "layout_detector.py").read_text(
            encoding="utf-8"
        )

    def test_no_real_row_numbers(self):
        """生产代码不得含 3481 或 3840 等真实行号硬编码。"""
        source = self._layout_source()
        assert "3481" not in source
        assert "3840" not in source

    def test_no_real_company_names(self):
        """生产代码不得含真实公司名（示例为虚构）。"""
        source = self._layout_source()
        # 真实公司名不会出现在代码中；只允许虚构示例
        assert "智联" not in source


# ---------------------------------------------------------------------------
# 12. 不读取或引用 data/private
# ---------------------------------------------------------------------------


class TestNoPrivateDataAccess:
    """测试不读取或引用 data/private。"""

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
