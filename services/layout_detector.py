"""布局识别器（任务 4）。

按工作表选择解析策略，逐行判定 `record_type` 与布局标签
（docs/ARCHITECTURE.md §4、docs/SOURCE_SCHEMA.md §3-§5、docs/WORKBOOK_PROFILE.md §3-§3.5）。

核心原则（docs/SOURCE_SCHEMA.md §5）：
- 凡不能可靠判定的，一律标 `record_type=unknown` 并保留 `raw_data`，
  在导入预览由人工确认，**不强行推断**。
- 每个工作表使用各自独立的解析策略，不得因列数相同而混用
  （如中国香港与新加坡不同构，见 §3.5.1 / §3.5.3）。

判定规则（强约束，已实证）：
- **中国大陆**：F=招聘类型关键词 & G=届次 → campaign（新版）；
  F=具体岗位名称（`_is_job_title_like` 判定）& G=城市 → job（旧版）；
  F=other（如"其他类别/综合招聘信息/招聘公告"等非岗位名称）→ unknown；
  其余 → unknown。不得把所有非招聘关键词的非空值都判为 job。
- **中国香港**：F=招聘类别 & G=届次 → 候选标准布局；
  F=学历 & G=岗位描述 → 候选错位布局；其余 → unknown。
  **候选布局只是签名判定，最终 record_type 与字段映射需人工复核**，
  故一律以 `record_type=unknown` + `needs_confirmation=True` + 暂定
  `suggested_record_type` / `suggested_fields` 输出，顶层不写最终业务字段。
  `suggested_record_type` 保守判定：D 通过 `_is_job_title_like()` → job；
  D 为空 → campaign；D 非空但不像岗位名称 → None（不提供最终建议）。
- **美国**：F=届次 & G=学历 → 候选标准布局；
  F=学历 & G=届次 → 候选交换布局（F↔G 互换映射）；其余 → unknown。
  同样以 unknown + 建议字段输出，需人工复核。H 列保守判定同香港 D 列。
- **英国 / 新加坡**：F 列稳定（招聘类型），使用独立规则
  `_is_uk_sg_recruitment_type`（非空且非日期/URL/届次/学历即可），
  **不复用大陆 `_RECRUITMENT_KEYWORDS` 白名单**，支持 Graduate Programme
  等文本；G 为届次时正常映射，G 歧义子集 → unknown。
  F=本科或 F=2026届 等明显错位值 → unknown。
- **低年级项目-全球版**：F 含明确岗位关键词（工程师/分析师/实习生/开发/测试）
  → job；含项目入口关键词（项目/计划/训练营/培养计划）→ campaign；
  其余 → unknown。
- **低年级项目-公司官网**：项目入口型，多为 campaign；F 空 → unknown。

本模块只做分类与字段映射建议，不向数据库写入，不访问网络。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 工作表名常量与注册
# ---------------------------------------------------------------------------

SHEET_MAINLAND = "中国大陆"
SHEET_HK = "中国香港"
SHEET_US = "美国"
SHEET_UK = "英国"
SHEET_SG = "新加坡"
SHEET_JUNIOR_GLOBAL = "低年级项目-全球版"
SHEET_JUNIOR_OFFICIAL = "低年级项目-美国&香港-公司官网"

# 支持的工作表集合（其他工作表名返回 unsupported）
SUPPORTED_SHEETS: frozenset[str] = frozenset(
    {
        SHEET_MAINLAND,
        SHEET_HK,
        SHEET_US,
        SHEET_UK,
        SHEET_SG,
        SHEET_JUNIOR_GLOBAL,
        SHEET_JUNIOR_OFFICIAL,
    }
)

# ---------------------------------------------------------------------------
# 取值分类器（基于 docs/WORKBOOK_PROFILE.md §3 / §3.5 的实证语义）
# ---------------------------------------------------------------------------

# 中国大陆 campaign 布局的招聘类型关键词（§3.1 / §8，任务 11A/11A.2 扩展）。
# 所有原子类型集中在本白名单，判断函数不得散落硬编码。
_RECRUITMENT_KEYWORDS: frozenset[str] = frozenset(
    {
        "秋招全职",
        "春招全职",
        "日常实习",
        "暑期实习",
        "秋招实习",
        "春招实习",
        "实习生招聘",
        "秋招提前批",
        "暑假实习",
        "社招全职",
        # 任务 11A.2：补招/补录类
        "春招补招",
        "秋招补招",
        "春招补录",
        "秋招补录",
        "秋招",
        "春招",
    }
)

# 届次正则：如 2026届、2027届、2026 届
_COHORT_PATTERN = re.compile(r"^\s*\d{4}\s*届\s*$")

# 多届次分隔符：/、-、,、，、
_COHORT_SEPARATORS_PATTERN = re.compile(r"[/\-，,、]")

# 单个届次部分：4 位年份 + 可选"届"（如 2026、2026届）
_COHORT_PART_PATTERN = re.compile(r"^\d{4}\s*届?$")
# 学历关键词（§3.5）
_EDUCATION_KEYWORDS: frozenset[str] = frozenset(
    {"大专", "本科", "硕士", "博士", "学历不限", "本科及以上", "硕士及以上"}
)

# 明确岗位名称特征关键词（修复点 1，新增 _is_job_title_like）
# 用于区分大陆 F=job 与 F=other，以及香港/美国候选布局的保守 suggested_record_type
_JOB_TITLE_KEYWORDS: tuple[str, ...] = (
    "工程师", "开发", "测试", "算法", "研发", "分析师", "实习生",
    "管培生", "经理", "专员", "助理", "顾问", "运营", "设计",
    "产品", "研究员", "销售", "财务", "法务", "审计", "供应链",
    "采购", "人力资源",
)

# 描述性前缀（以这些开头的取值通常是岗位描述/职责说明，而非岗位名称）
_DESCRIPTION_PREFIXES: tuple[str, ...] = (
    "负责", "要求", "需要", "岗位描述", "工作内容", "职责",
    "描述", "说明", "任职", "条件", "主要", "参与", "协助",
)

# ---------------------------------------------------------------------------
# 严格城市/地区判定（修复点 2）
# ---------------------------------------------------------------------------

# 知名城市/地区词（不含"市/省"后缀也能识别）
_KNOWN_REGIONS: frozenset[str] = frozenset(
    {
        # 直辖市
        "北京", "上海", "天津", "重庆",
        # 省会与计划单列
        "广州", "深圳", "杭州", "成都", "南京", "武汉", "西安", "苏州",
        "长沙", "青岛", "大连", "宁波", "厦门", "福州", "济南", "合肥",
        "郑州", "南昌", "太原", "南宁", "海口", "贵阳", "昆明", "兰州",
        "沈阳", "长春", "哈尔滨", "石家庄", "呼和浩特", "银川", "西宁",
        "乌鲁木齐", "拉萨",
        # 特别行政区
        "香港", "澳门",
        # 海外常见
        "纽约", "伦敦", "东京", "新加坡", "波士顿", "底特律", "硅谷",
    }
)

# 特殊地区表达（任务 11A 新增）
_SPECIAL_REGIONS: frozenset[str] = frozenset(
    {
        "全国",
        "全国多地",
        "多地",
        "海外",
        "远程",
    }
)

# 合理地区后缀（地名以这些结尾视为可识别地区）
_REGION_SUFFIXES: tuple[str, ...] = (
    "市", "省", "自治区", "特别行政区",
    "区", "县", "旗",
    "州",  # 如 "加利福尼亚州"
    "地区",  # 如 "华东地区"
    "新区", "开发区",
)

# 多地分隔符（按这些切分后每段都须可识别）
_LOCATION_SEPARATORS_PATTERN = re.compile(r"[/\-、,，;；\s]+")


def _is_cohort(value: object) -> bool:
    """判断是否为届次取值（任务 11A 扩展）。

    支持格式：
    - 单届次：2027届、2026 届
    - 多届次（分隔符 / - , ， 、）：
      - 2026/2027届
      - 2024/2025/2026届
      - 2026届/2027届
      - 2026届,2027届
      - 2026届、2027届
      - 2026-2027届

    必须继续拒绝日期、URL 和普通描述。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    # 先拒绝日期和 URL
    if _is_date(text) or _is_url(text):
        return False
    # 单届次
    if _COHORT_PATTERN.match(text):
        return True
    # 多届次：按分隔符切分，每个部分都必须是 4 位年份（+可选"届"），
    # 且整体必须包含至少一个"届"字
    parts = _COHORT_SEPARATORS_PATTERN.split(text)
    if len(parts) > 1:
        if "届" not in text:
            return False
        for part in parts:
            part = part.strip()
            if not part or not _COHORT_PART_PATTERN.match(part):
                return False
        return True
    return False


