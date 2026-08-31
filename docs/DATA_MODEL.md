# CareerCopilot 数据模型设计文档 (DATA_MODEL)

## 1. 机会字段 (Opportunity Fields)

本系统的核心数据表，存储从外部工作簿导入的**机会记录（Opportunity）**。一条机会可能是校招项目/招聘公告（`campaign`），也可能是具体岗位（`job`）。用户的管理状态（优先级、投递状态等）也记录于此表。

*   **表名**: `opportunities`（原 `jobs` 表重命名，以准确表达两类记录）

| 字段名 (Column) | 类型 (Type) | 约束 (Constraints) | 描述 (Description) |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PRIMARY KEY, AUTOINCREMENT | 系统内部唯一标识，自增 ID |
| `record_type` | String(20) | NOT NULL, CHECK IN (`campaign`,`job`) | 记录类型：只允许 `campaign` / `job`。`unknown` 不入库（仅在解析结果与导入预览阶段存在，见 §5、§7） |
| `display_title` | String(200) | NOT NULL | 展示标题。campaign 取公告名称，job 取岗位名称，用于列表显示 |
| `job_title` | String(200) | | 具体岗位名称。仅 `job` 记录必填，`campaign` 记录为空 |
| `job_categories` | String(200) | | 岗位类别（可空），如“研发/后端” |
| `company_name` | String(100) | NOT NULL | 企业名称 |
| `industry` | String(100) | | 所属行业（可空） |
| `recruitment_type` | String(50) | | 招聘类型（可空），如“秋招全职/春招全职/日常实习/暑期实习” |
| `target_cohort` | String(50) | | 招聘届次（可空），如“2026届” |
| `education_requirement` | String(50) | | 学历要求（可空） |
| `location` | String(100) | | 工作城市/地点（可空） |
| `deadline` | DateTime | | 投递截止时间（可空） |
| `announcement_title` | String(200) | | 公告名称（可空） |
| `announcement_url` | String(500) | | 公告链接（可空） |
| `application_url` | String(500) | | 投递链接（可空）。不再设 UNIQUE，因同公司多岗位可能共用校招入口 |
| `source_sheet` | String(50) | NOT NULL | 来源工作表名（如“中国大陆”） |
| `source_row` | Integer | NOT NULL | 来源行号（原 XLSX/CSV 物理行号，1-based，表头为第 1 行，第一条数据为第 2 行；空行占用行号但不输出记录），便于回溯原始数据 |
| `import_batch_id` | String(50) | | 导入批次 ID，用于追溯一次导入操作 |
| `dedupe_key` | String(100) | UNIQUE, NOT NULL | 去重唯一键，规则见 §7 |
| `raw_data` | Text | | 未映射的整行原始数据（JSON 文本），便于核对与回溯 |
| `priority` | String(20) | DEFAULT 'low' | 用户标记的优先级 (high/medium/low) |
| `status` | String(20) | DEFAULT 'discovered' | 投递状态，见 §3 |
| `notes` | Text | | 用户添加的备注信息 |
| `opened_at` | DateTime | | 用户点击链接打开投递页面的时间 |
| `applied_at` | DateTime | | 用户手动确认已投递的时间 |
| `created_at` | DateTime | DEFAULT CURRENT_TIMESTAMP | 记录创建时间（通常是导入时间） |
| `updated_at` | DateTime | DEFAULT CURRENT_TIMESTAMP | 记录最后更新时间 |

> 说明：详细 JD 全文不再设独立顶层字段，MVP 0.1 阶段保留在 `raw_data` 中；MVP 0.2 引入简历匹配时，再考虑提升为顶层字段 `job_description` 以便做文本相似度计算。

## 2. 投递记录字段 (Application/Status Record)

*   **说明**: MVP 0.1 阶段，所有状态信息均存储在 `opportunities` 表中。不建立独立的 `applications` 表或状态历史表，以简化数据结构。
*   **后续扩展**: 在 MVP 0.2+ 版本中，可考虑拆分出独立的 `status_logs` 表，以支持状态变更历史的完整记录。

## 3. 状态枚举 (Status Enums)

