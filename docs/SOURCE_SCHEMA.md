# CareerCopilot 源数据结构说明 (SOURCE_SCHEMA)

本文档记录真实岗位工作簿（XLSX）的结构，用于指导导入解析器开发与匿名样本生成。结构结论已通过对真实文件的**只读检查实证**（详见 [WORKBOOK_PROFILE.md](WORKBOOK_PROFILE.md)）。

> ⚠️ 真实源文件（约 70.8 MB、37,943 条记录）**不得提交到 GitHub**，也**不得作为测试夹具直接复制进仓库**。真实数据应放在仓库外部，或放入被 `.gitignore` 完整排除的 `data/private/`。仓库内只允许存放根据本文档规则生成的匿名样本（`data/sample/sample_opportunities.csv`）。

## 1. 工作簿概览

真实 XLSX 文件包含 **7 个工作表**，**每个工作表的列结构各不相同**（不只中国大陆存在布局差异）：

| 序号 | 工作表 | 数据行 | 列结构 | 是否存在内部布局漂移 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 中国大陆 | 8630 | A–N（14 列） | **是**（campaign 新版 / job 旧版混存，见 §3） |
| 2 | 中国香港 | 3923 | A–J（10 列） | **是**（标准布局 + 错位布局，F 列含 RT 2140 与 education 1783；E 列 2168 行承载届次） |
| 3 | 美国 | 21705 | A–L（12 列） | **是**（F/G 字段交换：标准 14348 + 交换 ~615） |
| 4 | 英国 | 2603 | A–K（11 列） | 基本否（F 稳定；G 约 540 行歧义） |
| 5 | 新加坡 | 833 | A–J（10 列） | 基本否（F 100% 稳定；G 约 155 行歧义；**与中国香港不同构**） |
| 6 | 低年级项目-全球版 | 122 | A–L（12 列） | 否 |
| 7 | 低年级项目-美国&香港-公司官网 | 127 | A–H（8 列） | 否 |

**结论**：这不是单一招聘平台导出的“智联岗位表”，而是覆盖多地区、多来源的**机会数据集（Opportunity Dataset）**。导入模块应使用通用命名，如 `opportunity_importer` 或 `workbook_importer`，不得命名为 `zhilian_importer`。

## 2. 记录类型 (Record Type)

数据集中同时存在两类记录，导入时必须区分，并在界面上明确标注：

| record_type | 含义 | 典型特征 |
| :--- | :--- | :--- |
| `campaign` | 校招项目、招聘公告或统一投递入口 | 有公告名称/公告链接/投递链接，但**没有明确岗位名称**；投递需进入官网选择具体岗位 |
| `job` | 具有明确岗位名称和岗位描述的具体职位 | 有明确 `job_title`，通常有岗位类别、工作城市等 |
| `unknown` | 无法可靠判定 | 保留 `raw_data`，导入预览时人工确认，不直接入库业务字段 |

### 2.1 两类记录的处理差异

* `campaign` 与 `job` 在看板上**必须视觉区分**（不同标签/颜色）。
* 对于只有 `campaign`、没有具体 `job` 的公司，界面显示提示：**“需进入招聘官网选择具体岗位”**。
* **MVP 0.1 不承诺**从 `campaign` 记录中拆出三个具体岗位；只统计每家公司是否有至少 3 个候选 opportunity（campaign + job 合计）。
* “每家公司至少推荐三个**具体岗位**”的能力，**只有在 MVP 0.2 获取到明确岗位信息后**才执行。

## 3. 中国大陆工作表的两种历史布局（实证确认）

中国大陆工作表 row1 表头如下（真实列标签）：

| 列 | row1 表头 |
| :--- | :--- |
| E | 企业名称 |
| F | 招聘岗位 |
| G | 招聘对象（届次） |
| H | 学历要求 |
| I | 招聘类别 |
| J | 工作城市 |
| K | 截止时间 |
| L | 公告名称 |
| M | 公告链接 |
| N | 投递链接 |

