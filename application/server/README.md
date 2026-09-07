# 服务端基础：已通过 Supabase 私有试点验收的 pilot

目标架构是 **PySide6 桌面 → FastAPI API → PostgreSQL**。截至 2026-09-07，已在用户指定的
Supabase 项目完成严格 TLS 连接、私有 schema 初始迁移、独立低权限运行角色及回滚式 API
试点验收。尚未部署 Render、接入桌面或上传历史用户资料。本目录不是已上线服务。

当前阶段的实际结果和限制见 [阶段 3 验收记录](SUPABASE_STEP3_CHECKS.md)；配置和复查方法见
[Supabase 本机配置说明](SUPABASE_NEXT_STEP.md)。管理员与后端运行凭据分别保存在本机 DPAPI
加密配置中，不要把密码发到聊天中，也不要把 `.local` 复制到源码仓库或桌面发布包。

本阶段只实现用户名密码、可撤销登录令牌、用户隔离的多会话和纯文字消息。
没有迁移真实旧库，没有健康档案、订单、顾问、运营等完整业务表，没有文件上传、
AI 请求代理或桌面接入。后端不接收 `api_key` / `ai_key` 配置字段；未知请求字段直接
拒绝。AI Key 继续由桌面进程保存在内存，不能写入本 API 的消息或模型标签。
任意文字字段无法自动识别所有秘密，调用方仍需避免把密钥作为正文提交。

## 本地运行（PowerShell）

依赖与桌面环境隔离。已有 `.venv-server` 时，在项目根目录执行：

```powershell
$env:PYTHONPATH = $null
.\.venv-server\Scripts\python.exe -m pip install -r requirements-server.txt
$env:SERVER_ENV = 'development'
$env:SERVER_DATABASE_URL = 'sqlite+pysqlite:///./server/local-pilot.db'
$env:SERVER_TOKEN_PEPPER = (& .\.venv-server\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))").Trim()
$env:SERVER_ALLOWED_HOSTS = '["127.0.0.1","localhost"]'
$env:SERVER_REQUIRE_HTTPS = 'false'
.\.venv-server\Scripts\python.exe -m server.cli init-db
.\.venv-server\Scripts\python.exe -m uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8000
```

上述 SQLite 文件是新建的独立开发库，不是桌面真实数据库。建表命令必须显式使用
`development` 环境且仅允许 SQLite，不创建账号或演示数据，也不修改已有表结构。
PostgreSQL 不允许通过 `init-db` 绕过版本化迁移；启动 API 永远不建表。
重新生成 `SERVER_TOKEN_PEPPER` 会使此前所有登录令牌失效；正式环境应通过受控秘密配置
长期保存并制定轮换策略。项目不自动读取 `.env`，也不提供默认生产密钥。

开发文档地址是 `http://127.0.0.1:8000/docs`。停止本地服务按 Ctrl+C。

离线验收只使用临时 SQLite 和合成数据：

```powershell
.\.venv-server\Scripts\python.exe -m pytest -p no:cacheprovider tests/server
```

桌面测试在原 `.venv` 中运行时使用 `--ignore=tests/server`；不要把后端依赖加入桌面打包。

## 配置字段

| 环境变量 | 中文解释与约束 |
| --- | --- |
| `SERVER_ENV` | 必填运行环境：`development`、`test`、`production`。 |
| `SERVER_DATABASE_URL` | 必填 SQLAlchemy 数据库连接。PostgreSQL 使用 `postgresql+psycopg://`。SQLite 仅允许开发/测试。 |
| `SERVER_TOKEN_PEPPER` | 必填独立秘密，随机 32 字节对应的 64 位十六进制文本，用于 HMAC-SHA256 令牌摘要；不是 AI Key。 |
| `SERVER_TOKEN_TTL_SECONDS` | 登录有效期，默认 3600 秒，允许 60–86400 秒；没有自动刷新令牌。 |
| `SERVER_ALLOWED_HOSTS` | JSON 主机名单；生产禁止通配符和默认本机名单。 |
| `SERVER_REQUIRE_HTTPS` | 默认 true；生产不允许关闭。仅两个健康检查可以由内部 HTTP 探针访问。 |
| `SERVER_MAX_BODY_BYTES` | 整个请求体字节上限，默认及最大 262144；同时检查声明长度和实际分块长度。 |
| `SERVER_BODY_TIMEOUT_SECONDS` | 请求体总接收时限，默认 10 秒，最高 30 秒。 |
| `SERVER_AUTH_ATTEMPTS_PER_MINUTE` | 每个来源地址的注册/登录合并限额，默认每 60 秒 20 次。 |
| `SERVER_AUTH_GLOBAL_ATTEMPTS_PER_MINUTE` | 每个进程的认证请求总限额，默认每 60 秒 120 次。 |
| `SERVER_ARGON2_CONCURRENCY` | 单进程允许的并发 Argon2 操作，默认 2，最大 4；每个操作约使用 64 MiB。 |
| `SERVER_EXTERNAL_AUTH_LIMITS_CONFIGURED` | 默认 false；生产启动会被拒绝，直到已部署并验证外部多实例认证限流后明确设为 true。该值是运维确认门槛，代码不会替代外部设施验证。 |

