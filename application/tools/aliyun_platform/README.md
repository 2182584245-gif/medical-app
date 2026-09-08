# 独立阿里云部署与维护

## 本次已上线实例（2026-09-08）

已按用户授权部署到 `39.106.166.15`，两条公网 HTTPS 路线均已通过实际业务验收：默认 `https://39.106.166.15/aliyun`，备用 `https://39.106.166.15/supabase`。现有 Supabase 平台的 1 个账号、1 个会话和 1 条审计已复制到新建阿里云数据库，来源保留；没有上传桌面历史、AI Key 或在线令牌。实际部署与测试证据见 [VALIDATION.md](VALIDATION.md)。

**以下“新部署顺序”只适用于另一个经批准的新环境，不要在当前运行实例重新执行 prepare 或重建数据卷。** 两条路线是独立数据库；切换地址不会自动合并账号，也不是两库持续互相复制。

本目录只部署到 `/opt/medical-app` 和三个带所有权标签的专用 Compose 卷。不会读取、上传或导入任何桌面 SQLite 数据；不会调用 Supabase 管理接口。新数据库为 `medical_app_aliyun`，使用独立随机 bootstrap/runtime 密码、内部 CA、两套 API TLS 证书及独立会话 pepper。现有目录或专用卷存在时，prepare 拒绝，不能用删除卷/重新生成密钥来“修复”认证失败。

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
sudo docker compose up -d api-aliyun caddy
```

bootstrap 使用经过完整阅读的 frozen v1 DDL 和 v2 增量 DDL 安装 32 张表。只接受新且独立标记的空库；已有安装只核验，不修补、不接管、不重设密码。校验角色权限/所有权、全部强制 RLS、元数据指纹、匿名用户不可读、verify-full TLS。首次运营账号需要管理员独立批准后创建，工具不会导入历史账号或合成演示账号。

只有 Caddy 公开 TCP 80、TCP/UDP 443。PG 不公开 5432，两个 API 不公开 8443；PG 仅在 internal 数据库网络。API 有独立出站网络供明确调用的 AI 等业务使用，但启动不会自行调用这些服务。`/aliyun/` 和 `/supabase/` 在网关被剥离前缀；内部继续 HTTPS，验证私有 CA 和上游主机名，保留原始 Host，Uvicorn `proxy_headers=False`。平台自身的真实共享认证限流 readiness 检查仍生效。

## 可选 Supabase 通道

只提供**已经配置好 v2/RLS 的现有最小权限 runtime** SQLAlchemy URI（`postgresql+psycopg`、`medical_app_platform_runtime` 或 pooler 后缀账号、5432 直连或会话池、`sslmode=verify-full`）和管理员从数据库设置取得的 CA 文件，不提供管理密码，不自动修改远端 DDL；拒绝 6543 事务池以保持既定连接语义：

```sh
cd /opt/medical-app/source
sudo python3 -m tools.aliyun_platform.configure supabase --runtime-url-file /root/runtime-uri --ca-file /root/database-ca.crt
cd /opt/medical-app
sudo docker compose --profile supabase up -d api-supabase
```

两个 API 的密钥目录互不挂载，Supabase 容器也不加入 Aliyun 数据库网络。未启用该 profile 时 `/supabase/` 返回上游不可用，不会回退到 Aliyun 或本地 SQLite。配置工具不会覆盖已经存在的 URL/CA，源文件应由管理员按自己的凭据管理规范保管。

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

备份为 PG17 custom 格式，不在命令行传密码，连接使用 verify-full。每次恢复到新生成、带独立所有权标记的 `medical_app_verify_<随机值>` 数据库；`pg_restore --exit-on-error --single-transaction` 成功且核验 32 表强制 RLS、可读计数后，才删除**本次成功的临时验证库**、写入 SHA256 与 verified 记录。失败备份/验证库保留供人工复查，脚本不自动清理任何归档；没有已验证状态的副本绝不会因保留策略被删除。运行中业务仍可写入，报告的记录数是恢复副本内的值，不是另一次时间点的在线计数。

备份包含敏感记录，文件保持 root-only 权限。应另行批准加密异地备份、保留周期和恢复演练；仅同盘备份不能防主机/磁盘丢失。容量达到 85% 或空闲少于 2 GiB、36 小时无验证备份、私有证书剩余不足 30 天时，capacity 返回失败供监控接收。脚本不会自动删除旧备份，管理员须有告警接收人。

随附 systemd service/timer 可在管理员审核后安装到 `/etc/systemd/system/` 并启用：每日北京时间 03:15 加 0～15 分钟随机延迟备份、每小时加 0～5 分钟随机延迟检查容量。备份日历显式使用 `Asia/Shanghai`，不依赖主机的默认时区。本次实例已在首次手动备份与恢复核验成功后启用这两项定时器；容量检查目前通过日志与退出状态报告，没有配置外部消息接收渠道。单机资源限制只是保守起点，不是并发承诺。

## 官方依据（2026-09-08 核对）

- [Caddy TLS issuer/profile](https://caddyserver.com/docs/caddyfile/directives/tls)：支持显式 ACME profile；该规范目前仍标注实验性。
- [Caddy HTTPS 上游和 Host](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)：2.11 起 HTTPS 上游默认改 Host，因此本配置显式保留 `{hostport}`；内部 CA 使用 `tls_trust_pool file`。
- [Let's Encrypt profiles](https://letsencrypt.org/docs/profiles/)：`shortlived` 支持 DNS/IP，证书有效期 160 小时，必须依赖可靠自动续期。
- [Supabase SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement)：verify-full 同时验证 CA 与主机名，需要显式数据库 CA。
- [Supabase changelog](https://supabase.com/changelog)：实现前已检查；Markdown 入口不可用时采用官方 HTML 和文档检索。

离线自动化测试位于 `tests/server/test_aliyun_deployment.py`。本次正式云上线、ACME 生产签发、真实 Supabase 连通和公网业务验收已另行实际执行，结果见验收记录；未来的新部署也必须独立验收，不能直接沿用本次结论或以静态测试代替。
