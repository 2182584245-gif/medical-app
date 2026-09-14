# v1.5.0 示例资料：非空目标追加验收与管理步骤

本页不是部署回执。开发验收只在新建、带所有权标签、仅发布本机回环端口的 PostgreSQL 17 临时容器执行；不连接云端，不使用真实管理密码，不运行真实 AI。正式云执行仍由已获授权的管理员核对目标、维护窗口及计划后决定。

## 已验收的来源和范围

来源固定为 `outputs/synthetic-demo-20260909`，是独立合成示例，数据库结构版本 7。数据库 SHA256：

```text
e051bdcb2872d408f27f8dcdf0e5d062dd7f82bd43301676b00ece5e66da6c6e
```

独立测试 `tests/server/test_demo_release_append_pg.py` 先调用发行用严格示例校验器，再只读形成 29 表完整快照。它故意向已存在的合成目标追加，不采用空库假设：

- 目标预先已有全部 29 张业务表的资料、3 个独立账号、1 条记录、原始文件二进制等；账号、商品 SKU、订单号和各表 ID 与源故意冲突。
- 默认 `empty-only` 被拒绝；未经审阅的 `append-only` 也被拒绝。只有明确选择重命名和审计引用含义后才允许生成可确认计划，不合并人、不覆盖旧行。
- 通过实际管理 CLI 的 `prepare`、AES-GCM 源包/目标备份认证回读、错误 SHA 拒绝、`apply`、`status`、重复提交拒绝。目标采用 v3 真实约束、所有权标记与最小权限运行角色。
- 新增源资料精确为：5 个账号、156 条记录、209 条审计、6 件商品、2 个订单、4 项购物车、4 项收藏、16 个服务任务等；全部 29 表逐行与审阅后的映射结果比较。目标旧行在导入及随后登录后仍全部不变。
- 42 条固定业务外键逐条查无孤儿。六张商品 PNG 以内容寻址路径落地，逐文件核对 SHA256 并实际解码验证。目标原文件二进制保留。
- 五个原示例密码通过真实 `PlatformAuth.login`、token 身份解析和注销；使用最小权限 PostgreSQL runtime，不以管理员代替应用认证。计划中改名的账号使用新名称和原密码登录。
- `platform_sessions`、`auth_rate_buckets`、`rpc_requests` 不随迁移复制。后续认证会正常生成新的目标会话/限流记录，并更新导入账号的登录时间；这些不是从源搬来的安全状态。
- 来源 10 个发行资料文件的 SHA256 在全过程后保持不变；密码、token、Argon2 哈希、随机运行凭据均不写入测试报告或受版本管理的文件。

本机 fixture 使用回环连接，不声称检验公网传输 TLS；生产连接校验未改动。图片验收证明保存/路径/字节完整，不证明公网图片网关已接入。部署端必须把目标资产根保留在持久目录；需要云端展示原图时，应另验收已有受限文件分发入口，不能仅导入路径后删除图片。

## 本机可重复执行

在项目根目录、Docker Desktop 已正常运行时使用以下显式双重门禁。报告目录必须是 `outputs` 的**全新直接子目录**，重复执行要另取新名，不覆盖历史证据。不要使用 `--showlocals` 或日志输出业务行。

```powershell
$env:PYTHONPATH="$PWD\src;$PWD"
$env:MEDICAL_APP_RUN_TRANSFER_PG_TESTS='synthetic-local-only'
$env:MEDICAL_APP_RUN_DEMO_APPEND_PG_TESTS='synthetic-local-only'
$env:MEDICAL_APP_DEMO_APPEND_REPORT_DIR="$PWD\outputs\demo-append-pg17-<新的验收编号>"
& .venv-server\Scripts\python.exe -m pytest tests/server/test_demo_release_append_pg.py -q --tb=short
```

测试只接受已固定本机 Docker named pipe，不接受 URL/密码输入；容器和网络仅按匹配所有权标签的精确 ID 清理。报告包含 `report.json`、已审阅 `reviewed-plan.json`、冲突列表、选择文件及文件 SHA256 清单。计划绑定本次随机临时目标身份，**不能把本机计划或其 SHA 用于云端 apply**。

## 本次示例特有的审阅选择

真实示例源的审计 4、94 使用 `entity_type=member_profile`，`entity_id` 指向 `member_profiles.user_id`。这里没有另一个独立的 profile ID 序列。非空目标时必须明确说明该含义：

```json
{"entity_types":{"member_profile":"users"}}
```

这份 `cloud-choices-template.json` 只是已审阅的源语义，不预先允许任何名称冲突。如果实际目标没有同名账号/SKU/订单号，它通常足以排除这两项引用歧义；仍必须以实际新计划的 `conflicts=[]` 为准。

本机为了验证拒绝覆盖，额外明确使用了以下选择；**不要不加核对地套到真实云目标**：

```json
{
  "renames": {
    "users": {"1": "demo-operator-imported-local"},
    "products": {"1": "demo-product-imported-local"},
    "orders": {"1": "demo-order-imported-local"}
  },
  "entity_types": {"member_profile": "users"}
}
```