# 组合招聘类型的分隔符（任务 11A.2）：/ , ， 、
_RECRUITMENT_COMBO_SEPARATORS = re.compile(r"[/,，、]")


def _is_recruitment_keyword(value: object) -> bool:
    """判断是否为招聘类型关键词（任务 11A/11A.2）。

    规则：
    - 原子类型必须命中 ``_RECRUITMENT_KEYWORDS`` 白名单（精确匹配）；
    - 组合类型（如"日常实习/秋招全职"）按分隔符 / , ， 、 切分，
      每部分去除首尾空格，全部非空部分都命中白名单才返回 True；
    - 有分隔符但任一部分非法 → False；
    - 日期、URL、学历、届次一律 False；
    - 不得使用"包含招/补/实习"等宽泛子字符串判断。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    # 先拒绝日期、URL、届次、学历
    if _is_date(text) or _is_url(text) or _is_cohort(text) or _is_education(text):
        return False
    # 单一关键词
    if text in _RECRUITMENT_KEYWORDS:
        return True
    # 组合类型：按分隔符切分，各部分都必须在白名单中
    if _RECRUITMENT_COMBO_SEPARATORS.search(text):
        parts = [
            p.strip() for p in _RECRUITMENT_COMBO_SEPARATORS.split(text)
        ]
        non_empty = [p for p in parts if p]
        if len(non_empty) > 1 and all(
            p in _RECRUITMENT_KEYWORDS for p in non_empty
        ):
            return True
        return False
    return False


def _is_education(value: object) -> bool:
    if value is None:
        return False
    return str(value).strip() in _EDUCATION_KEYWORDS


def _is_job_title_like(value: object) -> bool:
    """判断取值是否像具体岗位名称（修复点 1，新增）。

    保守规则：
    - 招聘类型关键词、届次、学历、URL、日期 → 不是岗位名称；
    - 以描述性前缀开头（如"负责…"）→ 视为描述，不是岗位名称；
    - 含明确岗位关键词（工程师/开发/分析师/实习生等）→ 视为岗位名称；
    - 其余非空文本 → 不像岗位名称（返回 False，交由 other 处理）。

    不得把"其他类别/综合招聘信息/招聘公告/多岗位/不限/可议"等判为 job。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    # 先拒绝明显非岗位取值
    if _is_recruitment_keyword(text) or _is_cohort(text) or _is_education(text):
        return False
    if _is_url(text) or _is_date(text):
        return False
    # 拒绝描述性前缀（如"负责产品设计与开发"是描述，不是岗位名称）
    if any(text.startswith(prefix) for prefix in _DESCRIPTION_PREFIXES):
        return False
    # 含岗位关键词 → 岗位名称
    return any(kw in text for kw in _JOB_TITLE_KEYWORDS)


# 日期模式：2026/09/30、2026-09-30、2026.09.30（用于拒绝把日期误判为城市）
_DATE_PATTERN = re.compile(
    r"^\s*\d{4}[/\-年]\d{1,2}[/\-月]\d{1,2}日?\s*$"
)
# URL 模式（http/https/file/ftp 等）
_URL_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _is_url(value: object) -> bool:
    if value is None:
        return False
    return bool(_URL_PATTERN.match(str(value).strip()))


def _is_date(value: object) -> bool:
    if value is None:
        return False
    return bool(_DATE_PATTERN.match(str(value).strip()))


def _is_single_region(text: str) -> bool:
    """判断单个非空字符串是否为可识别地区。

    规则（任务 11A 扩展）：
    - 特殊地区表达（全国/全国多地/多地/海外/远程）直接识别；
    - 知名地区词直接识别；
    - 以合理地区后缀结尾视为可识别；
    - 其余（含 URL、日期、届次、学历、普通描述）一律拒绝。
    """
    if not text:
        return False
    # 先拒绝 URL/日期/届次/学历（即使含地区后缀也不许混入）
    if _is_url(text) or _is_date(text) or _is_cohort(text) or _is_education(text):
        return False
    if text in _SPECIAL_REGIONS:
        return True
    if text in _KNOWN_REGIONS:
        return True
    if any(text.endswith(suffix) for suffix in _REGION_SUFFIXES):
        return True
    return False


def is_city(value: object) -> bool:
    """判断取值是否为城市/地区。

    规则（修复点 2，严格）：
    - 多地值按 / - 、 , 等切分后，**每个非空部分**都必须是可识别地区；
    - 明确拒绝 URL、日期、届次、学历、普通描述（如"可议"）；
    - 宁可标 unknown，也不得误判 job。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    # 单段直接判定
    if not _LOCATION_SEPARATORS_PATTERN.search(text):
        return _is_single_region(text)
    # 多段：每段都必须可识别
    parts = _LOCATION_SEPARATORS_PATTERN.split(text)
    parts = [p for p in parts if p]  # 去掉空段
    if not parts:
        return False
    return all(_is_single_region(p) for p in parts)


# 向后兼容的别名（旧测试可能引用）
_is_city = is_city


def _classify_mainland_f(value: object) -> str:
    """中国大陆 F 列语义分类：kw / job / other / empty（修复点 1）。

    不得把所有非招聘关键词的非空值都判为 job：
    - 明确招聘类型关键词 → kw；
    - 明确岗位名称特征（_is_job_title_like）→ job；
    - 其他非空文本 → other（如"其他类别/综合招聘信息/招聘公告"等）；
    - 空值 → empty。

    确保真实画像中的 F=other + G=city 不会直接变成 job。
    """
    if value is None or str(value).strip() == "":
        return "empty"
    if _is_recruitment_keyword(value):
        return "kw"
    if _is_job_title_like(value):
        return "job"
    return "other"


def _classify_mainland_g(value: object) -> str:
    """中国大陆 G 列语义分类：cohort / city / other / empty。"""
    if value is None or str(value).strip() == "":
        return "empty"
    if _is_cohort(value):
        return "cohort"
    if is_city(value):
        return "city"
    return "other"


def _classify_hk_us_f(value: object) -> str:
    """香港/美国 F 列语义分类：recruitment_type / education / cohort / other / empty。"""
    if value is None or str(value).strip() == "":
        return "empty"
    if _is_recruitment_keyword(value):
        return "recruitment_type"
    if _is_education(value):
        return "education"
    if _is_cohort(value):
        return "cohort"
    return "other"


def _classify_hk_us_g(value: object) -> str:
    """香港/美国 G 列语义分类：cohort / education / other / empty。"""
    if value is None or str(value).strip() == "":
        return "empty"
    if _is_cohort(value):
        return "cohort"
    if _is_education(value):
        return "education"
    return "other"


def _classify_uk_sg_g(value: object) -> str:
    """英国/新加坡 G 列语义分类：cohort / education / other / empty。"""
    return _classify_hk_us_g(value)


def _is_uk_sg_recruitment_type(value: object) -> bool:
    """英国/新加坡 F 列招聘类型判定（修复点 2，独立规则）。

    英国/新加坡 F 列已实证为稳定招聘类型列，不复用大陆关键词白名单：
    - 非空且不是日期、URL、届次或学历 → 可视为招聘类型字段；
    - 不要求必须出现在大陆 _RECRUITMENT_KEYWORDS 中；
    - 支持 Graduate Programme 等招聘类型文本；
    - F=本科或 F=2026届 等明显错位值 → 返回 False（标 unknown）。

    不得改变中国大陆 campaign 使用的严格关键词规则。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if _is_date(text) or _is_url(text) or _is_cohort(text) or _is_education(text):
        return False
    return True


