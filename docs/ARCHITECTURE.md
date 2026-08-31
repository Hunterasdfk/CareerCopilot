# CareerCopilot 架构设计文档 (ARCHITECTURE)

## 1. MVP 0.1 模块划分

为了保持代码清晰、职责单一，我们将 MVP 0.1 的代码组织为以下几个核心模块。**本阶段遵循极简原则，不引入连接池和复杂的 ORM 框架。**

*   **`pages/` (User Interface Layer)**
    *   遵循 Streamlit 标准的多页应用结构，`pages/` 目录应与 `app.py` (Home 页) 位于同一根目录下。Streamlit 会自动加载 `pages/` 目录下的 `.py` 文件作为独立页面。
    *   `import_page.py`: 机会导入页面（工作表选择、布局识别、字段映射、导入预览、导入报告）。
    *   `dashboard.py`: 机会看板页面（展示、筛选、状态管理，campaign/job 区分）。
    *   `components/`: 可复用的 UI 组件，如 `opportunity_card.py` (机会卡片), `filters.py` (筛选器)。

*   **`services/` (Business Logic Layer)**
    *   `opportunity_importer.py`（或 `workbook_importer.py`）：**通用机会导入器**。不得命名为 `zhilian_importer`。负责按工作表选择解析器、识别布局、产出标准化机会列表与导入报告（新增/重复/无效/待确认）。
    *   `layout_detector.py`: 布局识别器。按工作表选择解析策略：中国大陆 campaign/job/unknown 逐行识别；中国香港标准/错位布局检测；美国 F/G 标准与交换布局检测；英国/新加坡稳定字段映射 + G 歧义子集标 unknown；低年级两个工作表独立策略。所有无法可靠判断的记录进入导入预览。
    *   `dedup_service.py`: 去重服务，按 `record_type` 差异化计算 `dedupe_key`。
    *   `opportunity_service.py`: 机会服务，封装增删改查与状态流转逻辑（区分自动跳转 `opened` 与手动确认 `applied`）。
    *   `candidate_service.py`: 候选清单服务，实现“公司覆盖检查”：按公司返回全部已排序机会，同时返回 `total_count`、`coverage_gap`（不足 3 的缺口）、`highlighted_top_three`（按优先级默认突出的前 3 个，**非截断**）、`campaign_only`（仅 campaign 的公司标注“需进入官网选择具体岗位”）。候选清单可超过 3 个机会，任何视图不得因“前 3”隐藏其他机会。

*   **`database/` (Data Access Layer)**
    *   `db_handler.py`: 数据库操作处理器，直接使用 Python 标准库 `sqlite3`，封装对 SQLite 数据库的连接、初始化和基础 SQL 操作。

*   **`config/` (Configuration)**
    *   `settings.py`: 全局配置，如数据库文件路径、默认数据目录等。

## 2. 数据流

MVP 0.1 的核心数据流是一个闭环：

1.  **输入**: 用户上传工作簿 -> 选择工作表 -> `opportunity_importer.py` 调用 `layout_detector.py` 识别布局。
2.  **解析与预览**: 对自动判定布局的记录直接映射标准字段；对 unknown 记录保留 `raw_data`，在**导入预览**阶段交由用户人工确认记录类型与字段映射。
3.  **去重**: `dedup_service.py` 按 `record_type` 计算 `dedupe_key`，与数据库现有记录比对。
4.  **报告与入库**: 系统向用户展示“新增/重复/无效/待确认”数量。用户确认后，`opportunity_service.py` 调用 `db_handler.py` 将数据写入 `opportunities` 表。
5.  **读取**: 页面加载时，从数据库读取全量或筛选后的机会数据。
6.  **处理**: `candidate_service.py` 按公司分组返回全部已排序机会，并返回 `total_count` / `coverage_gap` / `highlighted_top_three` / `campaign_only`；前 3 个仅作突出展示，不截断、不隐藏其他机会。
7.  **渲染**: `components/opportunity_card.py` 将数据渲染为可视化卡片，campaign/job 视觉区分。
8.  **状态变更**:
    *   **自动**: 用户点击“投递”按钮 -> `opportunity_service.py` 将状态更新为 `opened` (不得覆盖更高级状态)。
    *   **手动**: 用户点击“确认已投递” -> 状态更新为 `applied`。
    *   **持久化**: 所有变更实时保存于 SQLite。

