# 本机离线验收记录 — 2026-09-08

结论：部署工具的本机隔离验收通过；**没有连接或部署任何真实云数据库，也没有签发 ACME 正式证书**。

环境：本机 Docker Desktop 服务端 29.7.2，Linux x86_64，PostgreSQL 17.11（Debian 17.11-1.pgdg12+2），Caddy 2.11.4。Caddy 使用主任务在目标主机核对过的 ECR 镜像摘要；Compose 同时固定目标 PG 镜像摘要。

## 已执行

- `tests/server/test_aliyun_deployment.py`：21 项通过。包括新目录/保留卷拒绝覆盖、四个独立随机密钥、默认 staging、正式切换显式确认、Supabase 运行账号/CA限制、32 表强制 RLS、仅 80/443 公开、数据网络隔离、源码白名单和不删除归档约定。
- 全部新增 Python 文件格式与静态检查通过。
- `docker compose config`（含 maintenance/supabase profiles）通过。
- 白名单 Dockerfile 重新构建成功；依赖均由带 SHA256 的 Linux 锁安装，`pip check` 通过。实际业务 API 的导入、启动和后续测试使用最终构建镜像，没有用完整工作区覆盖应用代码。
- `offline_smoke --confirm-local`：使用随机名称、所有权标签、本机 internal 网络、新空卷生成真实私有 CA 和证书；首次初始化 32 表成功，第二次只核验不重设密码；实际生产 readiness 验证受限运行角色、强制 RLS、共享认证限流成功。
- 非 root、只读根文件系统、删除 Linux capabilities 的 API 容器启动成功；使用私有 CA 验证内部 HTTPS，并实际访问 `/health/ready` 成功。
- 创建了仅本轮测试使用的随机密码虚构会员；实际 `pg_dump` custom 归档与 `pg_restore --exit-on-error --single-transaction` 恢复成功，恢复副本中 1 位合成会员和完整 32 表通过核验。只删除了本轮成功的临时验证库；源库和备份直到实验资源整体清理前均保留。
- Caddy 2.11.4 `validate` 读取生成的 staging 配置与私有信任池成功；未启动 ACME，不联系公网 API。
- 每轮实验结束只清理本轮生成且标签一致的容器、internal 网络和卷；已再次确认没有遗留 `medical-app.offline-review` 容器/卷。

## 明确未覆盖

公网入站、阿里云安全组、真实服务器资源余量、ACME staging/production 签发与自动续证周期、Supabase 真实运行 URI/CA、真实 AI/天气调用、正式运营账号、真实历史数据导入、systemd 定时器安装和告警接收、异地加密备份。以上需主任务/管理员后续独立执行并留存证据。

内部证书仍是 1 年有效、提前 30 天检查提醒，由管理员轮换；不会声称已有自动私有证书轮换。数据库备份默认全部保留，不自动删除未验证或已验证归档，容量告警需要有人接收；同盘副本不能代替异地备份。
