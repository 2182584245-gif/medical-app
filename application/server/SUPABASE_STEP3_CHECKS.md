# Supabase 阶段 3：私有数据库迁移与后端接口试点验收

验收日期：2026-09-07（北京时间）。

结论：用户指定的 Supabase 项目已经通过真实数据库连接、私有试点建表、独立低权限账号及
回滚式 API 联调；本阶段后端基础已落地。**尚未部署公共 API、接入桌面云同步或迁移历史数据。**
这份记录不是生产上线、完整业务迁移或“所有设备已同步”的证明。

## 1. 本轮实际完成的云端操作

目标项目：`https://rvtjveplqnelgwnpuqtn.supabase.co`，数据库为 PostgreSQL 17.6。

| 对象 | 中文含义与实际结果 |
| --- | --- |
| `medical_app_private` | 应用私有 schema（数据库命名空间），本轮创建。 |
| `pilot_users` | 试点用户表，保存账号信息和密码摘要，不保存明文密码。 |
| `pilot_login_sessions` | 登录会话表，保存登录令牌摘要、有效期及撤销状态。 |
| `pilot_conversations` | 用户的多个对话，包含标题和幂等请求编号。 |
| `pilot_messages` | 对话文字消息，包含顺序、角色和幂等请求编号。 |
| `alembic_version` | 数据库结构版本表，当前版本 `pilot_0001`。 |
| `medical_app_runtime` | 独立后端运行角色，已创建并使用新连接验证登录成功。 |

执行顺序是：只读目录检查 → 显式 Alembic 初始迁移 → 检查所有权和有效权限 → 显式创建运行
角色 → 使用运行角色独立登录 → 真实数据库上的 API 合成测试 → 回滚并检查本次合成数据零残留。

没有改变 `public` / `auth` / `storage` 业务结构，没有读取或上传桌面 SQLite 的真实健康记录。
没有导入实际账号或聊天历史，没有调用 AI，没有创建 Render 服务或启用付费资源。

## 2. 连接及凭据保护

- 使用项目 Session pooler（会话连接池）端口 5432，管理员和运行角色均验证了真实登录。
- `sslmode=verify-full`：同时核验可信证书链和数据库主机名；实际数据库连接的 TLS 已确认开启。
- 根证书已固定到项目本机 `.local/certs/prod-ca-2021.crt`，原 Downloads 文件保留。
- 根证书文件 SHA-256：`700723581420DD1AC98FD7E9AC529F0EF210EADCAF87FC868A3AD7D114C2F3B7`。
- `.local/supabase-connection.json`：管理员配置，供受控预检、迁移和授权使用。
- `.local/supabase-runtime.json`：独立运行配置，保存运行连接信息和 token pepper（令牌摘要额外秘密）。
- 两份配置均由本机当前 Windows 用户的 DPAPI 加密，不写入源码、聊天、桌面 EXE 或发布包。
  DPAPI 文件不能当成跨设备秘密迁移包，也不能抵御已控制当前 Windows 用户的恶意程序。
- 角色创建采用本地 `pending`（待完成）记录 → 数据库事务 → `ready`（就绪）记录；出现不明确结果
  时保留原凭据，不擅自接管未知角色或静默轮换密码。迁移异常不再被误报为“肯定未提交”。

实际测试发现 Windows 到 Session pooler 的首次 TLS/登录握手可能超过 5 秒，因此仅把后端
数据库连接时限调整到 10 秒。SQL 语句时限仍为 5 秒；连接池基本 5 个、最多额外 5 个，未放松
证书验证。该结果不能推断 Render 机器的连接性能或故障恢复表现。

## 3. 数据库权限检查

运行角色没有超级用户、建库、建角色、复制或绕过 RLS 能力，不是表所有者。
数据库连接数上限为 10；只授予目标私有 schema 的 USAGE（使用命名空间）和所需表操作。

| 表 | SELECT（读取） | INSERT（新增） | UPDATE（修改） | DELETE（删除） |
| --- | --- | --- | --- | --- |
| `pilot_users` | 允许 | 允许 | 允许 | 拒绝 |
| `pilot_login_sessions` | 允许 | 允许 | 允许 | 拒绝 |
| `pilot_conversations` | 允许 | 允许 | 允许 | 拒绝 |
| `pilot_messages` | 允许 | 允许 | 拒绝 | 拒绝 |
| `alembic_version` | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

