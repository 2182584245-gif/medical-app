# 平台私有数据升级前备份与隔离恢复验证

`python -m server.platform_backup` 是管理员工具，不属于桌面发布包。它没有云端恢复、迁移或授权入口，不能代替另外审批的升级操作。

## 固定边界

- 源连接仅从既有 Windows 当前用户 DPAPI 管理配置加载到内存，校验项目归属、管理员身份、5432 直连/会话池和 `verify-full` 可信证书。
- 导出仅包含 `medical_app_platform` 和独立的 `medical_app_private.alembic_version` 表。不会导出 Supabase 的 `public`、`auth`、`storage` 或其全实例角色密码。
- 使用只读、可重复读事务导出一个 PostgreSQL snapshot；源表计数、目录定义和两次 `pg_dump --snapshot` 共用该 snapshot。不暂停线上写入、不修改云端数据库参数。
- 固定使用已在本机缓存的官方 PostgreSQL 17 镜像摘要，`--pull=never`，不下载其他工具。
- 数据库密码只通过匿名 stdin 交给容器内 `read`/`export`，不进入参数、Docker 配置环境变量、明文文件或日志。CA 为只读挂载。导出容器禁日志、只读文件系统、移除 capabilities。
- 自定义格式归档仅在内存中捕获；序列化后立即用专属用途的当前用户 DPAPI 加密。密文以独占新文件写入 `%LOCALAPPDATA%\HealthLife\private-backups\`，禁止覆盖现有文件或保存在仓库内。
- 验证首先重新读取并解密实际落盘的文件，再在新建的唯一标记容器中恢复。恢复容器无网络、无发布端口、无宿主文件/数据卷，PGDATA 为 tmpfs、禁用 Docker 日志和 SQL/错误语句日志；不向容器提供任何云端密码。
- 恢复使用 `pg_restore --single-transaction --exit-on-error`，保留所有者、ACL、RLS 和约束。所需角色只在一次性本地库中以 NOLOGIN/NOINHERIT/NOBYPASSRLS 创建；不复制其凭据。
- 逐表核对同一源快照的行数，并核对列、约束有效性、索引、RLS、策略、ACL 和 Alembic 版本。检查完成后，只有 ID、唯一标签、镜像和隔离属性均符合本次创建记录的容器才会被删除。
- 目录与 ACL 的核对范围仅为平台及版本表的关系、平台 schema/default ACL，不是整个集群的全局权限。当前受控平台没有列级授权、数据库函数或自定义触发器；若出现这些未覆盖对象，工具会在源只读检查时拒绝继续，不会把部分检查宣称为完整权限验证。
- 表 ACL 的 NULL 使用 PostgreSQL `acldefault` 还原正式默认授权后比较；这处理了 `pg_dump` 将显式默认授权恢复为 NULL 的等价表示。空 ACL 不替换为默认值，实际撤权、新增授权或 grant option 差异仍严格拒绝。

## 明确执行门槛

先运行合成测试；测试必须隔离 `LOCALAPPDATA`，不读取实际 DPAPI profile。完整容器测试需要显式 `HEALTHLIFE_RUN_BACKUP_CONTAINER_TEST=1`，仅使用合成表与数据。

真实只读备份命令需要管理员已授权读取相应源项目，并显式确认：

```powershell
.\.venv-server\Scripts\python.exe -m server.platform_backup `
  --profile <已有加密管理配置的绝对路径> `
  --confirm readonly-platform-backup-and-isolated-restore
```

不要把 URL、密码、API Key 或用户内容替换进命令行。成功只输出密文路径、表计数和完整性状态。失败只输出固定安全错误，不显示原始 libpq/Docker/SQL 错误。若导出已加密而恢复验证失败，密文保留，不能视为已经验证的升级恢复点。

对已有密文重新做本机恢复验证，使用 `--verify-file <密文绝对路径>` 代替 `--profile`，并提供相同确认参数。此分支不加载连接配置、不建立云端连接、不重新导出，也不覆盖原密文。

## 限制

这是私有平台的逻辑数据恢复点，不是 Supabase 全实例备份；不包含存储对象、认证系统、全局角色密码、运行配置或 token pepper。DPAPI 通常要求原 Windows 用户和设备；本机管理员或已能以同一用户运行的程序不在其防护范围内。数据在操作系统和进程内存中仍短暂存在，不能承诺对操作系统交换文件或内存转储的防护。

工具采用内存导出，最大接受 128 MiB 的单次进程输出/加密输入；不适合大型生产数据库。任何计数、约束或权限不一致都应停止升级。即使验证成功，未来云端恢复或迁移仍需另外确认目标、所有权及操作授权。

依据：PostgreSQL 17 官方 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html) 的 schema/snapshot/RLS 规则、[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html) 的单事务恢复，以及 Supabase 官方 [平台备份迁移说明](https://supabase.com/docs/guides/self-hosting/restore-from-platform)。本工具刻意不采用全实例导出或文档中的明文文件范例。
