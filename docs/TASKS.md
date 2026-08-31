# CareerCopilot MVP 0.1 开发任务拆解

以下任务将按照开发顺序排列，每个任务都是可独立完成和测试的最小单元。

> 说明：真实源工作簿（约 70 MB、37943 条记录、7 个工作表）**不得提交到 GitHub**，也**不得作为测试夹具直接复制进仓库**。仓库内只允许存放根据 [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md) 规则生成的匿名样本。

---

### 任务 1: 真实工作簿结构检查与匿名样本建立 ✅ 已完成

*   **描述**: 检查真实 XLSX 的多工作表结构与历史布局变体，建立一份覆盖两类记录与多种布局的匿名样本文件。
*   **输入**:
    *   用户提供的真实岗位工作簿（`data/private/智联-岗位信息表.xlsx`，约 70.8 MB、37,943 条记录）。
    *   [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md) 的结构结论。
*   **输出**:
    *   [WORKBOOK_PROFILE.md](WORKBOOK_PROFILE.md): 真实工作簿结构画像（7 个工作表表头、行数、空行、必要字段缺失、字段类型、中国大陆两种布局的 F×G 实证交叉表、可否静态映射、须保留到 raw_data 的内容）。
    *   [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md): 已按真实表头实证修订，含各工作表标准字段映射、静态映射 vs 人工确认。
    *   [sample_opportunities.csv](../data/sample/sample_opportunities.csv): 13 行匿名样本，覆盖 campaign/job、同一公司≥3、公司<3、缺少 job_title、缺少 application_url、重复记录、无效记录、以及中国大陆 campaign 新版 / job 旧版 / 中国香港 / 低年级全球版 多种布局的标准化结果。
    *   匿名素材使用虚构公司（示例科技A/B/C/D/E）、`example.com` 域名、虚构岗位与描述；**未直接复制真实公司数据**。
*   **完成标准**:
    *   ✅ 真实源文件经 `git check-ignore` 确认被 `.gitignore` 排除（不会进入 Git）。
    *   ✅ 只读检查（标准库 `zipfile`+`xml`，未修改原文件、未安装依赖）。
    *   ✅ 样本准确反映真实数据的表头结构与布局变体；raw_data JSON 全部合法可解析。
    *   ✅ 样本无任何真实个人信息或公司敏感信息；`git check-ignore` 确认样本可提交。

### 任务 2: 项目初始化和安全配置

*   **描述**: 建立项目的基础目录结构、依赖管理和全局安全配置。
*   **输入**:
    *   确定的技术栈 (Python 3.11, Streamlit, SQLite, Pandas, openpyxl, pytest)。
*   **前置检查（必须最先执行，Python 3.11 版本验证）**:
    1.  运行 `py -0p`，确认本机已安装 Python **3.11**。
    2.  **如果不存在 Python 3.11**：
        *   **立即停止**本任务，不得继续后续步骤；
        *   **不创建**虚拟环境（`.venv`）；
        *   **不使用** Python 3.10 或其他版本代替；
        *   **不自动下载或安装** Python；
        *   向用户报告：需先安装 Python 3.11 后才能继续任务 2。
    3.  **如果存在 Python 3.11**，才使用 `py -3.11 -m venv .venv` 创建虚拟环境，并继续后续步骤。
*   **输出**:
    *   项目根目录结构 (`pages/`, `services/`, `database/`, `config/`, `tests/`)。
    *   `requirements.txt`: 包含所有核心依赖。
    *   `.gitignore`: 明确排除 `*.db`, `*.sqlite`, `data/*.xlsx`, `data/uploads/`, `credentials/`, `.env` 以及**真实源工作簿文件**。
    *   `config/settings.py`: 定义数据库文件路径等。
    *   `app.py`: 最小化的 Streamlit 应用骨架。
*   **完成标准**:
    *   `py -0p` 确认 Python 3.11 已存在，并使用 `py -3.11 -m venv .venv` 成功创建虚拟环境。
    *   环境可通过 `pip install -r requirements.txt` 成功安装。
    *   `.gitignore` 配置正确，确保真实源文件与本地数据库不会被提交。

### 任务 3: 数据模型与重复识别

*   **描述**: 定义 SQLite `opportunities` 表结构，并实现按 `record_type` 差异化的去重逻辑。
*   **输入**:
    *   [DATA_MODEL.md](DATA_MODEL.md) 中定义的 `opportunities` 表结构。
*   **输出**:
    *   `database/db_handler.py`: 实现数据库连接、`opportunities` 表初始化。`record_type` 列加 CHECK 约束，只允许 `campaign` / `job`。
    *   `services/dedup_service.py`: 仅对入库记录（`campaign` / `job`）计算 `dedupe_key`（两套规则）；`unknown` **不生成最终 `dedupe_key`**，只用临时 `preview_id`（或 `source_sheet` + `source_row`）做预览阶段识别。
    *   `tests/test_dedup_service.py`: 单元测试。