运行角色不获授 schema CREATE、TRUNCATE、REFERENCES、TRIGGER、MAINTAIN 等额外权限。
有效权限矩阵已检查。真实 SQL 拒绝探测额外覆盖：版本表读取、用户表 DELETE、消息表 UPDATE；
三者均返回 PostgreSQL `42501`（权限不足）。探测使用 `LIMIT 0` 或 `WHERE FALSE`，不改变业务行。
没有执行真实 CREATE / DROP / TRUNCATE 操作来测试权限，不能把目录验证说成破坏性执行验证。

另对 `anon`、`authenticated`、`service_role` 三个 Data API 角色逐表检查，共 15 组结果：不能使用
该 schema，也没有对应表的所查读写权限。本次结论只适用于当前对象；未来迁移仍需重新检查授权。

**当前四张试点表未启用 RLS（Row Level Security，数据库行级安全）。** 不同账号的数据隔离由
API 根据登录令牌推导用户、约束查询所有权实现；`NOBYPASSRLS` 不代表 RLS 已启用。当前运行
权限设计也不是整个 PostgreSQL 实例的能力沙箱，不能宣称已满足生产纵深隔离要求。

## 4. 18 项真实 PostgreSQL + 进程内 API 联调

成功状态：`passed_rolled_back`（全部通过且已回滚）。使用低权限运行角色，非管理员角色。

| 编号 | 检查 | 结果 |
| --- | --- | --- |
| 1 | 测试前数据库就绪检查 | 通过 |
| 2 | 实际运行角色及数据库 TLS | 通过 |
| 3 | 原生 UUID（全局唯一编号）和 TIMESTAMPTZ（带时区时间）字段 | 通过 |
| 4 | 不允许的 SQL 操作被拒绝 | 通过 |
| 5 | 两个合成账号注册 | 通过 |
| 6 | 登录及同账号独立令牌 | 通过 |
| 7 | 创建对话重试不重复新增 | 通过 |
| 8 | 同请求编号不同标题返回 409（冲突） | 通过 |
| 9 | 同账号另一个测试客户端可看到对话 | 通过 |
| 10 | 不同账号读取、改名和写消息均被隔离，返回 404 | 通过 |
| 11 | 消息重试不重复新增 | 通过 |
| 12 | 同请求编号不同正文返回 409 | 通过 |
| 13 | 同账号另一测试客户端可看到消息 | 通过 |
| 14 | 消息顺序和正文内容正确 | 通过 |
| 15 | 对话改名对同账号另一客户端可见 | 通过 |
| 16 | 退出只撤销所用令牌，另一登录保持独立 | 通过 |
| 17 | 外层事务回滚，核查本次合成账号及关联记录零残留 | 通过 |
| 18 | 测试后数据库就绪检查 | 通过 |

合成账号数为 2；成功结束后，本次合成记录保留数为 0。测试失败也有外层事务回滚保护；失败
不会被输出成成功。零残留核查使用本次随机账号标识及关联关系，不是遍历真实用户的全库审计。

测试方式为两个进程内 TestClient（接口测试客户端）**顺序**访问真实 API 逻辑，业务请求共享
一条 SQLAlchemy 连接及同一个外层数据库事务，内部使用 SAVEPOINT（保存点）隔离接口提交。
模拟地址 `https://testserver` **没有建立公网 HTTPS 连接**。因此以下能力仍未验收：

- 独立物理设备、桌面安装包和浏览器访问公共 API。
- 多进程、多实例或独立数据库连接之间的并发提交及竞争条件。
- 真实网络断开后的重试、连接池压力、冷启动、备份恢复和故障切换。
- 数据库级 RLS 隔离、附件同步、AI 调用、其他健康业务和历史记录迁移。

## 5. 本轮最终自动化回归

| 检查范围 | 结果 | 证据 |
| --- | --- | --- |
| 后端单元/接口测试 | 249 通过，0 失败，0 错误，0 跳过 | [后端 JUnit XML](../outputs/supabase-step3-tests.xml) |
| 原桌面程序离线回归 | 366 通过，0 失败，0 错误，0 跳过 | [桌面 JUnit XML](../outputs/supabase-step3-desktop-tests.xml) |
| Ruff 后端静态检查 | 通过 | `server`、`tests/server` |
| 独立后端环境依赖一致性 | `pip check` 通过 | `.venv-server` |
| 新增云端管理、运行角色及回滚测试代码的独立只读复核 | 本轮试点范围内无阻断发现 | 不访问本机秘密或云端的静态复核 |