### 3.1 投递状态 (`status`)

定义机会的完整生命周期状态，严格区分“自动跳转”与“人工确认”。

| 枚举值 (Value) | 显示名 (Display Name) | 描述 (Description) |
| :--- | :--- | :--- |
| `discovered` | 已导入 | 机会已成功导入系统，初始状态 |
| `shortlisted` | 已入选候选清单 | 用户手动将其加入重点关注名单 |
| `opened` | 已打开投递页面 | 用户点击“投递”按钮，系统打开链接。**此状态由系统自动设置，仅表示页面被打开。** |
| `applying` | 正在填写 | 用户在填写表单过程中（可选状态） |
| `applied` | 已投递 | **必须由用户手动点击“确认已投递”按钮才能变更至此状态。** |
| `assessment` | 笔试/测评 | 用户收到在线测评或笔试邀请 |
| `interview` | 面试阶段 | 用户进入面试阶段 |
| `offer` | 获得 Offer | 用户获得正式 Offer |
| `rejected` | 企业拒绝 | 用户被企业明确拒绝 |
| `withdrawn` | 主动放弃 | 用户主动选择放弃该机会 |

### 3.2 状态流转规则 (State Transition Rules)

*   **自动流转**：
    *   点击“投递”按钮 -> `discovered` / `shortlisted` -> `opened`
    *   **重要**：点击按钮**仅**能将状态更新为 `opened`。如果用户已经处于更高级的状态（如 `applied`），点击按钮**不得**将其回退为 `opened`。
*   **手动流转**：
    *   `applied` 状态必须通过专门的“确认已投递”按钮手动触发。
    *   其他所有状态（assessment, interview, offer, rejected, withdrawn）均需用户手动更新。

> 注：`campaign` 记录点击“投递”会打开 `application_url`（或回退到 `announcement_url`），同样只置为 `opened`，不自动 `applied`。

### 3.3 优先级 (`priority`)

用于在“候选清单视图”中对机会进行分组和排序。

| 枚举值 (Value) | 显示名 (Display Name) | 描述 (Description) |
| :--- | :--- | :--- |
| `high` | 高 | 用户最感兴趣的机会 |
| `medium` | 中 | 有一定意向的机会 |
| `low` | 低 | 暂不优先考虑或作为保底的机会 |

## 4. 字段是否必填 (Field Requirements)

*   **导入时必填 (Required on Import)**:
    *   `record_type`（记录类型：解析/预览阶段可为 `unknown`；**写入数据库时必须为 `campaign` 或 `job`**，`unknown` 不入库）
    *   `display_title`（展示标题）
    *   `company_name`（企业名称）
    *   `source_sheet` + `source_row`（来源追溯）
    *   *至少存在 `announcement_url` 或 `application_url` 之一*，否则记为无效。

*   **导入时选填 (Optional on Import)**:
    *   `job_title`（仅 `job` 记录有值）
    *   `job_categories` / `industry` / `recruitment_type` / `target_cohort`
    *   `education_requirement` / `location` / `deadline`
    *   `announcement_title`

*   **系统自动填充 (System Defaults)**:
    *   `id`: 数据库自动生成。
    *   `dedupe_key`: 系统根据规则自动生成。
    *   `import_batch_id`: 系统为每次导入操作生成唯一 ID。
    *   `raw_data`: 系统自动保存整行原始数据。
    *   `priority`: 默认为 `low`。
    *   `status`: 默认为 `discovered`。
    *   `created_at` / `updated_at`: 系统自动记录当前时间。

## 5. XLSX 字段兼容方案

鉴于源工作簿存在多工作表、多历史布局（详见 [SOURCE_SCHEMA.md](SOURCE_SCHEMA.md)），系统采用**按工作表选择解析器 + 运行时字段映射**双重兼容机制：