*   **完成标准**:
    *   `opportunities` 表能被正确创建，`record_type` CHECK 约束生效（写入 `unknown` 应失败）。
    *   给定 campaign / job 记录，能分别生成正确的 `dedupe_key`。
    *   重复记录生成相同 `dedupe_key`；`unknown` 记录不生成 `dedupe_key`，标记为待确认，不入库。

### 任务 4: XLSX/CSV 解析与布局识别（解析器开发）

*   **描述**: 实现按工作表选择解析器与布局识别功能，**不强行推断未知布局**。
*   **输入**:
    *   [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md) 的布局判定规则与各工作表列映射（已按 [WORKBOOK_PROFILE.md](WORKBOOK_PROFILE.md) 实证修订）。
    *   **完全虚构的原始布局测试夹具**（见输出），真实数据不得复制到夹具。
*   **输出**:
    *   `services/opportunity_importer.py`（通用导入器，**不得命名为 `zhilian_importer`**）。
    *   `services/layout_detector.py`: 按工作表选择解析策略；中国大陆新版/旧版/unknown 判定（F 为招聘类型关键词 & G 为届次 -> campaign；F 为岗位名称 & G 为城市 -> job）；中国香港标准/错位布局判定；美国 F/G 交换检测；英国/新加坡 G 歧义子集标记 unknown。
    *   `parse_workbook(path)` / `parse_sheet(sheet)`: 返回标准化机会列表 + 布局标签。
    *   `tests/test_opportunity_importer.py`: 单元测试。
    *   **原始布局测试夹具** `tests/fixtures/`：**完全虚构**的原始工作簿/CSV，覆盖：
        *   中国大陆 campaign 布局（F=招聘类型关键词 + G=届次）；
        *   中国大陆 job 布局（F=岗位名称 + G=城市）；
        *   中国香港确认后的标准布局与错位布局；
        *   美国确认后的标准布局与 F/G 交换布局；
        *   英国/新加坡少量 G 歧义行；
        *   缺字段、重复记录、无效 URL（非 http 取值）；
        *   XLSX 多工作表输入 与 CSV 单表输入 两种格式。
*   **完成标准**:
    *   能正确解析原始布局夹具，覆盖上述全部布局与边界。
    *   能正确识别中国大陆新版/旧版、香港标准/错位、美国 F/G 交换。
    *   对无法判定的记录标记为 `unknown` 并保留 `raw_data`，不强行推断。
    *   XLSX 多工作表与 CSV 单表两种输入均能解析。
    *   夹具中无任何真实公司数据或真实链接。

> 注：`data/sample/sample_opportunities.csv` 是**标准化后的输出样本**，只用于数据模型/去重/数据库测试（任务 3、6、9），**不能**用于验证原始 XLSX 布局识别；原始布局识别必须由本任务的虚构原始夹具覆盖。

### 任务 5: 机会导入预览与导入报告

*   **描述**: 开发导入页面的核心逻辑，实现导入前的预览（含人工确认）与导入后的统计报告。
*   **输入**:
    *   用户上传的工作簿。
*   **输出**:
    *   `pages/import_page.py`:
        *   工作表选择组件。
        *   布局识别结果展示。
        *   字段映射下拉菜单（对 unknown 记录由人工确认 `record_type` 与映射）。
        *   数据预览表格。
        *   导入报告：**新增 / 重复 / 无效 / 待人工确认** 数量。
    *   `services/opportunity_service.py`: `import_opportunities()` 方法。
*   **完成标准**:
    *   用户可选择工作表并查看布局识别结果。
    *   对 unknown 记录，用户可在预览阶段人工确认记录类型与字段映射；**确认改为 `campaign`/`job` 后才入库**。
    *   导入前清晰展示四类统计数量；导入后重复记录、未确认及无效记录**均不写入数据库**（`opportunities.record_type` 只存 `campaign`/`job`）。

### 任务 6: 候选清单与公司机会数量检查

*   **描述**: 实现“公司覆盖检查”视图，按公司返回全部已排序机会并默认突出前 3 个（不截断），标注只有 campaign 的公司。
*   **输入**:
    *   数据库中的机会数据。
