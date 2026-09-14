# 六类事实记录增量升级：SQLite 7 / platform_0003

这是管理员的升级手册，不是已经在真实云执行的证明。本轮工具与测试仅使用新建合成库；
实际部署、备份、维护窗口和目标身份须由管理员明确确认。

## 唯一结构变化

`life_records.category` 在 diet / water / activity / sleep / environment 后增加 medical。
医疗项仍使用原 `occurred_at` 与 `details_json`，只记已发生的看病、用药等事实：
`event_type=consultation|medication|other`，可选 `name` / `description` / `location`。
没有新增药量决策、诊断、处方或药物提醒。六类在录入、统计和已授权 AI 上下文中平级。
环境温度 0 是有效值，未填数值不当作 0。

云端仍是 32 张平台表、29 张可复制业务表。没有新表、列、权限或角色，旧 v1/v2 迁移与
稳定归属标记保持不变。提醒沿用原有 `none/daily/weekly/monthly`，没有新 CHECK 或时区列。

新 API 启动及 readiness 会核对已验证的六分类 CHECK；未升级的 v2 不能启动新版业务服务。
转移工具可只读接受原 SQLite 6 或 7；只有目标明确通过六分类约束核验后才可导入医疗记录。
没有医疗行的原 schema 6 示例仍可预检/导入；绝不靠改 PRAGMA 数字伪装升级。
备份工具接受已验证的 v1/v2/v3，并比较真实 catalog / 约束，未降低旧备份校验。

## Supabase：本轮只退役运行连接，不升级来源库

1.5.0 只保留本地和 Aliyun 模式。停止旧 `api-supabase`，在 Caddy 将原 `/supabase`
及其子路径改为静态 410；新客户端不能选择该路线，旧凭据不再交付运行服务。
**不执行 Supabase inspect / migrate / provision，不删除或改动其原始数据库、备份和管理配置。**
历史管理员工具及旧结构校验保持兼容，仅供将来另外授权的管理/恢复任务使用，
不能因为它们支持新版结构而把本次 Aliyun 维护扩大成第二个云库的迁移。

## Aliyun：维护容器内显式 plan / apply

先在 `/opt/medical-app` 确认 `deployment.json` 与已审核目标一致，安排无业务写入的维护窗口，
完成现有 `tools.aliyun_platform.maintenance backup` 与恢复报告核验。
部署根目录和 Compose 文件沿用现有安装，不重新 prepare / init，不删卷，不重新采集基线。
以审核后的新版源码构建并核对 `medical-app-aliyun-api:v3`，保留旧 v2 镜像供明确的回滚方案使用。
新 Docker allowlist 已包含升级模块，但应先升级数据库，再启动新版 API。

以下 `<部署ID>` 必须用当前已人工核实的 32 位值，不能复制其他环境的计划。
新建且权限 700 的 `/opt/medical-app/medical-upgrade` 目录仅存无密码的计划和结果。
命令从 `/opt/medical-app` 运行，沿用该目录已审核的 `compose.yaml` / 环境配置：

```bash
docker compose --profile maintenance run --rm --no-deps \
  -v /opt/medical-app/medical-upgrade:/work \
  bootstrap python -m tools.aliyun_platform.upgrade_medical plan \
  --deployment-id <部署ID> --plan-file /work/plan.json

# 人工审核 plan.json 的部署ID、旧/新版本、每表行数、only_change 与完整 SHA256。
docker compose --profile maintenance run --rm --no-deps \
  -v /opt/medical-app/medical-upgrade:/work \
  bootstrap python -m tools.aliyun_platform.upgrade_medical apply \
  --deployment-id <部署ID> --plan-file /work/plan.json \
  --confirm-plan <计划内完整64位SHA256> --result-file /work/result.json

docker compose --profile maintenance run --rm --no-deps \
  bootstrap python -m tools.aliyun_platform.upgrade_medical status --deployment-id <部署ID>
```

容器使用既有只读 bootstrap 密码、内部 CA、专用 database 网络，不公布 5432/管理端点。
plan 是只读一致性快照；apply 取得 32 表固定顺序锁，复核旧 DDL / catalog 标记、全部行指纹、
原约束及计划后才执行。仅当六分类约束、排除这一个约束的其他 catalog、全部行均正确，才写新
运行角色结构标记。旧标记不匹配、未审核触发器、计划后任何行变化均拒绝，绝不重置旧 fingerprint。
整笔事务失败回滚；结果文件独占新建，原计划不覆盖。最多 100,000 行 / 128 MiB 是保守维护
核验边界，超限直接拒绝，不做局部升级。计划包含敏感业务行的单向指纹，仍应限管理员阅读。
重复 apply 旧计划会拒绝，`status` 用于无写入确认已完成；不要因拒绝而换新基线。

若有 API 请求、会话清理或其他后台写入，所有行指纹可能变化；必须停止这些写入后重新 plan，
不可省略指纹。常规 PostgreSQL pg_isready 本身不写业务表。工具不自动做备份或停服务。

## 原示例 SQLite 6 → 7

原 `outputs/synthetic-demo-20260908/data/app.db` 保持不变。发布前将**已验证合成来源**的数据库与
附件复制到一个新的输出目录，在副本调用 `Database(副本路径).initialize()`。
初始化处于原有事务，逐行核对、保留 ID/索引，外键验收后才将 user_version 设为 7；失败原样回滚。
不初始化真实默认库、不覆盖旧示例目录、不复制 DPAPI / Key。新的示例清单与完整 hash 应重生成；
原来要求 schema 6 的示例包验证器须由发布负责人显式更新为 7 后重新验收，不能复用旧 EXE。

## 删除接口与离线边界

`health.delete_life_records(user_id, record_ids)`、`delete_reminders(user_id, reminder_ids)`
接受 1–100 个不重复的严格正整数 ID，按固定顺序逐项核验本人归属，同一事务删除并逐项审计。
混入不存在或他人数据时整批回滚。成功返回 `deleted_ids` / `count`，RPC 原 UUID 重试不重复审计。
提醒/记录 UI 支持 Ctrl / Shift 多选、数量与条目预览、明确确认；这些删除不加入离线重放白名单。
会话多删的服务和 UI 由聊天模块提供，同样已注册远程固定白名单，不提供任意 SQL。

## 本机合成验收

```powershell
$env:PYTHONPATH="$PWD\src;$PWD"
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests/test_medical_records_v7.py -q
.venv-server\Scripts\python.exe -m pytest tests/server/test_medical_records_rpc.py tests/server/test_medical_upgrade.py -q
$env:MEDICAL_APP_RUN_TRANSFER_PG_TESTS='synthetic-local-only'
.venv-server\Scripts\python.exe -m pytest tests/server/test_medical_records_pg.py -q
```

最后一组只接受本机 Docker Desktop 命名管道，用新建、带随机归属标签的 PostgreSQL 17 tmpfs
容器和合成密码；不接受远程 URL，不碰云库或真实本机数据库。测试完成只清理该次精确自有资源。
