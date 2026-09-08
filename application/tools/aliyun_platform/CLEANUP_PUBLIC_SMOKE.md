# 公网合成验收的精确清理

此工具仅供已授权管理员清理 `public_smoke.py` 完整通过的本次验收数据，不是通用清库工具，也没有 HTTP / 任意 SQL 入口。开发测试未连接真实云端；以下实际命令只由管理员在核对回执及计划后执行。

## 边界与确认

- 回执必须 `status=passed`、固定 9 项验收全部通过、两次注册和 13 次操作完全 confirmed。只接受同一 32 位 run ID 的精确 `_a`、`_b` 两个会员账号，使用回执 ID，不按前缀批量删除，不假设 ID 从 1 开始。
- 固定允许删除：2 users、2 默认且无消息的 conversations、1 life_records、1 user_preferences、1 user_files、1 user_file_contents、2 audit_logs、3 已注销 platform_sessions、3 成功 rpc_requests，共 16 行。健康记录、图片尺寸/哈希、昵称、审计类型和 UUID 均核验与固定合成流程一致。
- 顾问绑定、订单、资料、额外记录、其他操作者引用、结构化整数 ID 引用、平台外部外键、用户触发器或重写规则均拒绝。JSON 引用扫描覆盖规范 `JSON_IDS` 字段及未知 `*_id` / `*_ids` 整数字段，不声称理解任意自由文本或字符串形式 ID；若管理员发现此类额外引用，应停止清理并另行审核。拒绝后不扩大范围、不手工删 ID、不放松验证，应重新调查实际差异。
- 原账号及所有其他 32 表现有行都在提交前逐行内容对照；共享 auth_rate_buckets 保留不变，不重置序列。真实密码哈希仅在内存里参与指纹，计划/日志不写密码、token、摘要凭据或二进制内容。
- `plan` 为只读一致性快照；`apply` 必须完整计划 SHA，取得全部平台表写排他锁后重新核对目标、结构、回执与所有行指纹，再按每个主键删除，提交前再验证保留行。任何差异整笔回滚。为限制管理容器资源，本工具上限为十万行、128 MiB 原始字段。
- 计划对全部行敏感：计划后真实登录、业务变更或认证限流变化会使计划失效；创建新计划文件重新检查。`health/ready` 的限流写探针总回滚，不改变指纹，但并发检查可短暂争锁；锁等待超过 5 秒即安全拒绝。
- 已有计划、回执导出或结果文件都不覆盖。结果文件已存在会在连接前拒绝。提交回报不确定时禁止自动重试，应人工只读核对精确 ID 与原资料；本工具没有自动“再次删除”模式。删除本身不提供撤销，保留已有管理员备份及计划/回执/结果。

## Windows：仅导出回执元数据

在源码根目录，用管理环境解释器、当前 Windows 用户的 DPAPI 读取本次回执。`<...>` 是需管理员填入的已知路径，不是秘密内容。

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD"
.\.venv-server\Scripts\python.exe -m tools.aliyun_platform.cleanup_public_smoke export-receipt --run-directory "<本次验收私有目录>" --destination "<全新元数据JSON路径>"
```

只读固定文件 `receipt.dpapi`、固定目的串 `aliyun-public-smoke/v1/receipt`。字段白名单及疑似凭据扫描通过才独占创建 JSON，绝不读取 `credentials.dpapi`、管理 profile 或 outbox。导出结果包含 `receipt_sha256`；JSON 仍含本次测试用户名和 ID，按管理员元数据私下传输。向阿里云只需此 JSON，不需 Supabase 管理密码、测试密码或任何 DPAPI 文件。

## Windows：Supabase 本机管理连接

继续使用已配置、已授权的 Windows DPAPI 管理 profile，管理密码不上传服务器。

```powershell
.\.venv-server\Scripts\python.exe -m tools.aliyun_platform.cleanup_public_smoke plan --receipt "<Supabase元数据JSON>" --management-profile "<既有DPAPI管理profile>" --plan-file "<全新Supabase计划JSON>"
```

人工核对 `status=review_required`、route、target 主机/数据库/结构指纹、run_id、两账号及所有精确删除 ID/计数、全部表计数和 `plan_sha256`。保持相同 profile、receipt、plan 路径，再显式确认：

```powershell
.\.venv-server\Scripts\python.exe -m tools.aliyun_platform.cleanup_public_smoke apply --receipt "<Supabase元数据JSON>" --management-profile "<既有DPAPI管理profile>" --plan-file "<已审核Supabase计划JSON>" --confirm-plan <完整64位plan_sha256> --result-file "<全新Supabase结果JSON>"
```

## Linux：阿里云私有维护容器

复用已审核 `medical-app-platform-transfer:v1`，不修改正在运行的生产镜像。将最终审核的 cleanup 模块只读绑定进去；`<database-network>` 必须是本部署内部数据库网络；`<cleanup-work>` 是已创建的 root:root 0700 专用工作目录，仅放本次 metadata JSON、公开 target.json 和新计划/结果，不挂整个部署根或 Docker socket。

目标 JSON 与 `transfer-target-aliyun.example.json` 相同：固定 `postgres:5432`、`medical_app_aliyun`、`aliyun_bootstrap`、`sslmode=verify-full`，密码/CA 从已授权私有挂载读取。额外 `--deployment-id` 必须与数据库所有权标记一致，不能只依赖 IP 地址。

```sh
docker run --rm --pull=never --network <database-network> --user 0:0 --read-only \
  --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges \
  --log-driver none --memory 1g --pids-limit 128 --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount type=bind,src=<已审核源码根>/tools/aliyun_platform/cleanup_public_smoke.py,dst=/app/tools/aliyun_platform/cleanup_public_smoke.py,readonly \
  --mount type=bind,src=<cleanup-work>,dst=/work \
  --mount type=bind,src=/opt/medical-app/secrets/postgres,dst=/run/db-secrets,readonly \
  --mount type=bind,src=/opt/medical-app/secrets/api-aliyun,dst=/run/api-secrets,readonly \
  medical-app-platform-transfer:v1 python -m tools.aliyun_platform.cleanup_public_smoke plan \
  --receipt /work/receipt-metadata.json --target-profile /work/target.json \
  --deployment-id <已核对32位部署ID> --plan-file /work/cleanup-plan.json
```

审核计划后保留相同镜像、网络和挂载，只将命令部分改为：

```sh
python -m tools.aliyun_platform.cleanup_public_smoke apply \
  --receipt /work/receipt-metadata.json --target-profile /work/target.json \
  --deployment-id <同一部署ID> --plan-file /work/cleanup-plan.json \
  --confirm-plan <完整64位plan_sha256> --result-file /work/cleanup-result.json
```

只有 `status=cleanup_committed` 且保留行核验成功才能声明清理完成。随后管理员另做精确账号不存在、原运营账号/会话/审计及所有原资料仍在的只读检查，保留报告。不要将本工具接入周期自动化。

## 合成复核

不设置额外变量时只运行纯合成单测，本机 PostgreSQL 用例跳过。显式启用时仅允许本机 Docker Desktop 命名管道，新建带随机所有权标记的临时 PostgreSQL 17 / tmpfs 数据库；禁止传入云 URL。

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD"
$env:MEDICAL_APP_RUN_TRANSFER_PG_TESTS = 'synthetic-local-only'
.\.venv-server\Scripts\python.exe -m pytest tests/server/test_cleanup_public_smoke.py -q
```
