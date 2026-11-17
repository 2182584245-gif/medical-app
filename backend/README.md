# 凡思 try1.2 / 11864 云端源码

该目录对应手机版 11864 的云端接口源码。相对线上 11863 镜像，11864 的实际发布覆盖了 `server/ai139_stream.py`、`server/ai139_tools.py`、`server/platform_ai_proxy.py` 和 `server/record_tasks11864.py`。这里也保留这四个文件依赖的记录意图、必填复核、照片处理、数据库与会员业务模块。Qwen ASR 的生产实例地址改为部署时提供的 `QWEN_ASR_ENDPOINT`，私有配置中的地址须一致，模型和服务端域名校验仍保留。

## 本地回归

需要 Python 3.13。安装 `requirements-dev.txt` 后运行以下命令。测试使用合成记录、图片和服务替身，无需线上账号或生产数据库。

```text
# Linux/macOS
PYTHONPATH=src:. python -m pytest -q

# PowerShell
$env:PYTHONPATH = 'src;.'
python -m pytest -q
```

测试覆盖记录意图处理、缺项复核、图文保存、草稿确认、分页与多草稿切换。期望收集 156 项。

## 数据库与部署

- `server/alembic.ini` 和 `server/migrations/` 存有可解析的历史 Alembic 链，从 `pilot_0001` 到 `platform_0006_fawnsee_expansion`。11864 快照中的 0006 与历史工程内容完全一致，缺失的祖先来自同一历史工程。要查看链可运行 `python -m alembic -c server/alembic.ini history`。
- 原阿里云服务使用独立的结构初始化和升级流程，不能根据 Alembic 文件推断线上版本表，也不能把本目录套到生产库执行升级。恢复原线上部署仍需核对原 11863 镜像、当前数据库结构契约、TLS、运行角色和后续功能的 schema 步骤。
- `requirements-linux-lock.txt` 来自历史 Linux API 构建基线，锁定 Debian Bookworm x86-64 / CPython 3.13.15 的二进制依赖。Dockerfile 和 `compose.example.yaml` 提供独立 API 容器模板。需由操作者提供已初始化并经过验证的 PostgreSQL，配置专用运行角色、HTTPS、受信任主机与数据库证书。模板只将 API 端口绑定到本机。
- 复制 `.env.example` 到受控的本地路径，填写数据库连接、随机 32 字节十六进制 token pepper、服务允许域名。数据库连接需要 `postgresql+psycopg`、`sslmode=verify-full`、容器内 CA 路径。以只读挂载提供 TLS、DeepSeek 加密材料、开发者加密材料和 Qwen 凭据。Qwen ASR 的私有挂载需要 `config.json` 和 `api-key`。语义检索需要有效的 Qwen VL 配置。
- 按需挂载有权使用的知识库和匹配版本的阅读资源包。用户照片、生产配置、备份、证书私钥和冻结的线上发布包都由操作者独立保管。
- 容器运行后，通过操作者配置的 HTTPS 入口检查 `/health/live` 和 `/health/ready`，再在隔离环境验证登录、聊天、保存记录和旧版客户端。本目录的检查范围不包括实际恢复线上服务。

`data/nutrition-catalog/` 提供 0006 所需的公开食品数据。数据文件中保留 FDA 来源及公共领域声明，以及 GI 研究数据的 CC BY 4.0 归属和条目来源。它们不含用户记录。