1.  **标准 Schema**: 系统内部定义上述 `opportunities` 表字段为标准 Schema。
2.  **按工作表分发**: 导入器根据 `source_sheet` 选择对应的列布局解析器（如中国大陆 sheet 需区分新版/旧版）。
3.  **运行时映射**: 对无法自动判定的列，用户在导入预览阶段通过下拉菜单完成表头到标准 Schema 的映射。
4.  **未知列处理策略**: 未被映射的列**不强制入库**，但**必须保留在 `raw_data`（JSON）中**，便于后续回溯。MVP 0.1 不设计动态扩展表。
5.  **候选布局临时结构**: 香港/美国候选布局在解析阶段输出 `record_type=unknown` + `needs_confirmation=True` + `suggested_record_type`（可为 `None`）+ `suggested_fields`（暂定映射，仅为建议，不得入库）+ 完整 `raw_data`。顶层不写最终业务字段。详见 [ARCHITECTURE.md §6.1](ARCHITECTURE.md#61-解析阶段临时结构候选布局)。

## 6. 候选清单排序规则 (Tie-breaking)

当同一家公司存在多个机会具有相同 `priority` 值时，为保证候选清单视图的结果确定性，系统按以下优先级进行次级排序：
1.  `record_type` 为 `job` 的优先于 `campaign`（具体岗位优先于统一入口）。
2.  `id` 升序（即先导入的优先）。

> 该排序用于“默认突出前 3 个”的展示，**不截断结果**：系统始终保留并返回该公司的全部机会，候选清单可超过 3 个，不设上限。

## 7. 去重逻辑 (Deduplication Logic)

为避免重复导入，系统在导入前会根据 `record_type` 计算差异化的 `dedupe_key`。

*   **`dedupe_key` 生成规则（仅对入库记录）**:
    1.  **campaign 记录**：`company_name` + `recruitment_type` + `target_cohort` + `announcement_url`（无 URL 时回退到 `announcement_title`）。
    2.  **job 记录**：`company_name` + `job_title` + `location` + `application_url`（无 URL 时回退到 `job_categories`）。
    3.  **unknown 记录**：**不生成最终数据库的 `dedupe_key`**。`unknown` 只存在于解析结果与导入预览阶段，使用临时 `preview_id`（或 `source_sheet` + `source_row`）进行预览阶段识别；用户确认改为 `campaign`/`job` 后才按上述规则生成 `dedupe_key` 并入库。未确认或无效的 `unknown` 记录不进入数据库。
*   **导入报告**:
    *   导入操作将统计并向用户报告：**新增机会数**、**重复机会数**、**无效机会数**、**待人工确认数**。
    *   重复的记录将被跳过，不写入数据库。

## 8. 示例匿名数据 (Sample Anonymous Data)

以下为两条示例数据（campaign 与 job 各一条），均使用虚构素材：

```sql
-- campaign 记录示例
INSERT INTO opportunities (record_type, display_title, company_name, recruitment_type, target_cohort, education_requirement, location, deadline, announcement_title, announcement_url, application_url, source_sheet, source_row, dedupe_key, status, priority)
VALUES (
    'campaign',
    '示例科技A 2026 秋季校园招聘',
    '示例科技A',
    '秋招全职',
    '2026届',
    '本科及以上',
    '北京/上海/深圳',
    '2026-10-31 23:59:00',
    '示例科技A 2026 秋季校园招聘',
    'https://example.com/announcement/a-2026',
    'https://example.com/apply/a-2026',
    '中国大陆', 128, 'hash_campaign_a_2026',
    'opened', 'high'
);

-- job 记录示例
INSERT INTO opportunities (record_type, display_title, job_title, job_categories, company_name, recruitment_type, target_cohort, education_requirement, location, deadline, announcement_title, announcement_url, application_url, source_sheet, source_row, dedupe_key, status, priority)
VALUES (
    'job',
    '示例后端开发工程师',
    '示例后端开发工程师',
    '研发/后端',
    '示例制造B',
    '秋招全职',
    '2026届',
    '硕士',
    '上海市浦东新区',
    '2026-09-30 23:59:00',
    '示例制造B 2026 秋招',
    'https://example.com/announcement/b-2026',
    'https://example.com/apply/b-2026/4567',
    '中国大陆', 256, 'hash_job_b_2026_4567',
    'discovered', 'medium'
);
```
