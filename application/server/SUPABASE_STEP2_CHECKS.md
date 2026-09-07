# Supabase 阶段 2 本轮检查记录

日期：2026-09-06。范围仅为 Windows 桌面云端升级的后端准备，不改桌面 v1.1.0 成品、安卓或网站。

本文是 2026-09-06 的历史检查快照，不代表最新状态。2026-09-07 已完成数据库连接、私有试点
迁移及低权限账号联调，参见 [阶段 3 验收记录](SUPABASE_STEP3_CHECKS.md)。以下保留当时结果供追溯。

## 当前真实状态

- 用户确认项目：`https://rvtjveplqnelgwnpuqtn.supabase.co`。
- 用户进一步提供的 Direct URI 模板与该项目匹配，仍为 `[YOUR-PASSWORD]` 占位符，没有提供真实密码。
- 公开 DNS/HTTPS 网关检查成功；1 次无 Key/Authorization 的 `/rest/v1/` 请求返回 401。
- 本机加密凭据尚不存在，PostgreSQL 登录、TLS 握手和真实迁移尚未验证。
- 没有创建或修改云端表，没有上传 SQLite、健康记录或 AI Key，没有部署 Render。

## 已完成代码与验证

- `cloud_connection.py`：绑定项目、隐藏输入、连接参数白名单、严格 TLS、只读事务及强制回滚、错误脱敏。
- `local_secret_store.py`：Windows 当前用户 DPAPI；只有密文落盘；覆盖须明确；链接/联接/重解析点及畸形 JSON 拒绝。
- `medical_app_private` 私有 schema、四张 pilot 表和 Alembic 初始迁移；应用启动不建表。
- PostgreSQL 禁止走本地 `init-db`；在线迁移需要独立管理凭据、匹配项目及显式确认。
- 空表回滚保护：锁定、RLS 防隐藏行、未知对象拒绝；只用 RESTRICT，不删除 schema，不用 DDL CASCADE。
- 最终完整后端回归：**187 项通过，0 失败、0 错误、0 跳过，13.089 秒**。
- Ruff 和独立后端环境 `pip check` 通过。测试依赖有两项上游弃用提示，未影响结果。
- 原 Windows 包内 48 个项目模块与当前桌面源码仍一致，确认本轮未修改桌面实现。

证据：`../outputs/supabase-step2-tests.xml`。
离线审核 SQL：`../outputs/cloud-pilot-sql-20260906/`，分别明确标记 REVIEW_ONLY 与 EXPLICIT_ONLY。
这些测试使用临时 SQLite、合成凭据、模拟网络和离线 PostgreSQL 方言，不等于真实 PostgreSQL/权限/迁移验收。

## 下一项必要输入

按 `SUPABASE_NEXT_STEP.md` 在本机配置 Connect 页的 Session pooler URI 和真实数据库密码；
也支持项目 Direct URI，但直连网络/证书要求须实际通过。不要把密码发入聊天。
工具检查成功后，下一步才能核查真实数据库现状、准备最小权限运行角色并执行确认的测试迁移。
