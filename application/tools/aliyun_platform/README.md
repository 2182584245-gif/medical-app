# 独立阿里云部署与维护

## 当前代码的运行范围（2026-09-19）

当前源码仅保留 `https://39.106.166.15/aliyun` 运行路线。网关对 `/supabase` 与 `/supabase/*` 固定返回 **410 Gone**，不反向代理、不读取旧路线凭据、不回退到其他数据库。Compose 不再定义 `api-supabase` 或其 profile，API 入口只接受 `API_TARGET=aliyun`。本节描述当前代码，不将尚未完成的真实部署验收写成已完成。

新版 API 要求 v4 的完整 37 表结构及有效的私有开发者配置加密文件。**新装环境和已有生产环境升级，都必须先经过 v4 计划、审核、应用与状态核验；旧版 32 表 bootstrap 成功并不是新版 API 的启动条件已经满足。** 上传源码、发布 EXE 或重建镜像也不等于真实服务已完成升级；具体上线结论以该部署的独立验收记录为准。

**以下“新部署顺序”只适用于另一个经批准的新环境，不要在当前运行实例重新执行 prepare 或重建数据卷。** 2026-09-08 的历史双路线验收及来源资料复制记录仍保留在 [VALIDATION.md](VALIDATION.md)，不是当前支持双路线的承诺。原 Supabase 数据库和历史管理员备份／迁移工具没有删除；停止运行入口不等于删除来源资料。

本目录只部署到 `/opt/medical-app` 和三个带所有权标签的专用 Compose 卷。prepare 不读取、上传或导入桌面 SQLite，不调用 Supabase 管理接口。新数据库为 `medical_app_aliyun`，使用独立随机 bootstrap/runtime 密码、内部 CA、一套 API TLS 证书及会话 pepper。现有目录或专用卷存在时，prepare 拒绝，不能用删除卷／重新生成密钥来“修复”认证失败。

## 新部署顺序

在管理员批准的 Ubuntu 24.04 主机上准备 Docker Compose、Python 3、OpenSSL；从已审核源码目录运行（不要把包含真实数据库/凭据的整个桌面工作目录上传）：

```sh
sudo python3 -m tools.aliyun_platform.prepare --public-host 39.106.166.15 --confirm-new
```

这一步只生成新配置，证书环境固定为 Let's Encrypt **staging**。之后把已审核且经过 `Dockerfile.dockerignore` 白名单筛选的源码放到 `/opt/medical-app/source`（不要自动同步本地 data/、outputs/、密钥、OCR/语音模型）。构建时的依赖哈希锁使用 `tools/local_platform/requirements-linux-lock.txt`，其 Linux CPython 3.13.15/x86_64 范围需要与目标镜像一致；该历史锁的“local”命名不代表沿用了本地安全开关。

```sh
cd /opt/medical-app
sudo docker compose config --quiet
sudo docker compose build api-aliyun
sudo docker compose up -d postgres
sudo docker compose --profile maintenance run --rm bootstrap
```

bootstrap 使用 frozen v1 DDL、v2 增量 DDL 及 v3 医疗事实分类约束安装 32 张表。只接受新且独立标记的空库；已有安装只核验，不修补、不接管、不重设密码。校验角色权限／所有权、全部强制 RLS、元数据指纹、匿名用户不可读、verify-full TLS。既有 v2 数据库须经过单独审核的 `upgrade_medical` 计划、全行核验与事务升级，不能用 bootstrap 重建。首次运营账号需要管理员独立批准后创建，工具不会自动导入历史账号或合成演示账号。

此时保持 API 停止。在经过批准、已挂载实际部署标记、管理密钥和私有报告目录的维护容器中，先完成备份及隔离恢复，再运行下面的 **v3 → v4** 流程。占位符必须换成该部署真实值，计划和结果使用不同的新文件；不能将未挂载的容器临时目录当作可靠的审核凭据保存位置。

```text
python -m tools.aliyun_platform.upgrade_developer plan --deployment-id <现有部署ID> --plan-file <新私有计划文件>
python -m tools.aliyun_platform.upgrade_developer apply --deployment-id <同一部署ID> --plan-file <已审核计划文件> --confirm-plan <完整plan_sha256> --result-file <新私有结果文件>
python -m tools.aliyun_platform.upgrade_developer status --deployment-id <同一部署ID>
```

审核需确认原 32 表资料和权限未被重建或改写，只加入受控的五张开发者表及必要策略、索引和权限。开发者账号授权是另一个需要验证已有管理者密码的操作，不会因结构升级自动创建“万能账号”。完整边界见 [开发者 v4 部署说明](../../server/DEVELOPER_V4_UPGRADE.md)。

v4 状态核验通过、运行账号可读新增表且私有 AI 加密文件权限/长度验证通过之后，才允许启动新版 API：

```sh
cd /opt/medical-app
sudo docker compose up -d api-aliyun caddy
```

之后仍需核验受信任 HTTPS、三类账号登录、开发者二次验证与权限隔离，并再次执行备份恢复演练；不使用关闭 TLS 校验来绕过尚未完成的证书配置。