选择文件中的 `1` 是源 ID，不是目标 ID。ID 映射由计划根据目标当前最大编号生成。若有人要求“保留原名且覆盖同名账号”，本工具不提供该选项；需要另行授权和单独设计，不能用改名伪装身份合并。

## 管理员正式云预检和执行模板（本次未执行）

1. 确认用户已授权**只上传这份合成示例**、目标属于预期 Aliyun 部署、目标已完成 `platform_0003`、原有资料应保留。先安排暂停业务写入并完成现有平台备份/恢复校验。复制不是持续同步：源快照之后的更改不在此包中。
2. 在 Windows 先运行严格发行源校验并复核上述数据库 SHA。只通过已授权、固定主机身份的 SSH/SCP 将 `data/app.db` 和 `assets/products/demo-1.png` 至 `demo-6.png` 放到服务器全新的私有 `/source` 对应目录；保持相对目录结构。不要上传 `DEMO_ACCOUNTS.md/.txt`、DPAPI、默认本地数据库、连接配置或任何 Key。此路径是合成示例专用路径，不是本地真实历史上传入口。
3. 维护镜像必须由已核验当前 `medical-app-aliyun-api:v3` 派生的 `Transfer.Dockerfile` 构建，不能采用旧 v2 基础镜像。检查固定内部 database 网络、bootstrap 密码/CA只读挂载及无公网端口；绝不挂载 Docker socket。
4. 以独立 `keygen` 在 Linux 私有密钥目录创建新的 32 字节随机 key，密钥仅归 root、无组/其他权限，目录 0700、文件 0600 或更严。这个文件必须在源包和计划目录之外；不得复用数据库密码/API pepper。后续步骤只读挂载 key。可在 `--network none` 的同一维护镜像内执行：

```sh
python -m tools.aliyun_platform.transfer keygen --key-file /keys/demo-v150.key
```

5. 将公开目标模板 `transfer-target-aliyun.example.json` 复制到新的私有工作目录 `/work/target.json`；其中只应有固定目标身份与 `/run/...` 密码/CA文件路径。把上面只有 `entity_types` 的模板存为 `/work/review.json`。保持 `/source`、`/keys`、数据库密码/CA只读，只有专用 `/work` 和已核对的持久 `/assets` 可写。第一次 `prepare` 可以不带 `--choices`，先记录默认拒绝的原因；每次重新预检都必须使用尚不存在的计划目录。

以下尖括号全部需要替换成已核实的部署路径/网络，不是可直接照抄的默认值：

```sh
docker run --rm --pull=never --network <owned-database-network> --user 0:0 \
  --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges \
  --log-driver none --memory 1g --pids-limit 128 --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount type=bind,src=<private-demo-source>,dst=/source,readonly \
  --mount type=bind,src=<new-private-review-parent>,dst=/work \
  --mount type=bind,src=<private-key-directory>,dst=/keys,readonly \
  --mount type=bind,src=<confirmed-persistent-image-root>,dst=/assets \
  --mount type=bind,src=/opt/medical-app/secrets/postgres,dst=/run/db-secrets,readonly \
  --mount type=bind,src=/opt/medical-app/secrets/api-aliyun,dst=/run/api-secrets,readonly \
  medical-app-aliyun-transfer:v3 python -m tools.aliyun_platform.transfer prepare \
  --source-sqlite /source/data/app.db --source-assets /source --source-label confirmed-synthetic-demo-v150 \
  --target-profile /work/target.json --target-assets /assets --mode append-only \
  --choices /work/review.json --directory /work/demo-plan --key-file /keys/demo-v150.key
```

6. `prepare` 只读取数据库并生成经过认证回读的加密源包与目标备份；核对 `plan.json` 的实际目标部署 marker、schema 7、目标旧计数、源准确 29 表计数、ID 映射、六图落点、`status=review_required`、`conflicts=[]`。若有新冲突，停止并逐项审阅；修改选择后换**全新目录**重新 prepare，不改旧计划的 SHA。
7. 在同一镜像、网络、挂载下，将容器末尾命令替换为以下命令。必须使用**此次云端实际 plan 的完整 SHA**，不能复用本机验收 SHA：

```sh
python -m tools.aliyun_platform.transfer apply --target-profile /work/target.json \
  --target-assets /assets --directory /work/demo-plan --key-file /keys/demo-v150.key \
  --confirm-plan <实际云计划完整64位SHA256>
```

8. 再以相同挂载执行 `status`；去掉 `--confirm-plan`，其余参数不变。预期 `all_planned_rows_present`。保留加密备份、独立 key 和 `committed-*.json`；提交回复不确定时先 status，**不自动重试 apply**。数据库、所有既有资料、新资料范围计数和图片持久路径另做只读验收。只有管理员确认后再恢复业务写入。

导入事务会复查目标指纹、锁定 29 业务表、验证逐行内容与 FK/序列后提交；目标在计划后变动会拒绝，不会为赶进度覆盖。图片暂存和数据库不是同一个跨资源事务；失败时未引用的新图片保留供管理员判断，不自动删除已有或未验证资料。