生产还必须使用显式 PostgreSQL 账号、非占位密码、`sslmode=verify-full`、正确的 CA
配置及匹配证书的主机。连接超时、SQL statement timeout 和连接池规模均有上限。
可用的 PostgreSQL 连接字符串形态为
`postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=verify-full`；这是格式说明，
不是可用凭据。生产配置通过校验也不表示数据库、证书、网络或部署已经验证。
不得仅把外部限流标记改成 true 来绕过尚未完成的部署与验收。`sslrootcert` 应按实际
PostgreSQL/Supabase 证书链配置可信 CA 文件或驱动支持的系统证书库。本机已用项目下载的
根证书完成真实 Session pooler 严格 TLS 验证；这不替代 Render 主机上的再次验证。
Render 代理来源、转发头可信范围和 HTTPS scheme 识别也必须在真实环境中验证。

## API 契约

所有业务路由通过 `Authorization: Bearer <access_token>` 鉴别当前用户；服务端从令牌
推导所有权，不接受客户端提交的 `user_id`。同一用户可以创建多个会话。

| 路由 | 输入 / 行为 |
| --- | --- |
| `GET /health/live` | 仅确认进程存活，返回 `stage: pilot`。 |
| `GET /health/ready` | 实际连接数据库并检查四张表的所有预期列；未建表或数据库不可用返回 503，不泄露连接或 SQL 错误。不是 Alembic revision / 索引完整性验证。 |
| `POST /auth/register` | `username`（用户名，2–50 字）、`password`（密码，8–256 字）；返回安全用户视图。 |
| `POST /auth/login` | 同上；返回 `access_token`（登录令牌）、`token_type: bearer`、`expires_at`（UTC 失效时间）。 |
| `POST /auth/logout` | 撤销当前令牌，返回 204；其他登录令牌保持独立。 |
| `GET /auth/me` | 返回 `id`（UUID）、`username`、`created_at`，不含密码摘要。 |
| `POST /conversations` | `request_id`（UUID 幂等请求编号）、`title`（1–100 字）。首次创建 201，相同请求重试 200。 |
| `GET /conversations` | `limit` 默认 50、最大 100；`offset` 0–10000。按创建时间和 UUID 降序排序。 |
| `PATCH /conversations/{id}` | `title`。重复相同标题不会新增会话；并发不同标题采用最后一次提交。 |
| `POST /conversations/{id}/messages` | `request_id`、`role`（`user` / `assistant`）、`content`（1–50000 字）；可选 `provider`（100 字以内）、`model`（200 字以内）。首次保存 201，重试 200。 |
| `GET /conversations/{id}/messages` | `after_sequence`（排除此序号及更早消息），`limit` 默认 50、最大 100；按 `sequence_no` 升序返回。 |

用户名采用 Unicode NFKC、去首尾空白和大小写折叠后判重。密码不裁剪空格、不改写，
使用 Argon2id（64 MiB、3 次、32 字节摘要）验证。未知用户名也验证 dummy hash，
降低登录耗时造成的账号枚举风险。错误凭据、缺失/伪造/过期/已撤销令牌统一返回 401；
未知或不属于当前用户的会话统一 404。注册冲突返回通用 409，但仍可能暴露用户名可用性。