*   **输出**:
    *   `services/candidate_service.py`:
        *   `get_company_coverage()`: 按公司返回**全部已排序机会**（campaign + job 合计，按 `priority` 排序，job 优先于 campaign），同时返回：
            *   `total_count`: 该公司候选机会总数（可 > 3，不设上限）；
            *   `coverage_gap`: 不足 3 个时的缺口数（如 0 则表示已满 3 个及以上）；
            *   `highlighted_top_three`: 按优先级默认突出的前 3 个机会（**仅重点展示，非截断**，其余机会仍返回给前端展示）；
            *   `campaign_only`: 布尔值，该公司是否只有 campaign、没有 job。
        *   `mark_campaign_only_companies()`: 对只有 campaign、没有 job 的公司标注“需进入官网选择具体岗位”。
    *   `tests/test_candidate_service.py`: 单元测试。
*   **完成标准**:
    *   能正确按公司对机会分组。
    *   对每家公司返回**全部机会**（非仅 Top 3），并按 `priority` 排序、job 优先于 campaign。
    *   `highlighted_top_three` 为前 3 个，但全量机会仍可被前端访问，不得因“前 3”隐藏其他机会。
    *   机会少于 3 个的公司返回 `coverage_gap` 缺口数（非“数量不足”截断）。
    *   只有 campaign 的公司返回 `campaign_only=True` 与“需进入官网选择具体岗位”标记。

### 任务 7: 机会看板与筛选

*   **描述**: 开发核心的看板页面，支持全量浏览和候选清单视图，**campaign 与 job 视觉区分**。
*   **输入**:
    *   数据库中的机会数据。
*   **输出**:
    *   `pages/dashboard.py`:
        *   **侧栏筛选器**: 支持按公司、地区、记录类型(campaign/job)、状态等筛选。
        *   **主内容区**: 提供“全量视图”和“候选清单视图”的切换。
        *   **机会卡片**: 展示机会信息及操作按钮，campaign/job 用不同标签/颜色区分。
*   **完成标准**:
    *   页面能正确加载并展示所有机会。
    *   campaign 与 job 视觉区分清晰。
    *   筛选器工作正常。
    *   “候选清单视图”能正确展示数据，并对机会不足或只有 campaign 的公司给出提示。

### 任务 8: 链接打开和状态确认

*   **描述**: 实现状态流转逻辑，严格区分“自动打开链接”和“手动确认投递”。
*   **输入**:
    *   用户在 UI 上的操作。
*   **输出**:
    *   `services/opportunity_service.py`:
        *   `mark_as_opened(opp_id)`: 更新状态为 `opened`（不得覆盖更高级状态）。campaign 打开 `application_url` 或回退 `announcement_url`；job 打开 `application_url`。
        *   `confirm_applied(opp_id)`: 必须由用户手动调用，更新状态为 `applied`。
        *   其他状态更新方法。
    *   `pages/dashboard.py`: 在机会卡片中集成这些操作按钮。
*   **完成标准**:
    *   点击“投递”按钮能打开正确链接，并将状态更新为 `opened`（仅当状态低于 `opened` 时）。
    *   必须通过专门的“确认已投递”按钮才能将状态更新为 `applied`。

### 任务 9: 持久化与集成测试

*   **描述**: 确保所有数据变更能正确持久化，并完成端到端的流程测试。
*   **输入**:
    *   已完成的所有模块。
*   **输出**:
    *   `tests/test_integration.py`: 端到端测试用例。
*   **完成标准**:
    *   **持久化**: 应用重启后，所有状态变更、优先级变更依然保留。
    *   **集成测试**: 能模拟完整的用户操作流程：
        1. 启动应用 -> 选择工作表 -> 导入（含 unknown 人工确认） -> 查看导入报告。
        2. 切换到看板 -> 在候选清单视图中查看机会（campaign/job 区分）。
        3. 点击“投递” -> 状态变为 `opened`。
        4. 手动点击“确认已投递” -> 状态变为 `applied`。
        5. 重启应用 -> 验证状态依然存在。
    *   所有单元测试和集成测试均通过。

### 任务 10: 开源文档、许可证与 GitHub Actions

*   **描述**: 完成开源发布前的准备工作。
*   **输入**:
    *   完成开发的项目。
*   **输出**:
    *   `README.md`: 项目说明、安装指南、使用方法。
    *   `LICENSE`: MIT 许可证文件。
    *   `data/sample/sample_opportunities.csv`: 作为示例数据的匿名机会文件（已在任务 1 中准备）。
    *   `.github/workflows/ci.yml`: GitHub Actions 配置，自动运行 `pytest`。
    *   进行一次**隐私与凭据泄露检查**（确保 `.gitignore` 有效，真实源工作簿未入库）。
    *   在文档中**明确说明**：不得复制无许可证的第三方仓库代码；如后续引入 AGPL 许可组件，需重新评估项目整体许可证。
*   **完成标准**:
    *   `README.md` 内容清晰，能指导新用户完成从安装到使用的全过程。
    *   GitHub Actions 配置正确，PR 提交时能自动触发测试。
    *   确认项目中无任何真实源数据、个人隐私数据或违规代码。
