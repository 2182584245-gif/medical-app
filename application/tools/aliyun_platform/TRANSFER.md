# 管理员完整平台复制（schema 6 / platform_0002）

这是独立管理工具，不是会员同步、公开 HTTP/RPC 接口，也不读取默认本地库。所有命令只处理管理员明确选择的源、目标和目录。复制后不删除源。普通会员的本人首次导入、会话缓存与离线队列是不同功能。

## 安全契约与计划

- 固定迁移 29 张业务表的全部已审核列，包括账号 Argon2id 密码哈希、会员/顾问关系、服务/预约、商品/订单、记录、聊天、审计、个人偏好、购物车/收藏，以及报告/聊天原始二进制文件。原始密码不参与。
- 不复制 `platform_sessions`、`auth_rate_buckets`、`rpc_requests`、API Key、连接串、运行/管理凭据、pepper 或 DPAPI 凭据。疑似密钥字段/内容（包括加密 key 字段）会阻止整个预检，不会偷偷忽略某些行。源文件、数据库字段缺失或不一致也整体拒绝。
- `prepare` 使用只读一致性快照，输出 `plan.json` 和经过认证回读的加密源/目标备份。计划明确提供 `source`、`target`、`status`、29 表计数、内容 SHA256、ID 映射、冲突、资产落点和完整 `plan_sha256`。公开计划不包含行值、密码哈希或密钥；其中的账号 ID 和文件路径仍需按管理员资料保管。
- 默认 `empty-only`：任何业务表非空即阻止。`append-only` 必须显式选择；给导入实体分配独立新 ID、重写固定外键及已知 JSON/audit 引用，保留目标所有旧行。重名账号、SKU、订单号、全局配置或无法判定的历史引用都停止等待审阅，不会自动认定“同名是同一人”。
- `apply` 必须再次提供完整计划 SHA256，事务内锁定全部业务表、复查目标结构/身份/内容指纹后才插入；逐行、原件 SHA256、外键及序列验证通过才提交。遇失败不自动重试；先 `status`。不提供覆盖、清空、任意 SQL、自动人群合并开关。
- 外部商品/头像图片必须显式提供资产根目录；安全图片随加密源包携带，落地到新的内容寻址目录并更新引用。绝不从 URL 下载、不走链接/越界路径、不覆盖原图。数据库事务失败时新暂存图片保留供恢复，不自动删除；因此文件系统与数据库不是同一个跨资源事务。云图片展示仍需部署方将该资产目录接入受限文件读取服务。
- 本工具是一次性“快照复制”，不会承诺持续双向复制。源在快照时点之后的新写入不会包含；管理员应安排维护窗口。当前安全上限为序列化包 512 MiB、总业务行 100 万、报告原件 15 MiB/个、聊天附件 10 MiB/个、图片 15 MiB/个及 16777216 像素；超限整体拒绝而非部分搬运。

## Windows 源加密导出：Supabase 管理密码不离开原账户

以下均在源码根目录，用已安装管理依赖的 Python。先手动建立一个全新的、仅本管理员可访问的工作目录。`keygen` 独占创建 32 字节随机 key；不能以数据库密码/API pepper 代替。

```powershell
python -m tools.aliyun_platform.transfer keygen --key-file "D:\私有迁移工作目录\transfer.key"
python -m tools.aliyun_platform.transfer export --source-management-profile "C:\本机既有DPAPI管理配置\supabase-connection.json" --source-label confirmed-supabase-source --target-deployment-id <已核对的32位阿里云部署ID> --directory "D:\私有迁移工作目录\source-export" --key-file "D:\私有迁移工作目录\transfer.key"
```

源只读连接使用 `verify-full`。`source-export` 必须不存在；生成 `export.json` 和 `source.hltransfer`。独立 AES-256-GCM key 不在包内；随机 nonce、格式/schema、导出 SHA、源 SHA 和指定目标部署身份都经过认证绑定。不要传送本机 DPAPI profile、管理密码或整个源库文件。若源实际引用外部图片，必须增加 `--source-assets <明确资产根目录>`。

将这两个文件通过已授权、已固定主机身份的 SSH/SCP 放入服务器全新私有源包目录，不以普通云盘或公开链接分发。独立 key 单独通过标准输入交接：先安装 `receive_transfer_key.py` 与同目录 `guard.py` 到服务器 `/opt/medical-app/administration/tools/aliyun_platform/`，然后：

```powershell
python -m tools.aliyun_platform.handoff_transfer_key --host <已核对公网主机> --deployment-id <32位部署ID> --export-sha256 <export.json中的64位export_sha256> --key-file "D:\私有迁移工作目录\transfer.key" --ssh-key "C:\已授权密钥\id_ed25519" --known-hosts "C:\已固定主机\known_hosts" --ssh "C:\Windows\System32\OpenSSH\ssh.exe" --confirm
```

接收端核对 `/opt/medical-app/deployment.json`，仅接收恰好 32 个随机字节，独占创建 `/opt/medical-app/administration/transfer-keys/<export_sha256>.key`。目录 root:root 0700，文件 0400。不会覆盖已存在 key；返回不确定时先检查服务器，禁止盲目重复。SSH 禁用用户配置/代理/转发/复用连接，仅信任显式 known_hosts，密钥内容不进入 argv/env/日志。

## 独立 Linux 管理镜像与目标模板

先按部署流程重建并核验本机 `medical-app-aliyun-api:v2`，再在源码根目录构建可选维护镜像：

```sh
docker build --pull=false -f tools/aliyun_platform/Transfer.Dockerfile -t medical-app-platform-transfer:v1 .
```