> row1 表头标签描述的是 **campaign（新版）布局**的列语义。**但其中 4569 行 job（旧版）记录的列语义整体错位，不遵循 row1 标签**（另有 558 行 F=job 但 G≠城市，仍属待确认，不计入已确认 job），因此不能仅凭表头静态映射全部记录。

### 3.1 新版 campaign 布局（实证：2114 行，F=招聘类型关键词 + G=届次）

| 列 | 字段语义 |
| :--- | :--- |
| E | 企业名称 |
| F | 招聘类型（秋招全职/春招全职/日常实习/暑期实习等关键词） |
| G | 招聘届次 |
| H | 学历要求 |
| I | 招聘类别 / 岗位类别 |
| J | 工作城市 |
| K | 截止时间 |
| L | 公告名称 |
| M | 公告链接 |
| N | 投递链接 |

### 3.2 旧版 job 布局（实证：4569 行，F=具体岗位名称 + G=工作城市）

| 列 | 字段语义 |
| :--- | :--- |
| E | 企业名称 |
| F | 具体岗位名称 |
| G | 工作城市 |
| H | 招聘类型 |
| I | 招聘届次 |
| J | 学历要求 |
| K | 截止时间 |
| L | 公告名称 |
| M | 公告链接 |
| N | 投递链接 |

### 3.3 布局判定规则（强约束，已实证）

对 8712 条非表头行做 F×G 语义交叉（仅类别计数）：

| F 语义 \ G 语义 | cohort(届次) | city(城市) | other | empty |
| :--- | :--- | :--- | :--- | :--- |
| **kw**（招聘类型关键词） | **2114** | 0 | 0 | 0 |
| **job**（具体岗位名称） | 34 | **4569** | 524 | 0 |
| **other** | 121 | 1057 | 209 | 0 |
| **empty** | 0 | 0 | 0 | 84 |

**判定规则**：

1. 若 **F 列取值**为“秋招全职、春招全职、日常实习、暑期实习”等**招聘类型**关键词，**且 G 为届次** → 判定为**新版 campaign 布局**。
2. 若 **F 列取值**为具体岗位名称（通过 `_is_job_title_like()` 判定，含工程师/开发/分析师/实习生/管培生/经理/专员/助理/顾问等关键词），**且 G 为工作城市** → 判定为**旧版 job 布局**。
3. 若 F 为非岗位名称的普通文本（如“其他类别/综合招聘信息/招聘公告/多岗位/不限/可议”等），即使 G=城市，也**不得判为 job**，标记 `unknown`。
4. 其他情况（F=other 1387 行、F=job 但 G≠city 共 558 行、F=empty 84 行）**不得强行推断**，必须：
   - 保留 `raw_data`（整行原始数据，JSON 文本）；
   - 在**导入预览**阶段交由用户人工确认记录类型与字段映射；
   - 对无法判定的行，标记为 `record_type = unknown` 并计入“无效/待确认”统计，不直接入库。

## 4. 各工作表标准字段映射

> 仅映射从表头可**可靠对应**的列。无法对应的字段留空，原始值保留进 `raw_data`。所有工作表均写入 `source_sheet` + `source_row`。

### 4.1 中国大陆

| 标准字段 | campaign（新版）来源列 | job（旧版）来源列 |
| :--- | :--- | :--- |
| `company_name` | E | E |
| `display_title` | L（公告名称） | F（岗位名称） |
| `job_title` | （空） | F |
| `job_categories` | I | （空） |
| `industry` | D | D |
| `recruitment_type` | F | H |
| `target_cohort` | G | I |
| `education_requirement` | H | J |
| `location` | J | G |
| `deadline` | K | K |
| `announcement_title` | L | L |
| `announcement_url` | M | M |
| `application_url` | N | N |

### 4.2 中国香港（10 列，**多种布局**，不得静态映射）