只有 Caddy 公开 TCP 80、TCP/UDP 443。PG 不公开 5432，API 不公开 8443；PG 仅在 internal 数据库网络。API 有独立出站网络供明确调用的 AI 等业务使用，但启动不会自行调用这些服务。仅 `/aliyun/` 在网关被剥离前缀；内部继续 HTTPS，验证私有 CA 和上游主机名，保留原始 Host，Uvicorn `proxy_headers=False`。平台自身的共享认证限流 readiness 检查仍生效。

## 旧路线退出与已有实例升级

`configure supabase` 已从 CLI 移除。保留的兼容函数以及旧 `handoff_runtime`／`receive_runtime` 都固定拒绝，不读取 URL、CA、DPAPI 配置或 stdin，不发 SSH，不安装运行凭据。历史 `transfer`／备份及经独立授权的管理员工具不受影响，也不会自动执行。

升级已有部署前应先保存并核对原配置与备份、确认精确部署标记和旧容器身份。**先停止全部旧版 API，再启动新版 API**；旧认证限流清理器不认识新版 AI 桶，禁止混合运行。旧 `api-supabase` 即使已从新 Compose 文件消失，也不能假定已自动停止，须由管理员依据旧配置与容器标记明确停用。不得删除数据库卷或 Supabase 来源数据库。

对已有 v3 环境同样执行上一节的 v4 `plan → 人工审核 → apply → status`，不得重跑新装 `prepare` 或删除数据卷。已有 v4 环境用专用 `status` 验证，不能重放迁移或退回旧 bootstrap 的 32 表检查来冒充成功。旧 Alembic/Supabase 管理工具的严格白名单保持原范围，不应用来接管这条专用阿里云部署路线。

新版网关配置应经审核后单独替换并重新创建 Caddy；新配置生成器不会自动接管旧双路由文件，旧配置不匹配时安全拒绝。旧运行凭据保持不挂载、不使用；其归档或清除应另行批准，本文不会自动删除。

## 默认 AI 的分离加密挂载

API 只读挂载 `./secrets/ai-envelope → /run/ai-secrets` 与 `./secrets/ai-key → /run/ai-key`，分别读取 `deepseek-key.aesgcm` 和 `default-ai-kek.bin`。两个文件由经审核的私密交付步骤提供，不能放进源码、镜像层或发布包。prepare 只创建空私有目录，不生成或读取真实 AI Key；未配置时默认 AI 返回不可用，不影响普通数据库健康检查。

`handoff_default_ai` 在 Windows 上把默认 DPAPI Key 加密为 AES-GCM，并将重试材料再用本机 DPAPI 独占新建到源码目录之外；`receive_default_ai` 仅在已确认的 Linux root 部署中写入固定目录，文件为运行 UID 10001 的 `0400`。同一材料可重试，不同已有材料不会覆盖。密文和 KEK 一同取得即可解密，整个交付包仍按秘密管理；SSH 必须严格校验已固定主机密钥。两文件落地并不代表实际模型调用验收已经完成。

单 Aliyun 数据库的默认额度为每用户 2 次／分钟、20 次／UTC 日，全平台 6 次／分钟、100 次／UTC 日。服务器只能调低这些上限，不自动重试失败请求。完整 AES 文件格式、认证和流式限制见 [DEFAULT_AI_AND_REMINDERS.md](../../server/DEFAULT_AI_AND_REMINDERS.md)。

## 正式证书明确切换

staging 证书不受普通客户端信任，不能关闭客户端 TLS 校验来上线。先确认公网 80/443 到达 Caddy、证书签发与完整 API 验收通过；然后显式切换并重新创建 Caddy（避免文件 bind mount 保留旧 inode）：

```sh
cd /opt/medical-app/source
sudo python3 -m tools.aliyun_platform.configure production --confirm-production
cd /opt/medical-app
sudo docker compose up -d --force-recreate caddy
```

Caddy 自动管理 ACME 续证并保留 `/data` 卷；不要删除证书卷来重试。内部 CA 有效 10 年、内部服务证书 1 年；容量检查提前 30 天报告需维护，内部证书目前**没有自动轮换**。应安排管理员在到期前单独审核轮换。

## 备份、恢复演练和告警

```sh
cd /opt/medical-app/source
sudo python3 -m tools.aliyun_platform.maintenance backup
sudo python3 -m tools.aliyun_platform.maintenance capacity
```

备份为 PG17 custom 格式，不在命令行传密码，连接使用 verify-full。每次恢复到新生成、带独立所有权标记的 `medical_app_verify_<随机值>` 数据库；`pg_restore --exit-on-error --single-transaction` 成功后，严格核验完整的 v3 32 表或 v4 37 表集合、全部强制 RLS、恢复副本计数，并比较备份前源库、恢复库和备份后源库的目录指纹（列类型、默认值、约束、策略、授权、索引和归属）。所有检查通过才删除**本次成功的临时验证库**、写入 SHA256 与 verified 记录；未知对象或指纹不符会拒绝验收，不跳过新增五表。失败备份/验证库保留供人工复查，脚本不自动清理任何归档；没有已验证状态的副本绝不会因保留策略被删除。运行中业务仍可写入，报告的记录数是恢复副本内的值，不是另一次时间点的在线计数。

