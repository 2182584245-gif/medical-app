# 完整平台部署与运维

仅 Windows 桌面云端升级；不发布网站前端或修改 Android。免费服务用于小量测试。

## 组成和秘密边界

- Desktop（桌面）：Python 3.13 / PySide6；用户显式选择本地或云端。
- API（业务接口）：当前尝试 Railway 赠送额度，原 Render 为备选；单进程、HTTPS，启动入口
  `cd application && PYTHONPATH=src python -m server.platform_entrypoint`。
- Database（业务数据库）：现有 Supabase PostgreSQL；Session pooler（会话连接池）
  端口 5432，TLS `verify-full`（核对证书链和服务器主机名），不关闭验证。
- 新私有空间 `medical_app_platform`（完整平台）；旧 `medical_app_private`（四表试点）
  不删除。Alembic 同一迁移历史：`pilot_0001 -> platform_0001`。
- Runtime role（运行角色）`medical_app_platform_runtime`：无超级用户、建库、建角色、
  复制、绕过 RLS 权限，无角色成员资格；连接上限 5；逐表授权，无 schema CREATE。
- 秘密仅在本机 `.local` DPAPI 加密记录和经授权的后端服务秘密环境中。管理员数据库连接串、
  管理员应用密码、用户密码和 AI Key 禁止加入源码、发布包和普通日志。
- 云端应用账号存 Argon2id 哈希，Opaque token（随机不透明令牌）仅存 HMAC 摘要；
  真令牌在电脑内存，关闭后失效于本机，服务端到期或注销后拒绝使用。

## Railway 服务配置（当前路线）

- 实际前置测试及停止状态见 [交付检查](PLATFORM_RELEASE_CHECKS.md)；不要将测试探针
  地址填入桌面应用，不把一次 TCP 连通当作正式数据库登录和权限验收。
- 正式镜像说明见 [Railway 部署目录](../tools/railway_platform/README.md)。只安装
  `requirements-server.txt`，不要让自动检测安装桌面的 `requirements.txt` / `pyproject.toml`。
- 仅对正式服务核验公网入口只有平台 HTTP 边缘、没有 TCP Proxy/其他直连入口，
  且项目同环境内全部服务受信任后，才可显式确认 `PLATFORM_RAILWAY_PROXY=true` 和
  `PLATFORM_RAILWAY_EDGE_ONLY=true`；两者不是代理身份认证或防火墙。
- `PLATFORM_ALLOWED_HOSTS` 使用已核实的精确服务 Host，与 `RAILWAY_PUBLIC_DOMAIN`
  一致；不使用通配符、不伪造 Render 环境变量。Railway 模式不从任何转发头恢复
  客户端 IP，限流保守使用 socket peer，可能导致不同用户共享部分认证限额。
- 保持 `PLATFORM_REQUIRE_HTTPS=true`、`sslmode=verify-full`、项目 CA 及专用运行角色。
  运行连接、pepper 不可填入源码或探针；配置到第三方服务前需确认目标与秘密范围。
- 公网验收必须明确同时输入核验过的 HTTPS origin（源站地址）、相同的纯主机名和
  `--confirm medical_app_platform`，例如参数名 `--base-url`、`--expected-host`、`--confirm`。
  不接受管理员数据库 URL 作为上述参数，不通过聊天传递密码；只创建随机合成测试账号，
  清理沿用精确身份验证，不删除实际用户数据。

## Render 服务配置（历史备选，未部署）

- Workspace（工作区）：medical-app，用户已经确认。
- Repository（源码仓库）：2182584245-gif/medical-app；Branch（分支）：main。
- Build（构建）：`cd application && pip install -r requirements-server.txt`。
- Start（启动）：上文入口；Free（免费实例）；Singapore（新加坡）。
- `PYTHON_VERSION=3.13.15`；`GIT_LFS_SKIP_SMUDGE=1`；`PLATFORM_ENV=production`；
  `PLATFORM_RENDER_PROXY=true`。
- `PLATFORM_DATABASE_URL`：只使用新专用角色，不使用 postgres 管理员。
- `PLATFORM_TOKEN_PEPPER`：随机 256 位 HMAC 秘密，安全传入，不展示。
- `PLATFORM_DATABASE_CA_PEM`：项目根证书 PEM；进程写受限临时文件供 libpq 校验证书，
  不是业务持久存储。URL `sslrootcert=/tmp/medical-app-prod-ca.crt`。
- 允许域名从 Render 注入的真实 `RENDER_EXTERNAL_HOSTNAME` 读取，不允许通配符。
- 不配置自动创建管理员；初始化只有本机管理函数，没有公开 HTTP/RPC 入口。
- `/health/live`（进程存活）和 `/health/ready`（数据库、权限、RLS、共享限流就绪）。
  health ready 会做真实可回滚的限流探针，不消耗正常用户额度。