总计 615 项自动化回归通过。后端测试另有两项上游弃用提示（Starlette TestClient/httpx 和
AnyIO BlockingPortal 别名），不是测试失败；本轮没有为消除提示而改变锁定的依赖版本。
自动化测试中的临时 SQLite、模拟网络或模拟凭据结果，与上节真实 PostgreSQL 联调分别记录，
不相互冒充。桌面回归使用离屏 UI，不代表真实手机或所有 Windows 设备的人工体验验收。

## 6. 已实现文件及复查入口

- `server/cloud_admin.py`：绑定目标项目的只读目录检查和显式初始迁移入口。
- `server/cloud_runtime.py`：创建/核查专用角色，保存独立 DPAPI 运行配置。
- `server/cloud_smoke.py`：合成 API 测试、真实 PostgreSQL 类型/权限检查及事务回滚。
- `server/database.py`：保持安全边界的连接超时修正。
- `server/migrations/env.py`：迁移提交状态不明确时的准确、脱敏错误说明。
- `tests/server/test_cloud_admin.py`、`test_cloud_runtime.py`、`test_cloud_smoke.py`、
  `test_database_cloud.py`：管理确认门槛、错误脱敏、角色状态机、回滚和连接设置回归。

复查命令见 [本机操作指南](SUPABASE_NEXT_STEP.md)。当前已建表，不要把重复迁移当成日常连接检查。

## 7. 接下来如何推进

```text
├── 本阶段：Supabase 私有试点基础已完成
│   ├── 严格证书验证、四张业务表和数据库版本表
│   ├── 独立低权限后端运行账号
│   └── 回滚式接口联调及后端/桌面回归
├── 下一阶段：后端公开测试前的准备
│   ├── 用户确认后端源码仓库位置
│   │   └── 当前源码尚未初始化 Git；原 medical-app 仓库主要用于应用下载
│   ├── 数据库级隔离、授权矩阵和迁移策略审查
│   ├── 共享认证限流、代理来源和日志安全
│   └── 免费 Render 测试部署及真实 HTTPS、并发/恢复验收
├── 后续阶段：仅升级 Windows 桌面
│   ├── 登录和聊天通过自有 API 访问云端，不内置数据库管理员密码
│   ├── 超时、断网、重复请求和错误提示处理
│   ├── 新增业务云端表及相应 API，逐项接入其他现有功能
│   └── 跨设备实测后重新封装电脑程序
└── 历史数据迁移单独实施
    ├── 先备份本地数据并核查迁移映射
    ├── 由用户明确确认真实数据上传范围
    └── 小批迁移、数量/关联校验和恢复演练后再切换
```

公开部署停在源码位置确认：当前开发目录 `C:\Users\wuyuf\Documents\Codex\2026-09-02\wo-d`
不是 Git 仓库；另一个桌面目录 `C:\Users\wuyuf\Desktop\medical-app` 的 origin 指向
`https://github.com/2182584245-gif/medical-app.git`，本轮只读检查显示工作区干净。
没有把两个目录自动合并，也没有提交、推送或覆盖既有下载文件。

Render 部署技能要求先明确可部署的源码仓库或镜像来源；不能在没有源码远端时假定公共服务
已部署。即使确定仓库，仍必须完成生产安全门槛；**禁止只把
`SERVER_EXTERNAL_AUTH_LIMITS_CONFIGURED` 改成 true 来绕过共享限流验收。**

本轮没有新生成 EXE/APK，也没有修改原桌面程序的数据存储方式。原桌面程序仍按本地模式运行。

## 8. 实现依据

本轮使用 Supabase 和 PostgreSQL 最佳实践技能落实私有命名空间、凭据分离、最小权限以及短事务
回滚验收；没有把默认权限撤销、角色属性或 API 所有权校验误称为完整 RLS 方案。

- [Supabase Data API 安全边界](https://supabase.com/docs/guides/api/securing-your-api)
- [Supabase PostgreSQL 角色](https://supabase.com/docs/guides/database/postgres/roles)
- [Supabase 行级安全](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [PostgreSQL 17 默认权限及其作用范围](https://www.postgresql.org/docs/17/sql-alterdefaultprivileges.html)
- [SQLAlchemy 将会话加入外层测试事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)

本记录只包含脱敏状态，不包含数据库密码、完整连接串、登录令牌、pepper 或 AI Key。