## 3. 技术选型

*   **核心语言**: Python 3.11
*   **前端/框架**: Streamlit (v1.28+)
*   **数据库**: SQLite (Python 内置标准库 `sqlite3`)
    *   **理由**: 无需独立部署，单文件存储。本阶段直接使用原生 `sqlite3` API，不引入 SQLAlchemy / SQLModel，以保持轻量。
*   **数据处理**: pandas, openpyxl
*   **测试框架**: pytest

## 4. 按工作表选择解析器 (Sheet-aware Parsing)

针对多工作表、多布局的源数据（详见 [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md)）：

1.  **工作表注册表**: `opportunity_importer.py` 维护工作表名到解析策略的映射。**每个工作表使用各自独立的解析策略**，不得因列数相同而混用（如中国香港与新加坡不同构）。
2.  **中国大陆**：`layout_detector.py` 逐行判定 `campaign` / `job` / `unknown`：
    *   F 列 ∈ {秋招全职, 春招全职, 日常实习, 暑期实习, ...} **且 G 为届次** -> campaign（新版）布局。
    *   F 为具体岗位名称（通过 `_is_job_title_like()` 判定，含工程师/开发/分析师/实习生等关键词）**且 G 为工作城市** -> job（旧版）布局。
    *   F 为非岗位名称的普通文本（如"其他类别/综合招聘信息/招聘公告"）-> `unknown`，**不得因 G=城市 就判 job**。
    *   其他情况 -> `unknown`，保留 `raw_data`，进入导入预览人工确认。
3.  **中国香港**：检测**标准布局**（F=招聘类别 & G=届次）与**历史错位布局**（F=学历 & G=岗位描述）两类签名；签名匹配的标记为**候选布局**（最终需人工复核），其余记为 `unknown` 人工确认。`suggested_record_type` 保守判定：D 通过 `_is_job_title_like()` -> job；D 为空 -> campaign；D 非空但不像岗位名称（如招聘类型/届次/学历/描述）-> **不提供最终建议**（`suggested_record_type=None`）。
4.  **美国**：检测 **F/G 标准布局**（F=届次 & G=学历）与 **F/G 交换布局**（F=学历 & G=届次）；交换布局须将 F↔G 互换后映射；两类签名之外的记录标 `unknown` 人工确认。H 列保守判定同香港 D 列。
5.  **英国 / 新加坡**：F 列稳定（招聘类型），使用独立规则 `_is_uk_sg_recruitment_type`（非空且非日期/URL/届次/学历即可），**不复用大陆 `_RECRUITMENT_KEYWORDS` 白名单**，支持 Graduate Programme 等文本；按各自约定列映射正常解析；**仅 G 列歧义子集**（英国约 540 行、新加坡约 155 行）标 `unknown` 人工确认。F=本科或 F=2026届 等明显错位值 -> `unknown`。
6.  **低年级项目-全球版 / 低年级项目-美国&香港-公司官网**：使用各自独立解析策略；前者 F=职位名称、后者 F=项目名称，本质偏项目入口（campaign-like）；无具体岗位的记 `campaign`，歧义行标 `unknown`。
7.  **导入预览**：所有无法可靠判断的记录（`unknown`）进入导入预览，由用户人工确认 `record_type` 与列映射后才能写入 `opportunities` 表（见 §6 unknown 生命周期）。
8.  **不变原则**：任何工作表，凡不能可靠判定的，一律标 `unknown` 并保留 `raw_data`，**不强行推断**。