## 初始化顺序

1. 本机运行后端离线测试和源码检查；核实当前 profile 指向正确项目和可信证书。
2. `python -m server.platform_admin inspect`：只读对象/版本/权限；不读真实健康记录。
3. `python -m server.platform_admin migrate --confirm medical_app_platform --pilot-confirm medical_app_private`：
   显式创建新平台空间；遇到未知同名空间拒绝接管；不迁移任何历史用户数据。
4. `python -m server.platform_admin provision --confirm medical_app_platform`：先存加密 pending
   凭据再建角色，核实后标记 ready；重试不旋转密码、不接管未知角色。
5. `python -m server.platform_smoke --confirm medical_app_platform`：真实 PG 的单连接回滚
   验收，含业务与 SET LOCAL ROLE 行级隔离；不冒充公网或独立连接测试。
6. 由本机管理员调用 `bootstrap_admin` 隐藏输入密码；创建初始 operator（运营管理员）。
   已有 operator 或同名账号时拒绝覆盖；不得将密码设为全体用户共用默认密码。
7. 经确认安全传递运行角色 URL、pepper、根证书到所选后端服务，核实赠送额度与实际部署。
8. 验证实际 HTTPS、匿名拒绝、两独立客户端同步、注销撤销、角色隔离与故障提示。
   仅用随机合成账号；如测试临时提交，必须限定本次精确 ID 清理并核实无残留。

## 数据设计与备份边界

- ID（主键）：BIGINT identity（自动递增 64 位整数），兼容原桌面服务整数编号。
- `*_at`（时间）：统一 ISO UTC 文本 `YYYY-MM-DDTHH:MM:SS.ffffffZ`，数据库约束编码；
  **不是 TIMESTAMPTZ 类型**。日期是 ISO 日期文本；界面默认北京时间。
- `*_json`（结构化内容）：JSON 文本且数据库检查 JSON 格式；没有 pgvector/向量知识库。
- 文件内容 BYTEA（二进制）：报告原件单个 15 MiB/每用户100 MiB；聊天单个10 MiB、
  每次4个合计20 MiB/每用户100 MiB。两类配额分别计数，不等于 Supabase 可无限存储。
- RLS（行级安全）：从认证后身份安装事务级 user_id/role_code，连接池每次清空。
  普通用户只访问自己数据，顾问访问当前绑定会员的授权业务；运营仅经固定接口管理。
  无客户端 SQL、动态方法名任意调用、pickle 或远程文件系统路径。
- RLS 是受信任后端身份下的纵深防护，**不能抵御已泄露数据库密码后的任意 SQL**。
- 每次业务写入为一个事务；嵌套步骤使用 savepoint（保存点）；随机请求编号加摘要
  防止同请求重复写。禁止自动重试不确定写入；刷新确认后再操作。
- 当前测试负载使用单一事务级 advisory lock（建议锁）串行业务请求，不宣称高并发。
- 云端模式整库不装在 EXE 旁；复制程序只迁移本地模式的数据。云端另机登录查看同库，
  不是离线缓存或离线同步。普通用户不能导出全体账号、密码哈希、会话或审计记录。
- 审计与 RPC 防重记录运行角色无删除权限。当前不自动清理，需受控管理维护；
  定期检查配额。会话到期自动拒绝使用，但过期行清理不是自动计划任务。
- 云端备份应由管理员另行导出并离线验证恢复；不能把本地 SQLite 导出当成云库备份，
  也不能假设 Supabase 免费版提供全部付费备份能力。原本地完整备份功能保留。

## 免费与运行限制

Railway 当前试用提供一次性 5 美元、最长 30 天，耗尽/到期后 Free 方案每月 1 美元
额度且不结转。实际是否足够常驻取决于运行消耗，不能承诺永久免费在线。受限试用的
外连端口可能受限；当前探针只证明此账号当时能连通目标。测试后停止无用实例，不为
绕过限制注册多个账号、不自动升级或绑定卡。正式业务数据仍保存在 Supabase。
参见 [Railway 试用说明](https://docs.railway.com/pricing/free-trial)。

Render 免费 Web Service 约15分钟无访问后休眠，下一次唤醒约1分钟；文件系统临时，
所以业务数据保存在 Supabase，不放在 Render SQLite。每月免费实例小时、流量、构建
分钟受限；有支付方式时超出部分可能收费，使用前在控制台核对预算/限额，程序不自动
升套餐。大量访问外部数据库也可能触发免费实例限制；不适合作为长期可靠医疗系统。

参考：[Render 免费限制](https://render.com/docs/free)、
[Python 版本设置](https://render.com/docs/python-version)、
[Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)。
