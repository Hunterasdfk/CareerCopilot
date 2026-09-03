# 任务 12B：Supabase 登录、云端导入与申请记录

## 已实现范围

12B 在 12A 的五张表和 RLS 基础上增加：

- Supabase Auth 邮箱注册、登录、会话恢复和退出；
- 云端机会目录读取；
- 云端 Excel 预览（云端 dedupe key 会注入临时内存库，不创建本地 `.db`）；
- 只有点击“确认导入”才创建 `import_batches` 并写入 `opportunities`；
- 独立“申请记录”页面，按用户保存公司、岗位、统一状态、自定义流程步骤、
  下一步行动、备注和追加式时间线；
- 看板在云端模式下仅覆盖当前用户的申请状态，不修改全局机会目录；
- 未配置 Supabase 时继续支持原有 SQLite 离线模式。

## 配置

本地开发建议在项目根目录创建 `.streamlit/secrets.toml`（该文件已被 Git
忽略），内容如下：

```toml
SUPABASE_URL = "https://<project-ref>.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "<publishable-key>"

# 仅用于服务端管理员导入 Excel；不要放到前端、公开仓库或 URL。
SUPABASE_SERVICE_ROLE_KEY = "<service-role-or-secret-key>"
```

也可以设置同名环境变量。`SUPABASE_ANON_KEY` 仍兼容旧项目，但新项目优先
使用 publishable key。页面端只使用 publishable key，service-role key 仅在
Streamlit 服务器点击确认导入时创建管理员 client。

如果只配置前两个值，登录、申请记录和云端机会浏览可用；确认云端 Excel
导入时会提示缺少服务端导入密钥。该限制是为了不把全局 `opportunities` 写入
权限开放给普通用户，也避免在浏览器暴露可绕过 RLS 的密钥。

## 运行

```powershell
cd "E:\Graduation Season\CareerCopilot"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

首次使用：

1. 在侧栏“云端账户”注册并完成 Supabase 邮箱确认；
2. 登录后从“机会导入”上传 XLSX/CSV，完成 unknown 记录确认；
3. 点击“确认导入”，导入批次统计和合格机会写入云端；
4. 在“机会看板”点击打开链接或确认投递，状态写入当前用户的
   `applications`；
5. 在“申请记录”中按公司查看岗位，手动填写每家公司独有的流程步骤和时间线。

## 数据与安全边界

- `opportunities` 是全局目录；`applications` / `application_events` 是用户私有
  数据，所有读取和修改依靠 `auth.uid()` + RLS；
- 原始 Excel 不上传 Storage，只写入文件名、SHA-256 和导入统计，原始行仅在
  合格机会的 `raw_data` JSON 中保留；
- unknown、invalid、duplicate 记录不会写入云端 `opportunities`；
- 不自动打开招聘网站检测流程，不自动提交申请；流程步骤由用户手动维护；
- 绝不使用 `user_metadata` 做授权判断，绝不把 service-role/secret key 写入
  Git、日志、页面或客户端。

## 验证

仓库测试不连接生产 Supabase，使用 fake client 和临时 SQLite/内存对象验证：

- 配置读取、密钥边界和登录输入校验；
- RLS 访问层的表/字段白名单；
- 云端导入只写 `new`，并记录 SHA-256 批次统计；
- 用户申请状态、自由文本流程步骤和时间线事件；
- UUID 机会 ID 与旧 SQLite 整数 ID 均可在看板排序。

当前云端项目只读核验结果：12A 的两条迁移已存在，`profiles`、
`import_batches`、`opportunities`、`applications`、`application_events` 五张
表均已启用 RLS；安全顾问无告警。性能顾问报告的未使用索引属于空库上的信息性
提示，待产生真实查询负载后再评估是否调整。
