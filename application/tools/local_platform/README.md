# 本地容器实验环境（不是腾讯云正式部署）

这套配置只用于已安装 Docker Desktop 的 Windows 电脑。它把现有 Python 后端、真实
PostgreSQL 17、独立测试证书和随机测试凭据组合起来，不连接正式 Supabase，不读取
`.local`/DPAPI 配置，不导入电脑历史数据，也不调用付费 AI。

## 使用方法

在仓库 `application` 目录中，使用 CMD 或 PowerShell 逐条运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action Test
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action Status
```

首次需要下载官方 Python/PostgreSQL 镜像和 Python 依赖，可能需要几分钟。出现镜像下载
超时不等于 Docker 损坏；不要反复重装，不关闭证书校验或安全软件，不使用不明镜像源。

停止与重新启动（不删除数据）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action Stop
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\local_platform\local_lab.ps1 -Action VerifyPersistence
```

这里的 `-ExecutionPolicy RemoteSigned` 只作用于本次新启动的 PowerShell 进程，
不是修改全局执行策略，也不使用 `Bypass`。本机已验证该形式的 `-Action Check` 可用。
如果组织策略或下载来源标记仍然拒绝脚本，请保留提示并先核实来源，不关闭安全软件、
不执行全局 `Set-ExecutionPolicy`。也可以在已允许本地脚本的 PowerShell 7 中运行。

`Test` 只在这个实验环境的首次验收时运行一次，创建随机合成账号、聊天及附件。
首次成功后，后续检查一律使用 `VerifyPersistence`；它使用单独测试状态卷，验证
相同合成账号仍能登录并读取先前记录，重启前后都可以运行。再次运行 `Test` 会返回
`state_exists` 并拒绝覆盖已有测试状态，这不是数据损坏；请改用 `VerifyPersistence`。
测试密码与令牌不显示在报告中。如首次测试中途失败，可能保留部分合成记录；不要为了
重试自动删除或重置数据卷，先根据失败阶段检查原因。

## 组成及边界

- `prepare`：一次性生成专用测试 CA、API/数据库证书和随机口令。遇到未知或部分损坏的
  卷会拒绝继续，绝不自动重置已有口令。证书有效期为一年，仅供本机实验。
- `postgres`：独立的真实 PostgreSQL 数据库，不发布数据库端口，只接受加密 TCP 连接。
- `bootstrap`：固定连接本地 `postgres / medical_app_local`，核验数据库与服务器标记后
  初始化现有 27 张业务表、原 RLS 和专用低权限角色。不是正式 Alembic 云端迁移记录。
- `api`：现有 FastAPI 后端，非 root 用户、只读程序文件；不发布宿主电脑或公网端口。
  客户端与 API、API 与数据库两段均加密并验证服务端证书，**不是 mTLS 客户端证书认证**。
- `smoke`：容器内的独立客户端测试；无真实 AI Key，不进行模型生成。
- 业务容器仅连内部 Docker 网络，没有通往公网的默认网络出口。构建下载不受此限制。
- 运行密钥、数据库管理员口令、测试 CA 私钥分卷；API 不挂载管理员口令或 CA 私钥。
- `.env` 不自动加载：包装脚本指定空的 `compose.env`，配置不接受任意外部数据库地址。
- 包装脚本同时核验 Docker Desktop 的本地命名管道和 `desktop-linux` Buildx 构建器；
  只接受集成 `docker` 驱动、单节点、本地 `desktop-linux` 端点。构建显式指定这个
  构建器，拒绝远程 BuildKit、替代构建器及自定义 Docker/Buildx 配置路径等继承设置。
  临时构建环境变量在脚本结束时恢复，不更改全局构建器，也不创建或启动其他构建器。
  使用直接 `buildx build --builder desktop-linux --load` 构建本地镜像，再由 Compose
  `up --no-build` 启动，避开当前 Compose 对集成构建器的选择兼容问题，不推送镜像。

测试接口为隔离 Docker 网络内部的 `https://api:8443`，只供配套容器测试客户端访问，
**不是 Windows 浏览器可以打开的地址，也没有发布本机 18443 端口。**当前请用上述
`Test` 或 `VerifyPersistence` 脚本；正式桌面版本的云端地址不改。

本机 Docker 29.7.2 实测中，仅连接 `internal` 网络的容器虽然接受了
`127.0.0.1:18443` 的端口配置，但实际运行端口映射为空，Windows 连接失败。
因此本实验环境移除了无效的宿主端口配置，并保留无外网默认路由的隔离边界，
不为浏览器访问而给业务容器接入普通外网网络。
[Moby 官方仓库中的相关用户报告](https://github.com/moby/moby/discussions/53256)
描述了相似行为；它是相关报告，不代表官方对所有版本或部署环境的保证。

客户端使用专用测试 CA 验证证书，不关闭验证。**不要把测试 CA 安装进 Windows 系统
信任库，不要点击忽略证书警告，不要关闭桌面应用的证书检查。**宿主电脑界面连接、
Caddy 入口和正式域名属于后续独立部署步骤，本轮没有新增转发服务。

## 数据与资源

固定 Compose 项目名为 `medical-app-local-lab`。五个专用数据卷：

- `pg-data`：仅本轮本地合成业务数据。
- `ca-private`：只给初始化程序使用的测试 CA 私钥。
- `db-secrets`：本地数据库管理员口令及数据库服务端证书。
- `api-secrets`：本地低权限连接口令、会话保护密钥和 API 证书。
- `smoke-state`：只用于重启验收的随机合成账号凭据与记录标识。

`Stop` 和正常容器重建不会删除这些卷。Docker 镜像与卷会占本机磁盘；目前没有自动
删除测试数据功能。不要执行 `docker system prune` 或带 `--volumes` 的全局清理。
如确需删除，先确认仅删除这个实验环境的精确卷；删除后其中测试历史不可继续验证。

这些测试数据保存在 Docker 管理的命名卷中，**不在 `medical-app` 仓库文件夹内**。
只复制仓库文件夹到另一台电脑，不会迁移本实验环境的数据库、证书或测试凭据。
本阶段尚未交付整套数据卷的备份与跨设备恢复功能；现有便携桌面应用的数据目录和
迁移方式没有被本实验环境修改。

容器日志使用 `local` 驱动，每服务最多 3 个 10 MB 日志文件。时间显示使用
`Asia/Shanghai`（北京时间），业务数据仍沿用项目既有的 UTC 保存规则。

## 腾讯云上线前仍需完成

本套文件不可直接用于公网。后续需要单独配置正式域名、受信任的公网 HTTPS 证书、
Caddy 反向代理及来源验证、生产数据库凭据、系统更新、备份恢复与公网验收。
本地测试通过不等于腾讯云已部署或已验证网络；不会自动修改 Windows 安装包。

参考：
[Docker Compose 内部网络](https://docs.docker.com/compose/how-tos/networking/)、
[服务依赖与健康检查](https://docs.docker.com/reference/compose-file/services/)。
