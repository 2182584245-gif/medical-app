# 凡思移动端共享后端

该目录包含两端使用的云端 API。11868 加入无需手机号的会员注册和隐私政策入口。11870 修复向旧记录追加照片、口语补答及冲突字段保留。相对线上 11863 镜像，11864 的实际发布覆盖了 `server/ai139_stream.py`、`server/ai139_tools.py`、`server/platform_ai_proxy.py` 和 `server/record_tasks11864.py`。这里也保留这四个文件依赖的记录意图、必填复核、照片处理、数据库与会员业务模块。Qwen ASR 的生产实例地址改为部署时提供的 `QWEN_ASR_ENDPOINT`，私有配置中的地址须一致，模型和服务端域名校验仍保留。

## 本地回归

需要 Python 3.13。安装 `requirements-dev.txt` 后运行以下命令。测试使用合成记录、图片和服务替身，无需线上账号或生产数据库。

```text
# Linux/macOS
PYTHONPATH=src:. python -m pytest -q

# PowerShell
$env:PYTHONPATH = 'src;.'
python -m pytest -q
```

测试覆盖记录意图处理、缺项复核、图文保存、草稿确认、分页与多草稿切换。还会检查数据库迁移能否离线生成 SQL。提供 `TEST_MOBILE_POSTGRES_DSN` 后，会额外对空 PostgreSQL 数据库运行迁移和会员隔离测试。测试目标须为本机且库名以 `fawnsee_test_` 开头。

## 数据库与部署

### 初始化新库

Alembic 迁移链从 `pilot_0001` 到 `platform_0008_password_signup`。0008 创建免手机号注册会员标记及其访问策略。0007 补齐会员记忆和 AI 偏好、资料向量、多模态向量、会话分段、手机认证及其他移动端依赖的表和权限。

在空 PostgreSQL 17 数据库上准备 pgvector 0.8.7 扩展文件，并由数据库管理员预先配置独立的 `medical_app_platform_runtime` 角色。该角色需保持 `NOSUPERUSER`、`NOBYPASSRLS`，由迁移角色作为 schema 所有者执行下列审核后的 SQL。迁移会启用并强制执行相关表的行级安全。

```sh
PYTHONPATH=src:. python -m alembic -c server/alembic.ini upgrade head --sql > mobile-11864.sql
psql -X -v ON_ERROR_STOP=1 -f mobile-11864.sql
```

将 `psql` 的连接参数指向待初始化的空库。迁移后应在 `medical_app_private.alembic_version` 中看到 `platform_0008_password_signup`。

原阿里云服务通过独立流程初始化和升级，恢复或升级既有数据库前须核对实际的迁移履历、结构及权限。直接在其上重放空库脚本可能触发已有结构冲突。迁移带事务和结构检查，避免采用重建方式覆盖现有会员数据。

### 配置手机 API

- `requirements-linux-lock.txt` 来自历史 Linux API 构建基线，锁定 Debian Bookworm x86-64 / CPython 3.13.15 的二进制依赖。Dockerfile 和 `compose.example.yaml` 提供独立 API 容器模板。需由操作者提供已初始化并经过验证的 PostgreSQL，配置专用运行角色、HTTPS、受信任主机与数据库证书。模板只将 API 端口绑定到本机。
- 复制 `.env.example` 到受控的本地路径，填写数据库连接、随机 32 字节十六进制 token pepper、服务允许域名。数据库连接需要 `postgresql+psycopg`、`sslmode=verify-full`、容器内 CA 路径。以只读挂载提供 TLS、DeepSeek 加密材料、开发者加密材料和 Qwen 凭据。Qwen ASR 的私有挂载需要 `config.json` 和 `api-key`。语义检索需要有效的 Qwen VL 配置。
- 准备匹配版本的阅读资源包及 `data/content-catalog/` 下的 `catalog.json`、`daily-readings.json`，供 API 启动加载。按需挂载有权使用的知识库。用户照片、生产配置、备份、证书私钥和冻结的线上发布包都由操作者独立保管。
- 容器运行后，通过操作者配置的 HTTPS 入口检查 `/health/live` 和 `/health/ready`，再在隔离环境验证登录、聊天、保存记录和旧版客户端。本目录的检查范围不包括实际恢复线上服务。

`data/nutrition-catalog/` 提供 0006 所需的公开食品数据。数据文件中保留 FDA 来源及公共领域声明，以及 GI 研究数据的 CC BY 4.0 归属和条目来源。它们不含用户记录。
