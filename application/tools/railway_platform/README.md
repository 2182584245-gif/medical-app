# Railway 正式后端构建入口（公开文件，无凭据）

本目录只准备正式后端的构建规则和配置检查表，不代表镜像已构建、服务已启动、
数据库认证成功或公网业务验收通过。无密钥探针成功只证明当时的基础网络条件。

## 2026-09-07 实际部署交接状态

- 项目 `astonishing-celebration`，正式服务 `medical-app`，环境 `production`。
- 正式服务 ID：`afa6011e-0269-4fff-9b18-9a7b57f00093`；已绑定本仓库 `main`。
- 已分配域名 `medical-app-production-3afc.up.railway.app`，目标端口 8080；
  **分配域名不代表服务上线**。正式容器尚未构建/运行，不能作为已验收地址交付。
- 页面暂存了源码根目录、Dockerfile、就绪检查及 7 项非秘密变量，共 15 项待部署变更；
  这些是当时界面状态，继续操作前须重新核对。没有配置 TCP Proxy。
- 本机专用运行账号已通过数据库与认证就绪检查，保留 `sslmode=verify-full`；
  这不是 Railway 容器到数据库的验证，也没有迁移本地历史数据。
- 临时本机交接页在初始导航阶段被 Edge 以 `ERR_BLOCKED_BY_CLIENT` 阻止，
  没有触发一次性揭示；运行连接串、令牌 pepper 和 CA 尚未传入正式服务。
  临时服务已停止，不得绕过浏览器或杀毒软件限制来继续。
- 尚待：用户完成受控凭据交接、正式 Linux 构建、公开就绪检查、业务验收及精确测试清理，
  然后才更新桌面默认地址、构建并检查新发行包。不要启动缺少运行凭据的待部署变更。