def _suggest_record_type_from_title(title_value: object) -> str | None:
    """根据岗位名称列取值保守建议 record_type（修复点 3）。

    - 通过 _is_job_title_like() → "job"；
    - 为空 → "campaign"；
    - 非空但属于招聘类型、届次、学历、描述或无法判断 → None（不提供最终建议）。

    record_type 始终保持 unknown，此值仅作为 suggested_record_type 建议。
    """
    if title_value is None or str(title_value).strip() == "":
        return "campaign"
    if _is_job_title_like(title_value):
        return "job"
    return None


# ---------------------------------------------------------------------------
# 表头解析（任务 11A.1：表头驱动识别）
# ---------------------------------------------------------------------------

# 表头别名表：标准字段 → 别名列表（精确匹配规范化后的表头，不做子字符串匹配）
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "企业名称", "公司名称", "公司", "雇主", "employer", "company",
    ),
    "job_title": (
        "岗位名称", "招聘岗位", "职位名称", "职位", "岗位",
        "job title", "position",
    ),
    "job_description": (
        "职位简介", "岗位描述", "职位描述", "工作内容", "jd", "description",
    ),
    "industry": ("行业", "所属行业", "行业类别", "industry"),
    "recruitment_type": (
        "招聘类别", "招聘类型", "招聘性质", "用工类型", "employment type",
    ),
    "target_cohort": (
        "招聘对象（届次）", "招聘对象", "目标届次", "招聘届次", "届次",
        "target cohort",
    ),
    "education_requirement": (
        "学历要求", "学历", "education", "education requirement",
    ),
    "location": (
        "工作城市", "工作地点", "职位地点", "地点", "城市", "location",
    ),
    "deadline": (
        "截止时间", "截止日期", "投递截止", "申请截止", "deadline",
    ),
    "announcement_title": ("公告名称", "公告标题", "招聘公告"),
    "announcement_url": ("公告链接", "公告地址"),
    "application_url": (
        "投递链接", "申请链接", "职位链接", "岗位链接", "application url",
    ),
    "generic_url": ("链接", "网址", "url"),
}

# 连续空白（含换行、Tab、全角空格等）
_HEADER_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_header(value: object) -> str:
    """规范化表头文本（任务 11A.1）。

    处理：
    - 前后空格、换行和连续空白（统一删除）；
    - 英文大小写（casefold）；
    - 全角/半角括号与标点（NFKC）；
    - 空表头（None / 空串 → ""）。

    返回规范化文本；空表头返回 ""。
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.casefold()
    text = _HEADER_WHITESPACE_PATTERN.sub("", text)
    return text.strip()


def _build_alias_lookup() -> dict[str, tuple[str, ...]]:
    """构建 规范化别名 → 标准字段元组 的查找表。"""
    lookup: dict[str, list[str]] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            key = normalize_header(alias)
            if not key:
                continue
            lookup.setdefault(key, [])
            if field_name not in lookup[key]:
                lookup[key].append(field_name)
    return {k: tuple(v) for k, v in lookup.items()}


# 预计算别名查找表（模块级常量，不可变）
_ALIAS_LOOKUP: dict[str, tuple[str, ...]] = _build_alias_lookup()


@dataclass
class HeaderMapping:
    """表头映射结果（任务 11A.1，可独立测试）。

    Attributes:
        field_to_col: 标准字段 → 列字母。
        col_to_field: 列字母 → 标准字段。
        normalized_headers: 列字母 → 规范化表头（仅非空表头）。
        duplicates: 参与重复/同字段冲突的列字母列表（这些列不参与映射）。
        alias_conflict: 是否存在单个表头命中多个标准字段的别名冲突。
    """

    field_to_col: dict[str, str] = field(default_factory=dict)
    col_to_field: dict[str, str] = field(default_factory=dict)
    normalized_headers: dict[str, str] = field(default_factory=dict)
    duplicates: tuple[str, ...] = ()
    alias_conflict: bool = False

    @property
    def conflicts(self) -> bool:
        """表头是否存在重复或冲突（冲突时不得静默选择第一个）。"""
        return bool(self.duplicates) or self.alias_conflict

    @property
    def has_mapping(self) -> bool:
        """是否至少有一个表头成功映射到标准字段。"""
        return bool(self.field_to_col)


def resolve_header_mapping(headers: Mapping[object, object] | None) -> HeaderMapping:
    """把 列字母 → 原始表头 映射解析为 HeaderMapping（任务 11A.1）。

    规则：
    - 空表头（None / 规范化后为空）不参与映射；
    - 重复表头（多个列规范化后相同）：这些列全部不参与映射，
      不得静默选择第一个，记录到 duplicates；
    - 一个表头命中多个标准字段（别名冲突）：该列不参与映射，
      记录 alias_conflict；
    - 多个列命中同一标准字段：这些列同样视为重复冲突（ambiguous）。
    """
    normalized: dict[str, str] = {}
    for col, text in (headers or {}).items():
        norm = normalize_header(text)
        if norm:
            normalized[str(col)] = norm

    # 重复表头检测：相同规范化文本出现多次 → 全部不映射
    seen: dict[str, str] = {}
    duplicate_cols: list[str] = []
    for col, norm in normalized.items():
        if norm in seen:
            if seen[norm] not in duplicate_cols:
                duplicate_cols.append(seen[norm])
            duplicate_cols.append(col)
        else:
            seen[norm] = col
    duplicate_set = set(duplicate_cols)

    field_to_col: dict[str, str] = {}
    col_to_field: dict[str, str] = {}
    alias_conflict = False

    for col, norm in normalized.items():
        if col in duplicate_set:
            continue
        fields = _ALIAS_LOOKUP.get(norm)
        if not fields:
            continue
        if len(fields) > 1:
            # 单个表头命中多个标准字段（别名表冲突），不得静默选择
            alias_conflict = True
            continue
        field_name = fields[0]
        if field_name in field_to_col:
            # 多个不同表头列命中同一标准字段 → 歧义，全部不映射
            prev_col = field_to_col.pop(field_name)
            col_to_field.pop(prev_col, None)
            if prev_col not in duplicate_cols:
                duplicate_cols.append(prev_col)
            duplicate_cols.append(col)
            duplicate_set.add(prev_col)
            duplicate_set.add(col)
            continue
        field_to_col[field_name] = col
        col_to_field[col] = field_name

    return HeaderMapping(
        field_to_col=field_to_col,
        col_to_field=col_to_field,
        normalized_headers=normalized,
        duplicates=tuple(duplicate_cols),
        alias_conflict=alias_conflict,
    )


def _is_valid_job_title_value(value: object) -> bool:
    """判断 job_title 列的取值是否为有效职位名称（任务 11A.1）。

    与 _is_job_title_like 的区别：**不要求**包含"工程师/开发"等岗位关键词，
    只排除明显不是职位的取值：
    - 招聘类型关键词（如"秋招全职"——旧模板把招聘类型放在职位名称列）；
    - 届次、学历、URL、日期；
    - 描述性前缀（如"负责…"）。
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if _is_recruitment_keyword(text) or _is_cohort(text) or _is_education(text):
        return False
    if _is_url(text) or _is_date(text):
        return False
    if any(text.startswith(prefix) for prefix in _DESCRIPTION_PREFIXES):
        return False
    return True


# 表头可靠性检查：映射到这些语义受控字段的**非空值**必须通过语义校验，
# 否则视为表头与数据不一致（不可靠表头），回退固定列位置规则。
# 检查表 _HEADER_SEMANTIC_CHECKS 在 is_valid_url 定义之后构建。