> ⚠️ 香港存在多种布局（详见 [WORKBOOK_PROFILE.md](WORKBOOK_PROFILE.md) §3.5.1，互斥合计 3923 行）：候选标准布局 1368 行（F=招聘类别 & G=届次）；候选错位布局 923 行（F=学历 & G=岗位描述）；待确认 1632 行。F 列同时含招聘类型(2140)与学历(1783)，E 列在 2168 行中承载届次。下表为**候选标准布局**的列映射；错位布局与待确认行须在导入预览人工确认，不强行推断。上述"标准/错位"仅为基于签名的**候选布局**，不得声称全部已可靠识别。
>
> **`suggested_record_type` 保守判定**：D 通过 `_is_job_title_like()` → 建议为 job；D 为空 → 建议为 campaign；D 非空但不像岗位名称（如招聘类型/届次/学历/描述等）→ `suggested_record_type=None`（不提供最终建议）。`record_type` 始终为 `unknown`，`needs_confirmation=True`，`raw_data` 完整保留。

标准布局（F=招聘类别 & G=届次）：

| 标准字段 | 来源列 |
| :--- | :--- |
| `company_name` | C |
| `display_title` | D（招聘岗位）/ 公司+招聘类别（campaign） |
| `job_title` | D |
| `job_categories` | （空） |
| `industry` | B |
| `recruitment_type` | F |
| `target_cohort` | G |
| `education_requirement` | H |
| `location` | （空，无城市列） |
| `deadline` | I |
| `announcement_title` | （空） |
| `announcement_url` | （空） |
| `application_url` | J |

### 4.3 新加坡（10 列，与香港**不同构**）

新加坡 F 列 100% 为招聘类型（833/833），G 列约 155 行歧义。基本可静态映射，歧义行人工确认。列映射与香港标准布局相同（C/B/D/F/G/H/I/J），但**不得因列数相同而与香港混用同一解析器**。

> **独立招聘类型规则**：新加坡 F 列使用 `_is_uk_sg_recruitment_type`（非空且非日期/URL/届次/学历即可），**不复用大陆 `_RECRUITMENT_KEYWORDS` 白名单**，支持 Graduate Programme 等文本。F=本科或 F=2026届 等明显错位值 → `unknown`。

### 4.4 美国（12 列，**F/G 字段交换**，不得静态映射）

> ⚠️ 美国存在 F/G 字段交换（详见 [WORKBOOK_PROFILE.md](WORKBOOK_PROFILE.md) §3.5.2，互斥合计 21705 行）：候选标准布局 14348 行（F=届次 & G=学历）；候选交换布局 615 行（F=学历 & G=届次）；待确认 6742 行。其中“F 或 G 取值为 other”约 6114 行只是待确认记录的子集，不代表全部未确认记录。下表为**候选标准布局**映射；交换布局须将 F↔G 互换后映射；所有待确认行标 `unknown` 人工确认。

标准布局（F=届次 & G=学历）：

| 标准字段 | 来源列 |
| :--- | :--- |
| `company_name` | D |
| `display_title` | H（招聘岗位）/ 公司+届次（campaign） |
| `job_title` | H |
| `job_categories` | B（行业/岗位分类，按需拆分） |
| `industry` | B |
| `recruitment_type` | E |
| `target_cohort` | F |
| `education_requirement` | G |
| `location` | J |
| `deadline` | K |
| `announcement_title` | （空） |
| `announcement_url` | （空） |
| `application_url` | L |

### 4.5 英国（11 列）

| 标准字段 | 来源列 |
| :--- | :--- |
| `company_name` | C |
| `display_title` | D / 公司+招聘类别 |
| `job_title` | D |
| `industry` | B |
| `recruitment_type` | F |
| `target_cohort` | G |
| `education_requirement` | H |
| `location` | I（城市） |
| `deadline` | J |
| `application_url` | K |

