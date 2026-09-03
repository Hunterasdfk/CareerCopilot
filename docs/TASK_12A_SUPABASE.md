# 任务 12A：Supabase 云数据基础

## 状态

任务 12A 已在 CareerCopilot 的 Supabase 项目中执行，并以可复现的 SQL
迁移同步到本仓库。云端安全顾问检查结果为 0 项安全告警。

本任务只建立云端数据与权限基础，不代表 Streamlit 已经切换到 Supabase。
应用登录、客户端接入、Excel 云端导入和新的“申请记录”页面应在后续任务中
分别实现和测试。

## 数据边界

| 表 | 用途 | 访问边界 |
| :--- | :--- | :--- |
| `profiles` | 用户资料 | 用户只能读写自己的资料 |
| `import_batches` | Excel 导入批次与统计 | 仅管理员可管理 |
| `opportunities` | 全局岗位与招聘项目目录 | 登录用户可读，管理员可写 |
| `applications` | 用户申请的公司、岗位和当前步骤 | 用户只能管理自己的记录 |
| `application_events` | 申请流程时间线 | 用户只能读取和追加自己的事件 |

`applications.current_stage` 使用受长度约束的自由文本，而不是固定流程枚举。
这允许用户按不同公司的实际流程填写“简历筛选”“技术一面”“HR 面”等步骤；
稳定的 `status` 字段仍用于跨公司的统一筛选。

`application_events` 是追加式时间线。复合外键
`(application_id, user_id)` 强制事件和申请属于同一用户，避免只靠 RLS
检查事件行时产生跨用户关联。

## 迁移文件

1. `20260903022752_task_12a_cloud_data_foundation.sql`
   创建 5 张表、约束、索引、最小权限、RLS 策略和更新时间触发器。
2. `20260903022905_task_12a_cover_application_event_owner_fk.sql`
   增加申请人与事件所有者的一致性复合外键。

文件名与云端迁移历史一致。新的 Supabase 环境可使用官方 CLI 按顺序执行：

```bash
supabase link --project-ref <your-project-ref>
supabase db push
```

不要把数据库密码、secret/service-role key、用户 Token 或真实 Excel 文件加入
Git。Streamlit 客户端应使用 publishable key，并依靠登录用户 JWT 与 RLS 隔离数据；
管理员导入应使用服务端受保护的执行路径。

## 后续任务边界

- Supabase Auth 登录与会话恢复；
- Streamlit 数据访问层由 SQLite 切换为 Supabase；
- 将确认后的 Excel 记录写入 `opportunities`，记录 `import_batches`；
- 申请记录页面：公司、岗位、统一状态、自定义当前步骤、下一步行动和时间线；
- 多账户端到端验证：A 用户不能读取或修改 B 用户数据；
- 部署配置仅写入 Streamlit Secrets，不进入仓库。