def _check_header_value_reliability(
    row: Mapping, hm: HeaderMapping
) -> bool:
    """检查行值与表头语义是否一致（任务 11A.1）。

    只有全部语义受控字段的非空值都通过校验时，表头才被视为可靠；
    旧模板数据（表头存在但个别行数据错位）会在此失败并回退固定列规则。
    """
    for field_name, checker in _HEADER_SEMANTIC_CHECKS.items():
        col = hm.field_to_col.get(field_name)
        if col is None:
            continue
        value = row.get(col)
        if value is None or str(value).strip() == "":
            continue
        if not checker(value):
            return False
    return True


# job 分类支持字段：至少存在其一（映射存在且值非空）才可判 job
_JOB_SUPPORT_FIELDS: tuple[str, ...] = (
    "job_description",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "application_url",
    "generic_url",
)

# 表头驱动布局生成的标准字段（job / campaign 共用主体）
_HEADER_JOB_STD_FIELDS: tuple[str, ...] = (
    "company_name",
    "job_title",
    "industry",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "deadline",
    "announcement_title",
    "announcement_url",
    "application_url",
)
_HEADER_CAMPAIGN_STD_FIELDS: tuple[str, ...] = (
    "company_name",
    "industry",
    "recruitment_type",
    "target_cohort",
    "education_requirement",
    "location",
    "deadline",
    "announcement_title",
    "announcement_url",
    "application_url",
)


def _header_col_value_nonempty(row: Mapping, hm: HeaderMapping, field_name: str) -> bool:
    """字段有映射且对应行值非空。"""
    col = hm.field_to_col.get(field_name)
    if col is None:
        return False
    value = row.get(col)
    return value is not None and str(value).strip() != ""


def _build_header_mapping_dict(
    row: Mapping, hm: HeaderMapping, fields: tuple[str, ...]
) -> dict[str, str]:
    """按表头映射构建 列字母 → 标准字段 的 _apply_mapping 映射表。

    generic_url 特殊处理：仅当没有明确的 application_url 表头、
    且其值为有效 http/https URL 时，才作为 application_url。
    """
    mapping: dict[str, str] = {}
    for field_name in fields:
        col = hm.field_to_col.get(field_name)
        if col is not None:
            mapping[col] = field_name
    if "application_url" not in mapping:
        gcol = hm.field_to_col.get("generic_url")
        if gcol is not None and is_valid_url(row.get(gcol)):
            mapping[gcol] = "application_url"
    return mapping


def _classify_mainland_header_driven(
    row: Mapping, hm: HeaderMapping, source_row: int
) -> dict[str, Any] | None:
    """表头驱动分类（任务 11A.1）。返回 None 表示回退固定列规则。

    优先级：
    1. job：company_name + 有效 job_title 值 + 至少一个支持字段；
       - job_title 无需包含岗位关键词，但不得是招聘类型/届次/学历/URL/日期/描述；
       - "社招全职 + 2025/2026届" 不会把含明确职位名称的记录判为 campaign；
    2. campaign：公告标题非空，或 招聘类型 + 届次均非空 且无有效职位名称；
    3. 其余回退固定列规则（None）。
    """
    company_ok = _header_col_value_nonempty(row, hm, "company_name")
    title_col = hm.field_to_col.get("job_title")
    title_ok = _is_valid_job_title_value(row.get(title_col)) if title_col else False

    if title_ok:
        if not company_ok:
            # 职位名称明确但公司值为空：必要字段为空
            return _build_unknown_record(
                row, SHEET_MAINLAND, source_row,
                detection_reason="missing_required_job_values",
            )
        if not any(
            _header_col_value_nonempty(row, hm, f) for f in _JOB_SUPPORT_FIELDS
        ):
            # 有公司 + 职位但无任何支持字段：签名不完整
            return _build_unknown_record(
                row, SHEET_MAINLAND, source_row,
                detection_reason="incomplete_header_signature",
            )
        return _apply_mapping(
            row,
            _build_header_mapping_dict(row, hm, _HEADER_JOB_STD_FIELDS),
            "job",
            LAYOUT_MAINLAND_HEADER_JOB,
            SHEET_MAINLAND,
            source_row,
            job_title_required=True,
        )

    # job 不成立 → 尝试 campaign
    has_recruit = _header_col_value_nonempty(row, hm, "recruitment_type")
    has_cohort = _header_col_value_nonempty(row, hm, "target_cohort")
    has_announcement = _header_col_value_nonempty(row, hm, "announcement_title")
    if (has_recruit and has_cohort) or has_announcement:
        if _header_col_value_nonempty(row, hm, "company_name") or has_announcement:
            return _apply_mapping(
                row,
                _build_header_mapping_dict(row, hm, _HEADER_CAMPAIGN_STD_FIELDS),
                "campaign",
                LAYOUT_MAINLAND_HEADER_CAMPAIGN,
                SHEET_MAINLAND,
                source_row,
            )
    return None


# ---------------------------------------------------------------------------
# 工具：URL 校验与 raw_data 构造
# ---------------------------------------------------------------------------

_URL_SCHEME_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


def is_valid_url(value: object) -> bool:
    """判断是否为可打开的 http(s) URL（docs/WORKBOOK_PROFILE.md §2 "无效 URL"）。"""
    if value is None:
        return False
    return bool(_URL_SCHEME_PATTERN.match(str(value).strip()))


# 表头可靠性检查表（任务 11A.1）：映射到这些语义受控字段的非空值
# 必须通过语义校验，否则视为表头与数据不一致，回退固定列位置规则。
# URL 类字段不参与可靠性检查：URL 有效性只在字段生成时校验
# （无效 URL 不映射到标准字段），避免轻微链接问题导致整表回退。
# 在 is_valid_url 定义之后构建，避免模块加载顺序问题。
_HEADER_SEMANTIC_CHECKS: dict[str, Any] = {
    "recruitment_type": _is_recruitment_keyword,
    "target_cohort": _is_cohort,
    "education_requirement": _is_education,
    "location": is_city,
    "deadline": lambda v: _is_date(v)
    or bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", str(v).strip())),
}


def _build_raw_data(row: Mapping) -> dict[str, Any]:
    """构造 raw_data：保留整行原始单元格（键为列字母，值为原始文本）。

    列字母键便于回溯到源工作簿的列位（docs/ARCHITECTURE.md §7）。
    所有取值转 str，None 保留为 None。
    """
    return {col: (None if val is None else str(val)) for col, val in row.items()}


# ---------------------------------------------------------------------------
# 布局标签常量
# ---------------------------------------------------------------------------

# 中国大陆（实证已确认，可直接入库）
LAYOUT_MAINLAND_CAMPAIGN = "mainland_campaign_v2"
LAYOUT_MAINLAND_JOB = "mainland_job_v1"
# 中国大陆表头驱动布局（任务 11A.1，可直接入库）
LAYOUT_MAINLAND_HEADER_JOB = "header_mapped_job_v1"
LAYOUT_MAINLAND_HEADER_CAMPAIGN = "header_mapped_campaign_v1"
# 中国香港（候选，需人工复核）
LAYOUT_HK_STANDARD = "hk_standard_candidate"
LAYOUT_HK_SHIFTED = "hk_shifted_candidate"
# 美国（候选，需人工复核）
LAYOUT_US_STANDARD = "us_standard_candidate"
LAYOUT_US_SWAPPED = "us_swapped_candidate"
# 英国 / 新加坡 / 低年级（实证已确认，可直接入库）
LAYOUT_UK_DEFAULT = "uk_default"
LAYOUT_SG_DEFAULT = "sg_default"
LAYOUT_JUNIOR_GLOBAL = "junior_global"
LAYOUT_JUNIOR_OFFICIAL = "junior_official"
# 未知
LAYOUT_UNKNOWN = "unknown"

# 候选布局集合：香港 / 美国的"标准/交换"仅基于签名判定，最终需人工复核
_CANDIDATE_LAYOUTS: frozenset[str] = frozenset(
    {
        LAYOUT_HK_STANDARD,
        LAYOUT_HK_SHIFTED,
        LAYOUT_US_STANDARD,
        LAYOUT_US_SWAPPED,
    }
)