备份包含敏感记录，文件保持 root-only 权限。应另行批准加密异地备份、保留周期和恢复演练；仅同盘备份不能防主机/磁盘丢失。容量达到 85% 或空闲少于 2 GiB、36 小时无验证备份、私有证书剩余不足 30 天时，capacity 返回失败供监控接收。脚本不会自动删除旧备份，管理员须有告警接收人。

随附 systemd service/timer 可在管理员审核后安装到 `/etc/systemd/system/` 并启用：每日北京时间 03:15 加 0～15 分钟随机延迟备份、每小时加 0～5 分钟随机延迟检查容量。备份日历显式使用 `Asia/Shanghai`，不依赖主机的默认时区。本次实例已在首次手动备份与恢复核验成功后启用这两项定时器；容量检查目前通过日志与退出状态报告，没有配置外部消息接收渠道。单机资源限制只是保守起点，不是并发承诺。

## 已审核虚构资料的增量升级

`tools/plan_synthetic_upgrade_v170.py` 仅制定计划；`tools/aliyun_platform/upgrade_synthetic.py` 是独立管理员执行器，不是公共 API，也不属于基础 API/transfer 镜像或标准公开源码包的默认执行入口。使用时另行审核并交付这些运维源文件及其依赖；不能只重建已有镜像后假定工具已经存在。`tools/prepare_synthetic_upgrade_v170.py` 只在本机读取严格核验的两个虚构种子和旧导入计划，输出受保护输入包；不连接云端，不接受真实私有桌面资料。

实际目标快照在服务器原地读取，不下载到公开仓库。先验证旧云账号的身份映射与原口令兼容性，再审核新计划：仅追加已批准虚构事实；档案/展示字段按旧种子、新种子、当前云值三方比较，保留用户已修改的值；只对未被用户改动的两名虚构顾问期限作明确批准的更新。账号、口令、会话及开发者安全资料不重建、不重设。批次登记与业务变更处于同一事务，重复执行不补回用户后来删除的资料。

输入文件可能含虚构账号的密码哈希，仍按私密运维材料管理：在服务器保持 root-only 权限，不进入 Git、公开下载目录或发布 ZIP。实际执行仍须先备份恢复验证、确认完整计划摘要，完成后保存独立验收结果。工具存在或输入包生成成功不代表资料已经导入。

## 4 GB 测试主机的数据库运行配置

本轮 v4 实际部署使用现有 4 GB 主机，PostgreSQL 容器上限设为 1 GiB，API 仍为 384 MiB。
扩充资料后曾观察到 PostgreSQL cgroup OOM；仅提升内存仍不足，最终同时设置 `jit=off`，
重新加载 PostgreSQL 配置并仅重启 API 回收旧连接池。修改不涉及数据库权限或业务资料。
新装 `prepare` 默认生成该配置，但绝不会覆盖已有服务器配置；已有实例必须先备份配置、
审核变更、核对 `pg_file_settings` 的 applied/error 与 `pg_settings` 的实际值，不能重跑新装。
该内存数值不是适用于所有服务器的通用建议，2 GB 主机需重新做容量评估。
连续访问、双会员读取和无新增 OOM 的实测见 [云端登录修复验收](../../docs/release-1.7.0-cloud-login-validation.md)。

## 官方依据（2026-09-08 核对）

- [Caddy TLS issuer/profile](https://caddyserver.com/docs/caddyfile/directives/tls)：支持显式 ACME profile；该规范目前仍标注实验性。
- [Caddy HTTPS 上游和 Host](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)：2.11 起 HTTPS 上游默认改 Host，因此本配置显式保留 `{hostport}`；内部 CA 使用 `tls_trust_pool file`。
- [Caddy 静态响应](https://caddyserver.com/docs/caddyfile/directives/respond) 与 [路径匹配](https://caddyserver.com/docs/caddyfile/matchers)：2026-09-09 核对，退休路径同时精确匹配 `/supabase` 与其子路径并返回固定 410，不配置该上游。
- [Let's Encrypt profiles](https://letsencrypt.org/docs/profiles/)：`shortlived` 支持 DNS/IP，证书有效期 160 小时，必须依赖可靠自动续期。
- [Supabase SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement)：verify-full 同时验证 CA 与主机名，需要显式数据库 CA。
- [Supabase changelog](https://supabase.com/changelog)：实现前已检查；Markdown 入口不可用时采用官方 HTML 和文档检索。

离线自动化测试位于 `tests/server/test_aliyun_deployment.py`、`test_aliyun_runtime_handoff.py` 与 `test_default_ai_handoff.py`。本版单路线升级、加密默认 AI 和公网请求必须独立验收，不能沿用历史双路线结论或以静态测试代替。历史记录仅用于追溯，不作为当前运行入口配置。