> **独立招聘类型规则**：英国 F 列使用 `_is_uk_sg_recruitment_type`（非空且非日期/URL/届次/学历即可），**不复用大陆 `_RECRUITMENT_KEYWORDS` 白名单**，支持 Graduate Programme 等文本。F=本科或 F=2026届 等明显错位值 → `unknown`。

### 4.6 低年级项目-全球版（12 列）

| 标准字段 | 来源列 |
| :--- | :--- |
| `company_name` | D（公司） |
| `display_title` | F（职位名称）/ 项目名 |
| `job_title` | F（若为具体职位则 job，否则 campaign） |
| `industry` | C |
| `recruitment_type` | E（招聘类型） |
| `target_cohort` | I（毕业时间） / H（年级） |
| `education_requirement` | G（学位） |
| `location` | B（地区） |
| `deadline` | K |
| `application_url` | L（网申地址） |

### 4.7 低年级项目-美国&香港-公司官网（8 列）

| 标准字段 | 来源列 |
| :--- | :--- |
| `company_name` | D |
| `display_title` | F（项目名称） |
| `job_title` | （空，项目入口型） |
| `industry` | B |
| `recruitment_type` | E（项目类型） |
| `target_cohort` | G（适合年级） |
| `location` | A（地区） |
| `deadline` | （空，无截止日期列，**不得臆造**） |
| `application_url` | H（官方项目链接） |

> 该表 `record_type` 通常为 `campaign`（项目入口），`date_cells=0`，截止时间缺失需留空。

## 5. 静态映射 vs 人工确认（已按补充验证修订）

| 工作表 | 可否静态映射 | 需人工确认的记录 |
| :--- | :--- | :--- |
| 中国大陆 | **否** | F=other（1387）、F=job 但 G≠city（558）、F=empty（84） |
| 中国香港 | **否** | 候选标准 1368 行 + 候选错位 923 行 + 待确认 1632 行（=3923），均需人工复核 |
| 美国 | **否** | 候选标准 14348 行 + 候选交换 615 行 + 待确认 6742 行（=21705），均需人工复核 |
| 英国 | **基本可** | F 稳定；约 540 行 G 歧义需人工确认 |
| 新加坡 | **基本可** | F 100% 稳定；约 155 行 G 歧义需人工确认 |
| 低年级-全球版 | 是 | F 列无法判断职位/项目的行 |
| 低年级-公司官网 | 是 | 基本为 campaign，歧义行标 unknown |

**原则：凡不能可靠判定的，一律标 `record_type=unknown` 并保留 `raw_data`，在导入预览由人工确认，不得强行推断。**

## 6. 匿名样本数据规则

`data/sample/sample_opportunities.csv` 必须使用以下虚构素材，**不得直接复制真实公司数据**：

* **公司名**：示例科技A、示例制造B、示例银行C、示例咨询D、示例零售E ……
* **域名**：`example.com` 及其子域
* **岗位/公告名称**：虚构名称（如“示例后端开发工程师-2026秋招”）
* **描述**：虚构 JD 文本
* **数量**：至少覆盖 campaign 记录、job 记录、同一公司三个以上机会、公司机会不足三个、缺少具体岗位名称、缺少申请链接、重复记录、无效记录、以及两种历史字段布局的标准化结果。

### 6.1 样本用途与边界（重要）

`data/sample/sample_opportunities.csv` 是**标准化后的输出样本**（即解析器产出的 `opportunities` 记录），**只用于**：

- 数据模型（`opportunities` 表）字段映射测试；
- 去重服务（`dedupe_key` 生成与重复识别）测试；
- 候选清单/公司覆盖检查测试；
- 数据库读写持久化测试。

它**不能**单独用于验证原始 XLSX 布局识别（`layout_detector` / `opportunity_importer`）。原始布局识别须由**独立的原始布局测试夹具**覆盖（见 [TASKS.md](TASKS.md) 解析器任务）。真实数据仍不得复制到任何测试夹具。