登录令牌来自 32 字节安全随机数，库中只保存加 pepper 的 HMAC-SHA256 摘要。
令牌到期必须重新登录；撤销后的请求会被拒绝，已经通过认证并执行中的请求不被中断。
用户被禁用后，其全部令牌不能用于后续请求。响应设置 `Cache-Control: no-store`，
校验错误删去原始输入，数据库错误不会把 SQL 参数或凭据回传客户端。

消息 `request_id` 的作用域是单个会话；会话创建 `request_id` 的作用域是当前用户。
同一编号与相同有效载荷重试会返回已有记录，不增加消息或序号；编号相同但载荷不同
返回 409。桌面必须在第一次发送前生成并保留编号，重试时继续使用原编号。
消息写入与序号分配处在同一数据库事务中，并由唯一约束提供最终防重保障。
当前只保存最终文字消息，不实现 AI 生成、流式响应、附件、批量上传或同步冲突合并。

## 已验证和未验证的边界

离线测试覆盖账号规范化、密码不改写、dummy hash、摘要令牌、撤销/到期/禁用、用户隔离、
并发重复请求幂等、分页与请求大小、超时、认证限流与哈希并发上限、生产配置拒绝和开发
显式建表。已另行在实际 PostgreSQL 17.6 验证 Session pooler 登录、TLS/CA、UUID /
TIMESTAMPTZ 字段、低权限账号、API 所有权检查和合成记录回滚，共 18 项检查通过。
两套 TestClient 顺序调用同一进程并共享一个外层数据库事务；**不等于公网 HTTPS、真实双设备
或并发多连接验收**。数据库连接池压测、并发事务、备份恢复与故障切换尚未验证。

进程内限流表最多记录 2048 个活动来源地址，满表对新地址拒绝，不通过驱逐旧地址重置限额；
全局和单地址队列也有界。代码不自行信任 `X-Forwarded-For`。部署时只有在已验证可信代理的
准确 IP 范围后，才可让应用服务器使用该代理提供的客户地址，不能信任任意来源的转发头。
这些限制不在进程之间共享，重新启动会重置，并非生产多实例限流方案。

当前专用角色只具有四张 pilot 表所需的 SELECT / INSERT / 部分 UPDATE 权限；不具备
DELETE、建表、管理角色或读取迁移版本表权限。已验证 `anon`、`authenticated`、`service_role`
不能使用该私有 schema 或读取/写入这些表。**尚未启用 RLS（数据库行级安全）**，当前不同用户
的记录隔离由 API 的令牌认证及所有权查询实现，不能把它说成数据库级行隔离已完成。

公开部署前还需完成：部署源码位置确认；数据库级隔离及授权矩阵审查；共享认证速率限制、
账号攻击防护与容量压测；可信代理、云主机 TLS/CA、网关连接/请求超时；日志脱敏和备份恢复。
完整业务上线还需新增业务迁移、桌面 API 适配、保留/删除政策及经另行授权的旧数据迁移。
当前没有忘记密码或账号恢复功能，不应宣称提供这些能力。

本目录用 `medical_app_private` 私有 schema 和 `pilot_` 前缀隔离四张新表，SQLite 单元测试
将私有 schema 映射到本地默认命名空间；不复用或宣称兼容现有桌面 schema。云端初始版本为
`pilot_0001`，包含四张业务表和一张版本表。迁移和角色创建均通过显式管理命令执行，应用
启动不建表。不自动创建 Render 资源或调用付费服务。

## API 核实来源

- [FastAPI 的依赖认证、401 和 dummy hash 示例](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)：本实现采用 opaque 令牌，不照搬文档中的 JWT 示例密钥。
- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)：仅在生命周期结束时释放连接池。
- [SQLAlchemy 2.0 ORM](https://docs.sqlalchemy.org/en/20/orm/quickstart.html)：`DeclarativeBase`、`Mapped`、`mapped_column` 与会话事务。
- [SQLAlchemy UUID 类型](https://docs.sqlalchemy.org/en/20/core/type_basics.html#sqlalchemy.types.Uuid)：PostgreSQL 原生 UUID 与 SQLite 测试适配。
- [SQLAlchemy psycopg 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)及 [psycopg 3 使用说明](https://www.psycopg.org/psycopg3/docs/basic/usage.html)：`postgresql+psycopg` 与数据库事务行为。