- 官方支持仅限项目某一环境的 Project Token，可作为另外的授权方式；必须由用户明确
  授权并通过本机隐藏输入接收，不要放入聊天、源码或发行包，不能提取浏览器登录凭据。
  [Railway 官方 API 授权说明](https://docs.railway.com/integrations/api)

`server/railway_secret_handoff.py` 是本机运维辅助工具，已由 Docker 专属忽略规则排除；
不属于桌面软件，不应在云端启动。初始页面不读秘密；不得把其本机一次性 URL 视为
公网部署地址，也不得截图、输出或保存揭示后的内容。

### 项目令牌方式与当前账户限制

用户已同意项目令牌及本机隐藏输入方式。随后实际检查发现：项目设置的 Tokens 页面
明确提示 `Your account needs to be verified to create project tokens`；套餐页显示
`Limited Trial`，账户页同时显示 GitHub 已连接。官方 `/verify` 入口回到了套餐页，
本轮未取得可完成的免费验证流程。这是账户能力限制，不是 Python 依赖缺失。
不得把已连接 GitHub 等同于已通过 Railway 自动验证；不得改用更广权限账户令牌、
绑定卡片、购买方案、提取浏览器凭据或绕过安全屏障来替代用户授权。

工具 `server/railway_connection.py` 只负责接收令牌和只读核对范围，不会创建令牌、
解锁账户、部署服务、配置数据库变量或导入历史数据。该工具也被云镜像排除。

在 Railway 允许创建项目令牌后，由用户在项目 `astonishing-celebration` 的
Settings → Tokens（项目设置 → 令牌）创建仅属于 `production` 的 Project Token，
建议命名 `medical-app-local-deploy`。不要使用 Account Token 或 Workspace Token。
[项目令牌范围与授权头](https://docs.railway.com/integrations/api)、
[试用与自动验证说明](https://docs.railway.com/pricing/free-trial)

在本机源项目的交互式 PowerShell 中执行（命令不包含令牌）：

```powershell
$env:PYTHONPATH = $null
& '.\.venv-server\Scripts\python.exe' -m server.railway_connection configure
& '.\.venv-server\Scripts\python.exe' -m server.railway_connection check
```

也可双击 `tools/configure_railway.cmd`，它会先本机配置，再执行只读范围检查。
必须在有 `.venv-server` 的源项目目录运行；源码副本中的启动器不会偷偷调用别处的解释器。
`configure` 只通过 Windows 当前用户 DPAPI 加密保存 `.local/railway-project-token.json`，
不联网；已有配置默认拒绝覆盖，明确需要更换时使用 `configure --replace`。
`status` 只看配置是否存在，不解密、不联网。`check` 仅向固定 Railway 官方 HTTPS
端点发送一次 `projectToken` 只读查询，验证项目和环境 ID，拒绝重定向、范围错误与
GraphQL 错误，不打印原始响应或令牌；它不证明服务部署成功或服务级权限已经验证。
不要在命令参数、聊天、环境文件或截图中填写令牌；部署验收完成后可由用户在官网撤销
不再需要的令牌。Windows 当前用户加密不能防御以同一用户运行的恶意程序。

## 选用方案与目录关系

正式路线使用 **Dockerfile**，不是自动识别桌面项目的 Railpack。
固定基础镜像 `python:3.13.15-slim-bookworm`，与本项目 Python 3.13 范围一致。
官方 Python 镜像目录在核对时列出了 3.13.15 及 slim-bookworm 变体；本轮没有拉取镜像，
镜像标签存在不等于依赖在目标平台已经完成安装。标签不是不可变 digest，后续若需要
完全可复现构建，应在实际审核镜像后再锁 digest，不虚构摘要。
[官方镜像版本目录](https://raw.githubusercontent.com/docker-library/python/master/versions.json)

仓库中的结构应为：

```text
application/                         ← Railway Root Directory：/application
├── requirements-server.txt         ← 只安装这一份依赖
├── server/                         ← 后端 Python 源码
├── src/ollama_chat_app/             ← 后端复用的共享业务源码
└── tools/railway_platform/
    ├── Dockerfile
    ├── Dockerfile.dockerignore
    ├── public-settings.json        ← 人工配置检查表，不是 Railway 自动配置文件
    └── README.md
```

不要把 Root Directory 设置成 `/application/tools/railway_platform`：这样会找不到
共享的 `server`、`src` 和 `requirements-server.txt`。
Railway 的构建和启动命令以 Root Directory 为工作范围；自定义 Dockerfile 可以通过
服务设置指定。这里 Dockerfile Path 填 `tools/railway_platform/Dockerfile`；如使用服务
变量设置同一项，使用 `RAILWAY_DOCKERFILE_PATH=tools/railway_platform/Dockerfile`。
不要同时保留相互矛盾的构建器或路径配置。
[Railway 构建目录规则](https://docs.railway.com/builds/build-configuration)、
[Railway 自定义 Dockerfile](https://docs.railway.com/builds/dockerfiles)

## 依赖与构建内容

Dockerfile 只执行 `python -m pip install --no-cache-dir --only-binary=:all: -r
/app/requirements-server.txt`，随后 `python -m pip check`。不执行 `pip install .`，
不安装桌面 `requirements.txt`、`requirements-lock.txt` 或 `pyproject.toml` 中的依赖。
若目标平台缺少所需 wheel，应让构建失败后核查，不静默改为安装桌面依赖或关闭检查。

当前服务端直接依赖固定为 FastAPI 0.141.1、Uvicorn 0.52.4、SQLAlchemy 2.0.52、
Alembic 1.19.2、psycopg[binary] 3.3.5、argon2-cffi 25.1.0、pydantic-settings 2.15.0、
httpx 0.28.1、Pillow 12.3.0、pytest 9.1.1。以仓库 `requirements-server.txt` 为唯一来源，
不要另复制一份容易过期的依赖锁文件。此清单锁定直接依赖版本，尚非包含所有传递依赖
及 wheel 哈希的完整供应链锁；pytest 目前在原清单中，此部署规则没有擅自删改它。

相邻的 `Dockerfile.dockerignore` 采用默认拒绝的白名单，只允许服务端顶层 `.py` 和
必要的共享业务 `.py`。目录、缓存、模型和本机文件即使出现在本地根目录，也不在白名单：

- 不包含 `.local`、本地账号/聊天数据库、日志、备份、证书配置或私钥。
- 不包含桌面/安卓发行包、虚拟环境、模型资源、测试目录或 Git 元数据。
- 不包含桌面 UI、工作线程、启动器、AI 提供商实现、OCR 和语音模型。
- 不包含迁移执行文件；服务启动不运行建表、迁移、导入或测试账号创建。
- 后端复用 `providers.base` 的类型接口，不安装 Ollama，也不调用 AI 提供商。

Docker 官方支持与 Dockerfile 同目录的专属 `.dockerignore`，它优先于根目录忽略文件；
因此这里不修改项目根 `.dockerignore`。Dockerfile 使用明确的三个 COPY 来源，没有
`COPY . .`。注意：构建上下文过滤不能替代仓库安全，秘密仍不得先提交进 Git。
[Docker 构建上下文与专属忽略文件](https://docs.docker.com/build/concepts/context/)

## Railway 界面应设置的项目

| 项目 | 设置或检查 |
| --- | --- |
| Root Directory（源码根目录） | `/application` |
| Builder（构建器） | Dockerfile |
| Dockerfile Path（构建文件） | `tools/railway_platform/Dockerfile` |
| Start Command（启动命令） | `python -m server.platform_entrypoint`，与 Docker CMD 一致 |
| Pre-deploy Command（部署前命令） | 留空；禁止自动迁移或创建测试账号 |
| Healthcheck Path（就绪检查） | `/health/ready`；建议超时 120 秒 |
| Replicas（实例） | 1，后端 Uvicorn worker 也是 1 |
| PORT（监听端口） | 显式 `8080`；公共域名目标端口应一致 |
| 公共访问方式 | 经 Railway HTTPS 公共域名；不得开启 TCP Proxy 或其他直达端口 |

`public-settings.json` 是上述人工检查表，不会被 Railway 自动加载。
没有增加 `railway.json` / `railway.toml`：官方已将旧 Config as Code 标为弃用，
新服务使用服务设置或另行审核的 Infrastructure as Code。
[Railway 配置说明](https://docs.railway.com/config-as-code/reference)

## 运行期变量与安全门槛

只在平台的受控运行期变量中填写真实值；不要写入 Dockerfile、ARG、构建日志、Git、
桌面配置、截图或聊天文本。构建不需要任何数据库秘密，也没有构建参数读取它们。

- `PLATFORM_DATABASE_URL`：专用低权限运行账号的连接串，不是管理员账号；要求
  `postgresql+psycopg`、`sslmode=verify-full`，CA 路径指向
  `/tmp/medical-app-prod-ca.crt`。此文档不保存任何真实连接串。
- `PLATFORM_TOKEN_PEPPER`：平台独立令牌摘要秘密；与 AI Key 无关，不能随便重置。
- `PLATFORM_DATABASE_CA_PEM`：经过审核的数据库公共 CA 文本；由启动入口在 `/tmp`
  创建权限 0600 文件，再由数据库驱动严格验证证书，不在镜像里烘焙本机配置。
- `PLATFORM_ALLOWED_HOSTS`：JSON 数组，精确列出真实公网域名，不使用 `*`。该域名必须
  与 Railway 注入的 `RAILWAY_PUBLIC_DOMAIN` 一致，且符合单标签 `*.up.railway.app`。
- `PLATFORM_ENV=production`、`PLATFORM_REQUIRE_HTTPS=true`、
  `PLATFORM_RENDER_PROXY=false`、`PORT=8080`。
- `RAILWAY_SERVICE_ID`、`RAILWAY_PROJECT_ID`、`RAILWAY_ENVIRONMENT_ID` 应由 Railway 注入
  真实合法 UUID，不在仓库写值或虚构平台标识。

只有核实服务无 TCP Proxy / 其他直连入口，而且同项目环境中的服务都可信之后，才可
同时显式设置 `PLATFORM_RAILWAY_PROXY=true` 与 `PLATFORM_RAILWAY_EDGE_ONLY=true`。
这两个开关是部署边界确认，不是认证机制，不证明公网测试已经通过；不得启用 Render
模式来模拟 Railway。入口恢复 HTTPS scheme 时不信任客户端 IP 转发头，保留代理 peer
执行保守共享限流。`healthcheck.railway.app` 仅允许精确 GET `/health/live` 和
`/health/ready`。这些约束依赖主项目已合入并测试的 Railway 适配，不由 Dockerfile绕过。

## 为什么不继续用自动 Railpack

Railpack 的 Python 探测会读 `requirements.txt` / `pyproject.toml`，而本项目这些文件
属于桌面应用，可能导致安装 PySide6、OCR 和语音依赖。Python 版本也需要显式固定。
[Railpack Python 文档](https://railpack.com/languages/python/)

如果以后明确改回 Railpack，必须重新审核并提供相对 `/application` 的自定义配置：
用 `RAILPACK_CONFIG_FILE` 指定配置文件，固定 Python 3.13.15；`steps.install.commands`
必须完整替换为建立服务端虚拟环境、仅安装 `requirements-server.txt` 的命令，不能用
`"..."` 保留自动桌面安装；`steps` / `deploy.inputs` 必须使用源码白名单过滤，只复制
必要 `server` / `src` 和依赖虚拟环境，且设置 `PYTHONPATH=/app/src` 与
`deploy.startCommand="python -m server.platform_entrypoint"`。不能仅改 Start Command。
Railpack 官方说明了数组覆盖、`local/include/exclude` 图层过滤与自定义启动命令。
本目录没有交付或验证第二套 Railpack 配置，正式部署只选上面的 Docker 路线。
[Railpack 自定义配置](https://railpack.com/config/file/)

## 离线验收与上线顺序

先运行 `tests/server/test_railway_deployment_files.py`，确认公开配置、忽略规则和最小
源码导入闭包；测试只用临时目录，不启动平台应用或连接数据库。
随后由明确获授权的主流程在 Railway 构建：检查日志确实使用该 Dockerfile、Python
3.13.15、仅安装服务器依赖。配置秘密后验证 `/health/ready`，再运行经过审核的公网
合成账号业务验收及精确清理；只有真实验收成功后才填桌面默认服务地址。

若构建、平台计费、权限、数据库证书或 Railway 适配尚未就绪，应保持停止并报告。
不要将检查开关改成 false 来绕过门槛，不迁移用户历史数据，不运行付费 AI 测试。