# ---------------------------------------------------------------------------
# 字段映射建议表（候选布局用 suggested_fields）
# ---------------------------------------------------------------------------

# 列字母 → 标准字段 的映射建议
# 直接入库布局（中国大陆、英国、新加坡、低年级）用 _apply_mapping 写顶层字段；
# 候选布局（香港/美国）只写 suggested_fields，顶层不写最终业务字段。

_HK_STANDARD_SUGGESTED: dict[str, str] = {
    "C": "company_name",
    "D": "job_title",
    "B": "industry",
    "F": "recruitment_type",
    "G": "target_cohort",
    "H": "education_requirement",
    "I": "deadline",
    "J": "application_url",
}

# 香港错位布局：F=学历、G=岗位描述、E 列承载届次（§3.5.1）
# 保守规则：只有符合届次规则的值才建议为 target_cohort；
# H 等语义漂移字段不得直接建议为 recruitment_type，保留在 raw_data。
_HK_SHIFTED_SUGGESTED: dict[str, str] = {
    "C": "company_name",
    "D": "job_title",
    "B": "industry",
    "F": "education_requirement",  # F=学历（已实证）
    "I": "deadline",
    "J": "application_url",
    # E 列：仅当取值符合届次规则时才建议为 target_cohort（运行时判定）
    # G 列岗位描述：不强行建议为某标准字段，保留在 raw_data
    # H 列语义漂移：不直接建议为 recruitment_type，保留在 raw_data
}

_US_STANDARD_SUGGESTED: dict[str, str] = {
    "D": "company_name",
    "H": "job_title",
    "B": "industry",
    "E": "recruitment_type",
    "F": "target_cohort",
    "G": "education_requirement",
    "J": "location",
    "K": "deadline",
    "L": "application_url",
}

# 美国交换布局：F↔G 互换后映射
_US_SWAPPED_SUGGESTED: dict[str, str] = {
    "D": "company_name",
    "H": "job_title",
    "B": "industry",
    "E": "recruitment_type",
    "G": "target_cohort",  # 交换后 G=届次
    "F": "education_requirement",  # 交换后 F=学历
    "J": "location",
    "K": "deadline",
    "L": "application_url",
}

# 直接入库布局的映射表
_MAINLAND_CAMPAIGN_MAP: dict[str, str] = _HK_STANDARD_SUGGESTED.__class__()  # 占位，下方重新定义
_MAINLAND_CAMPAIGN_MAP = {
    "E": "company_name",
    "F": "recruitment_type",
    "G": "target_cohort",
    "H": "education_requirement",
    "I": "job_categories",
    "J": "location",
    "K": "deadline",
    "L": "announcement_title",
    "M": "announcement_url",
    "N": "application_url",
    "D": "industry",
}

_MAINLAND_JOB_MAP: dict[str, str] = {
    "E": "company_name",
    "F": "job_title",
    "G": "location",
    "H": "recruitment_type",
    "I": "target_cohort",
    "J": "education_requirement",
    "K": "deadline",
    "L": "announcement_title",
    "M": "announcement_url",
    "N": "application_url",
    "D": "industry",
}

_UK_MAP: dict[str, str] = {
    "C": "company_name",
    "D": "job_title",
    "B": "industry",
    "F": "recruitment_type",
    "G": "target_cohort",
    "H": "education_requirement",
    "I": "location",
    "J": "deadline",
    "K": "application_url",
}

_SG_MAP: dict[str, str] = {
    # 新加坡与香港列位相同但不同构（§3.5.3）；G 歧义子集标 unknown
    "C": "company_name",
    "D": "job_title",
    "B": "industry",
    "F": "recruitment_type",
    "G": "target_cohort",
    "H": "education_requirement",
    "I": "deadline",
    "J": "application_url",
}

_JUNIOR_GLOBAL_MAP: dict[str, str] = {
    "D": "company_name",
    "F": "job_title",
    "C": "industry",
    "E": "recruitment_type",
    "I": "target_cohort",  # 毕业时间
    "G": "education_requirement",  # 学位
    "B": "location",
    "K": "deadline",
    "L": "application_url",
}

_JUNIOR_OFFICIAL_MAP: dict[str, str] = {
    "D": "company_name",
    "F": "display_title",  # 项目名称
    "B": "industry",
    "E": "recruitment_type",
    "G": "target_cohort",  # 适合年级
    "A": "location",
    "H": "application_url",
}

# 低年级全球版：F=职位/项目名称的关键词判定（修复点 4）
_JUNIOR_JOB_KEYWORDS: tuple[str, ...] = (
    "工程师", "分析师", "实习生", "开发", "测试", "研究员",
    "设计师", "产品经理", "运营", "算法", "架构师",
)
_JUNIOR_CAMPAIGN_KEYWORDS: tuple[str, ...] = (
    "项目", "计划", "训练营", "培养计划", "培养", "实习项目", "招聘项目",
)


def _junior_global_classify(f_value: object) -> str:
    """低年级-全球版 F 列分类：job / campaign / unknown。"""
    if f_value is None:
        return "unknown"
    text = str(f_value).strip()
    if not text:
        return "unknown"
    if any(kw in text for kw in _JUNIOR_JOB_KEYWORDS):
        return "job"
    if any(kw in text for kw in _JUNIOR_CAMPAIGN_KEYWORDS):
        return "campaign"
    return "unknown"


# ---------------------------------------------------------------------------
# 记录构造：可直接入库的布局
# ---------------------------------------------------------------------------


def _apply_mapping(
    row: Mapping,
    mapping: dict[str, str],
    record_type: str,
    layout: str,
    source_sheet: str,
    source_row: int,
    *,
    display_title_override: str | None = None,
    job_title_required: bool = False,
) -> dict[str, Any]:
    """按列字母→字段映射构造标准化 Opportunity 记录（可直接入库布局用）。

    Args:
        row: 原始行（键为列字母）。
        mapping: 列字母→标准字段名映射。
        record_type: 已确定的记录类型（campaign / job / unknown）。
        layout: 布局标签。
        source_sheet: 来源工作表名。
        source_row: 来源行号（1-based，对应原文件物理行号，含表头）。
        display_title_override: 若指定，覆盖默认 display_title 推导。
        job_title_required: 若 True（job 记录），job_title 必填；缺失则降级 unknown
            并清理顶层业务字段。
    """
    record: dict[str, Any] = {
        "record_type": record_type,
        "source_sheet": source_sheet,
        "source_row": source_row,
        "layout": layout,
        "raw_data": _build_raw_data(row),
    }

    for col_letter, field_name in mapping.items():
        if col_letter in row:
            record[field_name] = row[col_letter]

    # display_title 推导
    if display_title_override:
        record["display_title"] = display_title_override
    elif record_type == "campaign" and record.get("announcement_title"):
        record["display_title"] = record["announcement_title"]
    elif record_type == "job" and record.get("job_title"):
        record["display_title"] = record["job_title"]
    elif record.get("job_title"):
        record["display_title"] = record["job_title"]
    elif record.get("company_name"):
        # 兜底：用公司名作展示标题
        record["display_title"] = record["company_name"]
    else:
        record["display_title"] = ""

    # job 记录必须有 job_title；缺失则降级为 unknown（不强行推断）
    if job_title_required and not str(record.get("job_title", "")).strip():
        record = _demote_to_unknown(record, row, source_sheet, source_row)

    # URL 校验：无效 URL 不写入 application_url / announcement_url，保留在 raw_data
    for url_field in ("application_url", "announcement_url"):
        if url_field in record and not is_valid_url(record[url_field]):
            # 置空标准字段，原始值仍在 raw_data
            record[url_field] = None

    return record


def _demote_to_unknown(
    record: dict[str, Any],
    row: Mapping,
    source_sheet: str,
    source_row: int,
) -> dict[str, Any]:
    """把一条记录降级为 unknown：清理顶层业务字段，只保留 raw_data 与回溯字段。"""
    return {
        "record_type": "unknown",
        "source_sheet": source_sheet,
        "source_row": source_row,
        "layout": LAYOUT_UNKNOWN,
        "raw_data": _build_raw_data(row),
        "display_title": str(record.get("display_title") or "")[:200],
        "needs_confirmation": True,
        "suggested_record_type": record.get("record_type"),
    }


