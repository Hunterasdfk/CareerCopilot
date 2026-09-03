# CareerCopilot

> 面向中国大学生秋招 / 春招场景的开源求职机会管理助手。
> MVP 0.1：岗位导入 → 布局识别 → 去重入库 → 看板浏览 → 公司覆盖检查 → 投递状态追踪。
> 任务 12B：可选 Supabase 登录、云端机会目录与按用户隔离的申请记录。

## 目录

- [项目简介](#项目简介)
- [核心功能](#核心功能)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [使用流程](#使用流程)
- [示例数据](#示例数据)
- [数据安全与隐私](#数据安全与隐私)
- [许可证与第三方代码](#许可证与第三方代码)
- [路线图](#路线图)
- [贡献](#贡献)

---

## 项目简介

CareerCopilot 是一个本地运行的求职机会管理工具，帮助应届毕业生管理覆盖多地区、多来源的求职机会数据。MVP 0.1 聚焦于构建一个轻量级的机会管理看板，解决以下核心问题：

- **信息过载与多布局兼容**：源数据覆盖多地区、多来源，且同一工作表存在历史布局变体，CareerCopilot 按工作表分发解析器，自动识别布局，对无法判定的记录保留原始数据并交由人工确认。
- **项目入口与具体岗位混淆**：数据中既有校招项目 / 统一入口（campaign），也有具体岗位（job）。CareerCopilot 在界面上明确区分二者：campaign 需进入官网选择具体岗位，job 可直接投递。
- **状态管理混乱**：严格区分“打开链接”（opened）与“确认已投递”（applied），避免误判投递进度。
- **公司覆盖度不明确**：按公司返回全部机会并默认突出前 3 个（不截断、不设上限），机会少于 3 个时显示数量缺口。
- **申请记录易丢失**：登录 Supabase 后，申请公司/岗位、统一状态、自定义流程步骤、下一步行动和时间线保存到云端。

## 核心功能

| 功能 | 说明 |
| :--- | :--- |
| 多工作表导入 | 支持 XLSX / CSV，按工作表选择解析器，自动识别布局（中国大陆新版 / 旧版 / 香港标准 / 错位等）。 |
| 人工确认 | 对无法可靠判定的记录保留 `raw_data`，在导入预览阶段由用户人工确认记录类型与字段映射后才入库。 |
| 导入报告 | 导入前清晰展示新增、重复、无效、待人工确认四类数量。 |
| 去重 | 按 `record_type` 差异化计算 `dedupe_key`，同一文件重复导入不会创建重复机会。 |
| 机会看板 | 全量浏览与候选清单视图切换，campaign / job 视觉区分。 |
| 公司覆盖检查 | 按公司返回全部已排序机会，默认突出前 3 个（不截断）；不足 3 个显示缺口；只有 campaign 的公司提示“需进入官网选择具体岗位”。 |
| 状态追踪 | 完整状态流转（discovered → shortlisted → opened → applying → applied → assessment → interview → offer → rejected → withdrawn）。 |
| 持久化 | 本地 SQLite 数据库存储，应用重启后所有状态与优先级变更依然保留。 |
| 云端申请记录 | Supabase Auth + RLS；每个账户只读取/修改自己的申请记录和时间线。 |

## 技术栈

| 类别 | 选型 |
| :--- | :--- |
| 核心语言 | Python 3.11 |
| 前端框架 | Streamlit (≥ 1.28) |
| 数据库 | 本地 SQLite（离线兼容）+ Supabase Postgres（登录后的云端模式） |
| 数据处理 | pandas, openpyxl |
| 测试框架 | pytest |

后续版本（MVP 0.2 / 0.3）计划引入 Playwright、Gmail API、Apprise，均不在 MVP 0.1 范围。

## 项目结构

```
CareerCopilot/
├── app.py                      # Streamlit 主入口（Home 页）
├── requirements.txt            # 核心依赖
├── conftest.py                 # pytest 全局夹具
├── config/
│   └── settings.py             # 全局配置（应用标题、数据库路径等）
├── database/
│   └── db_handler.py           # SQLite 连接与表初始化
├── services/
│   ├── opportunity_importer.py # 通用机会导入器（解析、布局识别）
│   ├── layout_detector.py      # 按工作表选择解析策略
│   ├── dedup_service.py        # 按 record_type 差异化去重
│   ├── opportunity_service.py  # 增删改查与状态流转
│   ├── candidate_service.py    # 公司覆盖检查
│   └── supabase_service.py     # Supabase Auth、云端机会与申请记录
├── pages/
│   ├── import_page.py          # 机会导入页面
│   ├── dashboard.py            # 机会看板页面
│   └── applications.py         # 按用户隔离的申请记录与时间线
├── components/
│   ├── opportunity_card.py     # 机会卡片组件
│   └── filters.py              # 筛选器组件
├── tests/
│   ├── fixtures/               # 完全虚构的原始布局测试夹具
│   ├── test_integration.py     # 端到端集成测试
│   └── ...                     # 各模块单元测试
├── data/
│   └── sample/                 # 虚构样本数据（可提交）
└── docs/                       # 产品规格、架构、数据模型等文档
```

## 环境要求

- **Python 3.11**（必须，不兼容更低版本）
- 操作系统：Windows / macOS / Linux 均可
- 无需独立数据库服务（SQLite 由 Python 标准库提供）

## 快速开始

### 1. 克隆仓库

```bash
git clone <仓库地址>
cd CareerCopilot
```

### 2. 确认 Python 版本

```bash
python --version
# 必须输出 Python 3.11.x
```

### 3. 创建并激活虚拟环境

```bash
# Windows（PowerShell）
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

# macOS / Linux
python3.11 -m venv .venv
source .venv/bin/activate
```

### 4. 安装依赖

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5. 启用云端账户（可选，任务 12B）

复制 `.streamlit/secrets.toml.example` 为 `.streamlit/secrets.toml`，填写
Supabase 项目的 URL 和 publishable key；如需从页面确认云端 Excel 导入，再
填写仅保存在服务器端的 service-role/secret key。该文件已被 Git 忽略，禁止
提交真实密钥。未配置时应用继续使用本地 SQLite 模式。

### 6. 运行测试

```bash
python -m pytest
```

### 7. 启动 Streamlit 应用

```bash
streamlit run app.py
```

浏览器会自动打开本地页面，通过侧边栏访问“机会导入”与“机会看板”页面。

## 使用流程

1. **启动应用**：运行 `streamlit run app.py`。
2. **导入机会表**：上传 XLSX / CSV → 选择工作表 → 查看布局识别结果 → 对 unknown 记录人工确认记录类型与字段映射 → 查看导入报告（新增 / 重复 / 无效 / 待确认）→ 确认导入。
3. **浏览与覆盖检查**：全量视图查看所有机会；候选清单视图查看每家公司全部已排序机会（前 3 个默认突出，不截断；不足 3 个显示缺口；只有 campaign 的公司提示“需进入官网选择具体岗位”）。
4. **投递与状态管理**：点击“投递”按钮 → 浏览器跳转链接，状态变为 `opened`；投递完成后**手动点击“确认已投递”**，状态才变为 `applied`；根据进展手动更新后续状态。
5. **申请记录（云端模式）**：登录 Supabase 后，在“申请记录”页面按公司查看岗位，手动填写每家公司独有的流程步骤、下一步行动和时间线；这些记录按账户隔离，下一次登录仍可读取。

> **重要**：只能导入用户有权使用的 XLSX / CSV 文件。CareerCopilot 不绕过验证码、不实现反检测、不自动捏造求职信息、不默认点击最终提交、不默认自动发送邮件。

## 示例数据

仓库提供以下完全虚构的示例数据，用于测试与演示：

- **`data/sample/sample_opportunities.csv`**：标准化后的输出样本，覆盖 campaign / job / unknown、同一公司 ≥ 3、公司 < 3、缺少字段、重复记录、无效记录等多种边界情况。所有公司名称（示例科技A / 示例制造B / 示例银行C 等）、岗位名称、链接（`https://example.com/...`）均为虚构，不含任何真实公司、真实岗位或真实个人信息。
- **`tests/fixtures/`**：虚构的原始布局 XLSX / CSV 测试夹具，覆盖中国大陆、香港、美国、英国、新加坡等多种布局变体，全部使用虚构公司与 `example.com` 链接。

## 数据安全与隐私

CareerCopilot 高度重视数据隐私。以下原则必须严格遵守：

### 不应提交到 Git 的内容

- **真实源工作簿**：用户真实持有的岗位信息表（如智联、前程无忧等平台导出的 XLSX）必须放在 `data/private/` 目录，该目录已被 `.gitignore` 完整排除。
- **用户上传的原始工作簿**：导入流程临时存放的上传文件位于 `data/uploads/`，已被 `.gitignore` 排除。
- **本地数据库**：所有 `*.db` / `*.sqlite` 文件已被 `.gitignore` 排除。
- **个人信息与凭据**：密码、Token、API Key、真实简历、`credentials*.json`、`token*.json`、`*.pem`、`*.key`、`.env` 等均已被 `.gitignore` 排除。
- **虚拟环境**：`.venv/` 已被 `.gitignore` 排除。

### 可以提交的内容

- 匿名示例数据（`data/sample/`）：必须使用虚构公司、虚构岗位、`example.com` 链接。
- 测试夹具（`tests/fixtures/`）：必须完全虚构，不含任何真实公司或真实链接。
- 源代码、文档、配置文件。

### 数据存放建议

- 真实工作簿放在仓库外，或放在被 `.gitignore` 完整排除的 `data/private/` 目录。
- 定期备份本地数据库与 `data/private/` 目录（仓库不负责保存这些文件）。
- 导入前确认你拥有使用该 XLSX / CSV 文件的权利（自有数据或已获授权）。

## 许可证与第三方代码

### 许可证

本项目采用 [MIT License](LICENSE)。

### 第三方代码使用原则

- **不得复制无许可证的第三方仓库代码**。引入任何第三方代码前，必须确认其许可证允许在 MIT 项目中使用，并保留原始版权与许可证声明。
- **AGPL 兼容性**：若未来引入 AGPL（ Affero General Public License）许可的组件，需重新评估项目整体许可证兼容性。AGPL 与 MIT 不兼容，可能要求整个项目改为 copyleft 许可证，需在引入前进行许可证审查。
- **优先选择许可证清晰的依赖**：优先选择 MIT / BSD / Apache 2.0 等宽松许可证的依赖包。
- **记录第三方依赖**：`requirements.txt` 中列出的依赖均为开源且许可证清晰的包。

## 路线图

详见 [docs/ROADMAP.md](docs/ROADMAP.md)。

| 阶段 | 目标 |
| :--- | :--- |
| **MVP 0.1（当前）** | 岗位管理看板与公司覆盖检查 |
| **MVP 0.2** | 简历解析与智能匹配、Playwright 半自动投递（最终提交前必须由用户确认） |
| **MVP 0.3** | Gmail 邮件获取、招聘邮件分类、重要邮件提醒、回复草稿生成（禁止默认自动发送） |

## 贡献

本项目处于 MVP 0.1 阶段，遵循以下开发原则：

- 每次只完成一个小任务；
- 修改前先说明计划；
- 修改后运行测试；
- 不擅自扩大需求；
- 关键业务逻辑必须有测试；
- 优先采用许可证清晰的依赖；
- 输出和文档以中文为主，代码命名使用英文。

任务拆解与开发进度详见 [docs/TASKS.md](docs/TASKS.md)。