专用 deny-all 文件清单仅加入 3 个迁移模块和 CLI；不加入任何数据库、导出包、DPAPI、UI/OCR/语音源模块或管理凭据，也没有新公网入口。该派生镜像只增加精确锁定并校验 wheel 哈希的 `cryptography==50.0.1`，不修改桌面或基础 API 依赖。`requirements-transfer-linux-lock.txt` 的 Linux x86_64 wheel SHA256 为 `51afcfceb15597cf2635068e4ac9a56b2abde622edde17f37d85fd7b5306497a`；实际使用以锁文件为准。

公开目标 JSON 模板见 `transfer-target-aliyun.example.json`。其内容仅固定数据库身份和容器内密码/CA文件路径；不含密码。复制为全新管理员 `target.json`，不要给桌面程序。

目标必须为内部 `postgres:5432` / `medical_app_aliyun` / `aliyun_bootstrap`，CA 与密码分别读取 `/run/api-secrets/ca.crt`、`/run/db-secrets/bootstrap-password`。仅这个固定密码路径兼容部署工具创建的 postgres UID999/0400 所有权；其他密码文件仍要求 root 所有且无组/其他权限。维护容器使用 root 和只读密码挂载，DAC_OVERRIDE 仅用于读取已有 UID999/10001 的私有部署文件。不允许任意 DSN、hostaddr、PG 环境覆盖、关闭 TLS 或 6543 事务池。

下面的 `<...>` 需由已验证部署状态填入；`<database-network>` 必须是该 Compose 部署的内部 database 网络，不能附加公网/outbound 网络。`<private-source>` 已存在且仅含源包；`<private-work>` 已存在且是全新私有计划工作父目录；`<private-key-directory>` 是上述 key 的 root 私有目录。挂载只读源/密钥/凭据，唯一可写挂载为专用计划目录；不要挂载 Docker socket、整个部署根或本机用户目录。

```sh
docker run --rm --pull=never --network <database-network> --user 0:0 --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges --log-driver none --memory 1g --pids-limit 128 --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount type=bind,src=<private-source>,dst=/source,readonly \
  --mount type=bind,src=<private-work>,dst=/work \
  --mount type=bind,src=<private-key-directory>,dst=/keys,readonly \
  --mount type=bind,src=/opt/medical-app/secrets/postgres,dst=/run/db-secrets,readonly \
  --mount type=bind,src=/opt/medical-app/secrets/api-aliyun,dst=/run/api-secrets,readonly \
  medical-app-platform-transfer:v1 python -m tools.aliyun_platform.transfer prepare \
  --source-export /source --target-profile /work/target.json --directory /work/plan \
  --key-file /keys/<export_sha256>.key
```

`prepare` 不写目标。先读取计划确认源/目标、目标 29 表全为 0、`status=review_required`、`conflicts=[]`、预期计数及完整 SHA。保持**相同镜像、网络、挂载**，将命令 `prepare --source-export /source ...` 改为：

```sh
python -m tools.aliyun_platform.transfer apply --target-profile /work/target.json --directory /work/plan --key-file /keys/<export_sha256>.key --confirm-plan <完整plan_sha256>
```

这一步才写入目标。最后同样容器条件运行 `status`（参数同 apply，去掉 `--confirm-plan`），应为 `all_planned_rows_present`；同时保留 `committed-*.json`。`status` 只验证计划行仍存在，不声称阻止其他管理员之后合法变更。提交回报丢失时先 `status`，不得自动再次导入。

若有图片，额外只挂载已核对的新资产目录为 `/assets`，在 prepare/apply/status 均传 `--target-assets /assets`；需目标写入时该目录可写。既有目标图片也必须完整备份，不能丢失。

## 反向、直接跨云、本地与非空目标

同一个核心支持任意 PostgreSQL 源到 PostgreSQL 目标：`prepare --source-profile <固定源JSON> --target-profile <固定目标JSON>`，分别读取管理员显式提供的只读密码/CA文件。源、目标都严格 TLS；数据库必须是受本项目标记保护的 v2 结构和 schema 所有者管理连接。Aliyun→Supabase 是同样的全量业务复制，不要求把管理密码给桌面/API。若两云连接不能在管理员认可的同一受控环境建立，则不要擅自放宽路由/交出凭据；Windows 源导出桥当前专为 DPAPI Supabase→绑定 Aliyun 部署设计。

本地入口是 `prepare --source-sqlite <明确schema6库> --source-assets <明确根目录>`；工具不会初始化、升级、修改源库，也不自动把本地历史上传云端。操作本地健康原件需要单独明确源/目标授权。加密方案使用独立 `--key-file`，或仅同一 Windows 账户使用显式 `--dpapi`。

非空目标只有显式 `--mode append-only`。可提交无业务行内容的 `--choices review.json`：

```json
{"renames":{"users":{"12":"reviewed-new-account"},"products":{"3":"reviewed-new-sku"},"orders":{"9":"reviewed-new-order"}},"json_id_fields":{"reviewed_member_id":"users"},"entity_types":{"reviewed_old_member":"users"}}
```

这些映射只允许已存在源 ID / 固定已审核业务表，不接受 SQL。每次修改选择都必须新建计划目录、重新生成加密备份、重新确认 SHA。人是否同一人的合并、全局配置冲突等没有自动覆盖策略；保留两边并阻止操作是明确默认。

## 验收边界

所有开发验收仅用新建合成 SQLite 和本机带所有权标记的临时 PostgreSQL 17 容器，不接触云端/真实用户库。覆盖 29 表/原件/哈希、非空追加、ID映射、未知引用拒绝、凭据拒绝、原图保留、原子回滚、改变目标/重复导入拒绝、源包目标错绑、AES 篡改拒绝及 Linux 私有 key 权限。生产实际运行、最终业务验收与另行完整数据库备份由已授权管理员执行。