## 5. 后续扩展位置 (Extension Points)

为后续版本预留清晰的扩展接口：

*   **MVP 0.2: 简历匹配与 Playwright 自动化**
    *   **简历解析**: 在 `services/` 下新增 `resume_parser.py`。
    *   **匹配算法**: 在 `services/` 下新增 `matcher.py`，其输出可集成到 `candidate_service.py` 中，替代手动优先级作为推荐依据。**此阶段才执行“每家公司至少推荐三个具体岗位”**。
    *   **campaign 拆岗**: 在 `services/` 下新增 `campaign_expander.py`，从 campaign 入口拉取具体岗位（需联网，受风控约束）。
    *   **自动化投递**: 在 `services/` 下新增 `auto_applier.py`，UI 层 `pages/dashboard.py` 增加辅助投递按钮。

*   **MVP 0.3: Gmail 集成与通知**
    *   **邮件服务**: 在 `services/` 下新增 `email_service.py`。
    *   **通知服务**: 在 `services/` 下新增 `notifier.py`。
    *   **UI 集成**: 新增 `pages/emails.py` 页面。

## 6. unknown 生命周期

`unknown` 是导入流程中的**过渡态**，不进入最终数据库：

1. **解析阶段**：`layout_detector.py` 对无法可靠判定的记录标 `record_type=unknown`，保留 `raw_data`（整行原始数据）。
2. **预览阶段**：`unknown` 记录使用临时 `preview_id`（或 `source_sheet` + `source_row`）识别，**不生成最终数据库的 `dedupe_key`**。
3. **确认入库**：用户在导入预览将 `unknown` 改为 `campaign` 或 `job` 并指定列映射后，才计算 `dedupe_key` 并写入 `opportunities` 表。
4. **未确认/无效不入库**：未确认或无效的记录**不进入 `opportunities` 表**。
5. **数据库约束**：`opportunities.record_type` 只允许 `campaign` / `job` 两个值，**不存储 `unknown`**。

> 匿名样本 `data/sample/sample_opportunities.csv` 中的 `unknown` 行用于测试"无效/待确认记录不会入库"的行为。

## 6.1 解析阶段临时结构（候选布局）

香港/美国候选布局（`hk_standard_candidate` / `hk_shifted_candidate` / `us_standard_candidate` / `us_swapped_candidate`）在解析阶段输出如下临时结构，**不得直接入库**：

| 字段 | 值 | 说明 |
| :--- | :--- | :--- |
| `record_type` | `"unknown"` | 始终为 unknown，compute_dedupe_key 返回 None |
| `layout` | 候选布局标签 | 如 `hk_standard_candidate`，保留候选签名 |
| `needs_confirmation` | `True` | 标记需人工复核 |
| `suggested_record_type` | `"job"` / `"campaign"` / `None` | 保守建议；D（或 H）通过 `_is_job_title_like()` -> job；为空 -> campaign；无法判断 -> **None（不提供最终建议）** |
| `suggested_fields` | `{字段: 取值}` | 暂定字段映射，仅为建议，不得入库；只纳入符合字段语义的取值 |
| `raw_data` | 完整原始行 | 整行单元格保留，键为列字母 |
| `source_sheet` / `source_row` | 工作表名 / 物理行号 | 回溯字段 |

> 顶层**不写** `company_name` / `job_title` / `target_cohort` 等最终业务字段。确认操作属任务 5。

## 6.2 source_row 行号约定

`source_row` 对应原 XLSX/CSV 文件的**物理行号**（1-based）：

- 表头位于第 1 行；
- 第一条数据 `source_row = 2`；
- 空行可以不输出记录，但**必须占用物理行号**；
- 空行后的记录继续使用真实 Excel/CSV 行号，**不得重新连续编号**；
- `parse_sheet` 使用 `enumerate(df.iterrows(), start=2)` 按行位置计算物理行号，**不依赖 DataFrame index 可转 int**（支持自定义或字符串 index）。