# ---------------------------------------------------------------------------
# 记录构造：候选布局（香港 / 美国）
# ---------------------------------------------------------------------------


def _build_candidate_record(
    row: Mapping,
    suggested_mapping: dict[str, str],
    suggested_record_type: str,
    layout: str,
    source_sheet: str,
    source_row: int,
    *,
    extra_suggested: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造候选布局记录（修复点 1、5）。

    候选布局（香港标准/错位、美国标准/交换）只是基于签名的候选判定，
    最终 record_type 与字段映射需人工复核，故：
    - `record_type` 一律为 `unknown`（compute_dedupe_key 对其返回 None）；
    - `needs_confirmation = True`；
    - `suggested_record_type` 给出暂定类型（campaign/job）；
    - `suggested_fields` 给出暂定字段映射（仅当取值符合该字段的语义规则时才纳入）；
    - `raw_data` 完整保留；
    - **顶层不写** company_name / job_title / target_cohort 等最终业务字段。
    """
    suggested_fields: dict[str, Any] = {}

    for col_letter, field_name in suggested_mapping.items():
        if col_letter not in row:
            continue
        raw_value = row[col_letter]
        if raw_value is None or str(raw_value).strip() == "":
            continue
        # 语义校验：只把符合字段语义的取值纳入建议
        if _value_matches_field(field_name, raw_value):
            suggested_fields[field_name] = raw_value

    if extra_suggested:
        for field_name, value in extra_suggested.items():
            if value is not None and str(value).strip() != "":
                if _value_matches_field(field_name, value):
                    suggested_fields[field_name] = value

    # display_title：候选记录不给最终 display_title，只用 raw_data 兜底
    display = str(row.get("C") or row.get("D") or row.get("E") or "")[:200]

    return {
        "record_type": "unknown",
        "source_sheet": source_sheet,
        "source_row": source_row,
        "layout": layout,
        "raw_data": _build_raw_data(row),
        "display_title": display,
        "needs_confirmation": True,
        "suggested_record_type": suggested_record_type,
        "suggested_fields": suggested_fields,
    }


def _value_matches_field(field_name: str, value: object) -> bool:
    """候选字段语义校验：只有符合字段语义的取值才纳入 suggested_fields。

    修复点 5：避免把"机械专业"等普通描述误建议为 target_cohort；
    H 等语义漂移字段不得直接建议为 recruitment_type。
    """
    if value is None or str(value).strip() == "":
        return False
    text = str(value).strip()

    if field_name in ("target_cohort",):
        # 只有符合届次规则的值才建议为 target_cohort
        return _is_cohort(text)
    if field_name in ("education_requirement",):
        return _is_education(text)
    if field_name in ("recruitment_type",):
        return _is_recruitment_keyword(text)
    if field_name in ("application_url", "announcement_url"):
        return is_valid_url(text)
    if field_name in ("location",):
        return is_city(text)
    if field_name in ("deadline",):
        # 日期或日期字符串均可
        return _is_date(text) or bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", text))
    # 其余字段（company_name / job_title / job_categories / industry /
    # display_title / recruitment_type 已单独处理）保守接受非空文本
    return True


# ---------------------------------------------------------------------------
# 各工作表判定函数
# ---------------------------------------------------------------------------

# detection_reason 中文显示映射（任务 11A；任务 11A.1 增补表头相关原因）
DETECTION_REASON_DISPLAY: dict[str, str] = {
    "unrecognized_cohort": "届次格式无法识别",
    "unrecognized_recruitment_type": "招聘类型无法识别",
    "unrecognized_location": "工作地点无法识别",
    "incomplete_layout_signature": "布局签名不完整",
    "missing_required_source_fields": "缺少必填源字段",
    # 任务 11A.1 表头相关原因
    "missing_required_headers": "缺少可靠的公司或职位表头",
    "ambiguous_source_headers": "表头重复或冲突，无法安全确定语义",
    "conflicting_header_mapping": "表头与多个字段冲突",
    "missing_required_job_values": "必要字段值为空",
    "incomplete_header_signature": "表头结构不完整，缺少支持字段",
}


def get_detection_reason_display(reason: str | None) -> str:
    """获取 detection_reason 的中文显示。"""
    if not reason:
        return ""
    return DETECTION_REASON_DISPLAY.get(reason, reason)


# unknown 原因的归类（任务 11A.2）：帮助用户判断剩余记录的性质。
# 四类闭合：规则疑似漏识别 / 缺少必要字段 / 表头冲突 / 需人工确认。
REASON_CATEGORY_DISPLAY: dict[str, str] = {
    "rule_gap": "规则疑似漏识别",
    "missing_fields": "缺少必要字段",
    "header_conflict": "表头冲突",
    "needs_review": "需人工确认",
}

# detection_reason → 归类 key
_REASON_CATEGORY_MAP: dict[str, str] = {
    "unrecognized_cohort": "rule_gap",
    "unrecognized_recruitment_type": "rule_gap",
    "unrecognized_location": "rule_gap",
    "missing_required_source_fields": "missing_fields",
    "missing_required_job_values": "missing_fields",
    "missing_required_headers": "missing_fields",
    "incomplete_layout_signature": "missing_fields",
    "incomplete_header_signature": "missing_fields",
    "ambiguous_source_headers": "header_conflict",
    "conflicting_header_mapping": "header_conflict",
}


def get_reason_category(reason: str | None) -> str:
    """获取 detection_reason 的归类 key（四类闭合，未知原因归 needs_review）。"""
    if not reason:
        return "needs_review"
    return _REASON_CATEGORY_MAP.get(reason, "needs_review")


def summarize_unknown_reasons(
    records: "Sequence[Mapping] | None",
) -> list[dict[str, Any]]:
    """按 ``detection_reason`` 汇总全部 unknown 记录（任务 11A.2，纯函数）。

    只输出原因代码、中文原因、归类和数量，**不携带**任何公司名称、
    职位描述、链接或 raw_data，供页面折叠面板安全展示。

    Args:
        records: 解析记录列表（opportunity_importer / detect_row 的输出）。

    Returns:
        按 count 降序、reason 代码升序排列的列表，每项::

            {
                "detection_reason": str,   # 原因代码（无 reason 记为 "unspecified"）
                "reason_display": str,     # 中文原因
                "category": str,           # 归类 key（四类闭合）
                "category_display": str,   # 归类中文
                "count": int,              # 数量
            }
    """
    counts: dict[str, int] = {}
    for rec in records or []:
        if not isinstance(rec, Mapping) or rec.get("record_type") != "unknown":
            continue
        reason = rec.get("detection_reason") or "unspecified"
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {
            "detection_reason": reason,
            "reason_display": get_detection_reason_display(reason),
            "category": get_reason_category(reason),
            "category_display": REASON_CATEGORY_DISPLAY[
                get_reason_category(reason)
            ],
            "count": count,
        }
        for reason, count in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]


def _has_mainland_job_signature(row: Mapping) -> bool:
    """中国大陆 job 多字段签名判定（任务 11A）。

    当 E、F、G 非空，且 H/I/J 分别符合招聘类型、届次、学历语义时，
    即使 F 不含"工程师"等关键词，或 G 含"全国多地/海外/非常见城市"，
    也可可靠判断为 mainland_job_v1。

    不得仅凭 F、G 两个普通非空字段强行判 job。
    """
    e_val = row.get("E")  # 公司名称
    f_val = row.get("F")  # 岗位名称或岗位类别
    g_val = row.get("G")  # 工作地点
    h_val = row.get("H")  # 招聘类型
    i_val = row.get("I")  # 目标届次
    j_val = row.get("J")  # 学历要求

    # E、F、G 必须非空
    if not e_val or not str(e_val).strip():
        return False
    if not f_val or not str(f_val).strip():
        return False
    if not g_val or not str(g_val).strip():
        return False
    # H/I/J 分别符合招聘类型、届次、学历语义
    if not _is_recruitment_keyword(h_val):
        return False
    if not _is_cohort(i_val):
        return False
    if not _is_education(j_val):
        return False
    return True


def _determine_mainland_unknown_reason(
    row: Mapping, f_cls: str, g_cls: str
) -> str:
    """确定中国大陆 unknown 的具体原因（任务 11A）。"""
    e_val = row.get("E")
    # 缺少必填源字段
    if e_val is None or str(e_val).strip() == "":
        return "missing_required_source_fields"
    if f_cls == "empty" or g_cls == "empty":
        return "missing_required_source_fields"
    # F 是招聘关键词但 G 不是届次
    if f_cls == "kw" and g_cls != "cohort":
        return "unrecognized_cohort"
    # F 是 other（非招聘关键词、非岗位名称）
    if f_cls == "other":
        return "unrecognized_recruitment_type"
    # G 是 other
    if g_cls == "other":
        return "unrecognized_location"
    # 其余情况
    return "incomplete_layout_signature"


def _detect_mainland_fixed(row: Mapping, source_row: int) -> dict[str, Any]:
    """中国大陆固定列位置判定（旧版布局回退，任务 11A 逻辑保持不变）。"""
    f_val = row.get("F")
    g_val = row.get("G")
    f_cls = _classify_mainland_f(f_val)
    g_cls = _classify_mainland_g(g_val)

    if f_cls == "kw" and g_cls == "cohort":
        # campaign 新版（实证已确认，可直接入库）
        return _apply_mapping(
            row,
            _MAINLAND_CAMPAIGN_MAP,
            "campaign",
            LAYOUT_MAINLAND_CAMPAIGN,
            SHEET_MAINLAND,
            source_row,
        )
    if f_cls == "job" and g_cls == "city":
        # job 旧版（实证已确认，可直接入库）
        return _apply_mapping(
            row,
            _MAINLAND_JOB_MAP,
            "job",
            LAYOUT_MAINLAND_JOB,
            SHEET_MAINLAND,
            source_row,
            job_title_required=True,
        )
    # job 多字段签名判定（任务 11A 新增）
    if _has_mainland_job_signature(row):
        return _apply_mapping(
            row,
            _MAINLAND_JOB_MAP,
            "job",
            LAYOUT_MAINLAND_JOB,
            SHEET_MAINLAND,
            source_row,
            job_title_required=True,
        )
    # 其余一律 unknown，附带 detection_reason
    reason = _determine_mainland_unknown_reason(row, f_cls, g_cls)
    return _build_unknown_record(
        row, SHEET_MAINLAND, source_row, detection_reason=reason
    )


def detect_mainland(
    row: Mapping, source_row: int, headers: Mapping[object, object] | None = None
) -> dict[str, Any]:
    """中国大陆逐行判定 campaign / job / unknown（§3.3，任务 11A.1）。

    判定顺序：
    1. **表头驱动优先**：传入表头且解析出可靠映射（无重复/冲突，
       且语义受控字段的非空值都通过语义校验）时，按表头语义分类，
       生成 header_mapped_job_v1 / header_mapped_campaign_v1；
       列顺序变化不影响结果；
    2. **固定列位置回退**：缺少表头、表头无别名命中、表头与数据
       语义不一致（旧模板错位）或表头驱动无法判定时，沿用任务 11A
       的固定列规则（mainland_campaign_v2 / mainland_job_v1）；
    3. 表头冲突时回退固定列；若最终 unknown，detection_reason 优先
       使用表头冲突原因（ambiguous_source_headers）。

    不使用任何全局可变状态；表头映射通过参数显式传入。
    """
    if headers:
        hm = resolve_header_mapping(headers)
        if hm.conflicts:
            # 表头重复/冲突 → 不得静默选择，回退固定列；
            # 固定列也判 unknown 时，用表头冲突原因
            result = _detect_mainland_fixed(row, source_row)
            if result["record_type"] == "unknown":
                if hm.duplicates:
                    result["detection_reason"] = "ambiguous_source_headers"
                elif hm.alias_conflict:
                    result["detection_reason"] = "conflicting_header_mapping"
            return result
        if hm.has_mapping and _check_header_value_reliability(row, hm):
            driven = _classify_mainland_header_driven(row, hm, source_row)
            if driven is not None:
                return driven
            # 表头可靠但分类未成立 → 回退固定列；unknown 时若缺
            # company_name / job_title 表头，用表头视角原因
            result = _detect_mainland_fixed(row, source_row)
            if result["record_type"] == "unknown":
                if (
                    "company_name" not in hm.field_to_col
                    or "job_title" not in hm.field_to_col
                ):
                    result["detection_reason"] = "missing_required_headers"
            return result
        # 表头与数据语义不一致（旧模板错位）或无别名命中 → 固定列
    return _detect_mainland_fixed(row, source_row)


def detect_hk(row: Mapping, source_row: int) -> dict[str, Any]:
    """中国香港标准/错位候选布局检测（§3.5.1）。

    候选布局只输出 unknown + 建议字段，需人工复核（修复点 1、5）。
    """
    f_val = row.get("F")
    g_val = row.get("G")
    f_cls = _classify_hk_us_f(f_val)
    g_cls = _classify_hk_us_g(g_val)

    # 候选标准布局：F=招聘类别 & G=届次
    if f_cls == "recruitment_type" and g_cls == "cohort":
        suggested_type = _suggest_record_type_from_title(row.get("D"))
        return _build_candidate_record(
            row,
            _HK_STANDARD_SUGGESTED,
            suggested_type,
            LAYOUT_HK_STANDARD,
            SHEET_HK,
            source_row,
        )

    # 候选错位布局：F=学历 & G=岗位描述（§3.5.1，签名匹配）
    if f_cls == "education" and _is_job_description_like(g_val):
        suggested_type = _suggest_record_type_from_title(row.get("D"))
        # 额外建议：E 列若符合届次规则才建议为 target_cohort（修复点 5）
        extra: dict[str, Any] = {}
        e_val = row.get("E")
        if e_val and _is_cohort(e_val):
            extra["target_cohort"] = e_val
        return _build_candidate_record(
            row,
            _HK_SHIFTED_SUGGESTED,
            suggested_type,
            LAYOUT_HK_SHIFTED,
            SHEET_HK,
            source_row,
            extra_suggested=extra or None,
        )

    # 其余 unknown
    return _build_unknown_record(row, SHEET_HK, source_row)


def _is_job_description_like(value: object) -> bool:
    """香港错位布局 G 列判定：岗位描述取值（§3.5.1，G 含 job_description 923 行）。

    岗位描述通常为较长的自由文本，既非届次也非学历也非空。
    本函数保守判定：非空且不匹配 cohort/education 即视为"岗位描述候选"。
    """
    if value is None or str(value).strip() == "":
        return False
    if _is_cohort(value) or _is_education(value):
        return False
    return True


def detect_us(row: Mapping, source_row: int) -> dict[str, Any]:
    """美国 F/G 标准布局与交换布局检测（§3.5.2）。

    候选布局只输出 unknown + 建议字段，需人工复核（修复点 1）。
    """
    f_val = row.get("F")
    g_val = row.get("G")
    f_cls = _classify_hk_us_f(f_val)
    g_cls = _classify_hk_us_g(g_val)

    # 候选标准布局：F=届次 & G=学历
    if f_cls == "cohort" and g_cls == "education":
        suggested_type = _suggest_record_type_from_title(row.get("H"))
        return _build_candidate_record(
            row,
            _US_STANDARD_SUGGESTED,
            suggested_type,
            LAYOUT_US_STANDARD,
            SHEET_US,
            source_row,
        )

    # 候选交换布局：F=学历 & G=届次（§3.5.2，615 行）
    if f_cls == "education" and g_cls == "cohort":
        suggested_type = _suggest_record_type_from_title(row.get("H"))
        return _build_candidate_record(
            row,
            _US_SWAPPED_SUGGESTED,
            suggested_type,
            LAYOUT_US_SWAPPED,
            SHEET_US,
            source_row,
        )

    # 其余 unknown
    return _build_unknown_record(row, SHEET_US, source_row)


def detect_uk(row: Mapping, source_row: int) -> dict[str, Any]:
    """英国：F 稳定（招聘类别），G 歧义子集标 unknown（§3.5.3）。

    修复点 2：英国 F 列使用独立招聘类型规则（_is_uk_sg_recruitment_type），
    不复用大陆 _RECRUITMENT_KEYWORDS 白名单，支持 Graduate Programme 等文本。
    """
    f_val = row.get("F")
    g_val = row.get("G")
    if not _is_uk_sg_recruitment_type(f_val):
        # F 异常（如 F=本科、F=2026届 等错位值）也归 unknown
        return _build_unknown_record(row, SHEET_UK, source_row)
    g_cls = _classify_uk_sg_g(g_val)
    if g_cls == "cohort":
        d_val = row.get("D")
        if d_val and str(d_val).strip():
            return _apply_mapping(
                row,
                _UK_MAP,
                "job",
                LAYOUT_UK_DEFAULT,
                SHEET_UK,
                source_row,
                job_title_required=True,
            )
        company = row.get("C")
        return _apply_mapping(
            row,
            _UK_MAP,
            "campaign",
            LAYOUT_UK_DEFAULT,
            SHEET_UK,
            source_row,
            display_title_override=str(company) if company else None,
        )
    # G 歧义 → unknown
    return _build_unknown_record(row, SHEET_UK, source_row)


def detect_sg(row: Mapping, source_row: int) -> dict[str, Any]:
    """新加坡：F 100% 稳定（招聘类别），G 歧义子集标 unknown（§3.5.3）。

    新加坡与中国香港不同构，使用独立策略（不得共用静态映射）。
    修复点 2：新加坡 F 列使用独立招聘类型规则（_is_uk_sg_recruitment_type），
    不复用大陆 _RECRUITMENT_KEYWORDS 白名单。
    """
    f_val = row.get("F")
    g_val = row.get("G")
    if not _is_uk_sg_recruitment_type(f_val):
        return _build_unknown_record(row, SHEET_SG, source_row)
    g_cls = _classify_uk_sg_g(g_val)
    if g_cls == "cohort":
        d_val = row.get("D")
        if d_val and str(d_val).strip():
            return _apply_mapping(
                row,
                _SG_MAP,
                "job",
                LAYOUT_SG_DEFAULT,
                SHEET_SG,
                source_row,
                job_title_required=True,
            )
        company = row.get("C")
        return _apply_mapping(
            row,
            _SG_MAP,
            "campaign",
            LAYOUT_SG_DEFAULT,
            SHEET_SG,
            source_row,
            display_title_override=str(company) if company else None,
        )
    return _build_unknown_record(row, SHEET_SG, source_row)


def detect_junior_global(row: Mapping, source_row: int) -> dict[str, Any]:
    """低年级项目-全球版：F=职位/项目名称，保守判定 job/campaign/unknown（§4.6，修复点 4）。"""
    f_val = row.get("F")
    classification = _junior_global_classify(f_val)
    if classification == "unknown":
        return _build_unknown_record(row, SHEET_JUNIOR_GLOBAL, source_row)
    return _apply_mapping(
        row,
        _JUNIOR_GLOBAL_MAP,
        classification,
        LAYOUT_JUNIOR_GLOBAL,
        SHEET_JUNIOR_GLOBAL,
        source_row,
        job_title_required=(classification == "job"),
    )


def detect_junior_official(row: Mapping, source_row: int) -> dict[str, Any]:
    """低年级项目-美国&香港-公司官网：项目入口型，多为 campaign（§4.7）。

    无截止日期列，deadline 不得臆造，留空。
    """
    f_val = row.get("F")
    if f_val is None or str(f_val).strip() == "":
        return _build_unknown_record(row, SHEET_JUNIOR_OFFICIAL, source_row)
    company = row.get("D")
    return _apply_mapping(
        row,
        _JUNIOR_OFFICIAL_MAP,
        "campaign",
        LAYOUT_JUNIOR_OFFICIAL,
        SHEET_JUNIOR_OFFICIAL,
        source_row,
        display_title_override=str(f_val) if f_val else (str(company) if company else None),
    )


# ---------------------------------------------------------------------------
# unknown 记录构造
# ---------------------------------------------------------------------------


def _build_unknown_record(
    row: Mapping,
    source_sheet: str,
    source_row: int,
    *,
    detection_reason: str | None = None,
) -> dict[str, Any]:
    """构造 unknown 记录：保留完整 raw_data，不强行映射业务字段。

    任务 11A：
    - 中国大陆预览优先级改为 L > E > F > 其他回退值（不得优先显示 C 列）；
    - 新增 detection_reason 字段，帮助用户判断 unknown 原因。
    """
    # display_title 优先级按工作表区分
    if source_sheet == SHEET_MAINLAND:
        # 中国大陆：L 公告标题 > E 企业名称 > F 岗位/招聘类型 > 其他
        display = str(
            row.get("L") or row.get("E") or row.get("F")
            or row.get("D") or row.get("C") or ""
        )[:200]
    else:
        # 其他工作表：保持原有优先级
        display = str(row.get("C") or row.get("D") or row.get("E") or "")[:200]

    record: dict[str, Any] = {
        "record_type": "unknown",
        "source_sheet": source_sheet,
        "source_row": source_row,
        "layout": LAYOUT_UNKNOWN,
        "raw_data": _build_raw_data(row),
        "display_title": display,
    }
    if detection_reason:
        record["detection_reason"] = detection_reason
    return record


# ---------------------------------------------------------------------------
# 工作表路由
# ---------------------------------------------------------------------------

# 工作表名 → 判定函数
_SHEET_DETECTORS: dict[str, Any] = {
    SHEET_MAINLAND: detect_mainland,
    SHEET_HK: detect_hk,
    SHEET_US: detect_us,
    SHEET_UK: detect_uk,
    SHEET_SG: detect_sg,
    SHEET_JUNIOR_GLOBAL: detect_junior_global,
    SHEET_JUNIOR_OFFICIAL: detect_junior_official,
}


def detect_row(
    sheet_name: str,
    row: Mapping,
    source_row: int,
    headers: Mapping[object, object] | None = None,
) -> dict[str, Any]:
    """按工作表名路由到对应判定函数。

    Args:
        sheet_name: 工作表名。
        row: 原始行（键为列字母 A/B/C...）。
        source_row: 来源行号（1-based，对应原文件物理行号，含表头）。
        headers: 列字母 → 原始表头文本（任务 11A.1，可选）。
            仅中国大陆判定函数使用；其余工作表忽略。
            通过参数显式传递，不使用全局可变状态。

    Returns:
        标准化 Opportunity 记录 dict，含 record_type / layout / raw_data /
        source_sheet / source_row 等字段。候选布局另含
        needs_confirmation / suggested_record_type / suggested_fields。

    Raises:
        UnsupportedSheetError: 工作表名不在 SUPPORTED_SHEETS 中。
    """
    detector = _SHEET_DETECTORS.get(sheet_name)
    if detector is None:
        raise UnsupportedSheetError(
            f"不支持的工作表：{sheet_name}（支持：{sorted(SUPPORTED_SHEETS)}）"
        )
    if sheet_name == SHEET_MAINLAND:
        return detector(row, source_row, headers=headers)
    return detector(row, source_row)


class UnsupportedSheetError(ValueError):
    """工作表名不被 layout_detector 支持。"""


def is_candidate_layout(layout: str) -> bool:
    """布局是否为"候选布局"（基于签名判定，最终需人工复核）。

    香港/美国的"标准/交换"仅是候选，不得声称全部已可靠识别
    （docs/SOURCE_SCHEMA.md §4.2 / §4.4）。
    """
    return layout in _CANDIDATE_LAYOUTS
