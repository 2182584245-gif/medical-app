# Supabase 操作指南：阶段 3 已完成，下一步准备后端部署

本轮目标项目：`https://rvtjveplqnelgwnpuqtn.supabase.co`。

这个 HTTPS 地址是项目 API 入口，不是 PostgreSQL 数据库连接串。

截至 2026-09-07，已完成真实数据库登录与严格 TLS 验证、`medical_app_private` 私有 schema
的 `pilot_0001` 迁移、独立运行角色 `medical_app_runtime`，以及 18 项真实 PostgreSQL +
进程内 API 联调检查。测试账号、对话和消息均已回滚，没有上传桌面 SQLite、健康记录或 AI Key。
详情见 [阶段 3 验收记录](SUPABASE_STEP3_CHECKS.md)。

**当前这台电脑已配置好，不需要再次输入密码或重复建表。** 下面的初次配置步骤仅用于新机器
或明确需要更换凭据时。当前尚未部署 Render，也没有把桌面切换到云端数据源。

## 现在可以安全复查的内容

在项目根目录使用 PowerShell：

```powershell
$env:PYTHONPATH = $null
$env:PYTHONIOENCODING = 'utf-8'
.\.venv-server\Scripts\python.exe -m server.cloud_connection check
.\.venv-server\Scripts\python.exe -m server.cloud_admin inspect
```

两条命令仅做只读检查；后者检查目标 schema、版本标记及 Data API 角色的有效权限，不读取
健康业务行。不要为了复查而重复执行 `migrate` 或 `provision`。

需要重新做合成数据验收时，可明确运行下列命令。它会在云端事务内临时写入合成账号和消息，
完成后回滚并核查本次合成记录未留下；不是纯只读检查，也不会调用 AI：

```powershell
.\.venv-server\Scripts\python.exe -m server.cloud_smoke --confirm medical_app_private
```

成功状态为 `passed_rolled_back`。该检查是同一进程内两个客户端的顺序联调，不是公开网站、
真实跨设备网络、并发写入或完整业务同步测试。

## 初次配置或换机器时填写一次（本机已完成）

1. 在浏览器登录 Supabase，进入项目 `rvtjveplqnelgwnpuqtn`。
2. 点击项目顶部 **Connect（连接）**，选择 **Session pooler（会话连接池）**，复制 URI。
   本步骤使用端口 **5432**；不要选 Transaction pooler 的 6543。不要凭项目网址猜地区主机。
   URI 中保留 `[YOUR-PASSWORD]` 占位符即可，程序会单独询问真实密码。
3. 运行项目里的 `tools/configure_supabase.cmd`。也可以在 PyCharm 的 PowerShell 终端运行：

```powershell
Set-Location 'C:\Users\wuyuf\Documents\Codex\2026-09-02\wo-d'
$env:PYTHONPATH = $null
.\.venv-server\Scripts\python.exe -m server.cloud_connection configure
.\.venv-server\Scripts\python.exe -m server.cloud_connection check
```

4. 依次粘贴连接 URI、填写**数据库密码**，输入时不显示文字是正常保护，不是卡住。
   数据库密码不是 Supabase 网站登录密码、项目 URL、`anon` Key、`service_role` Key 或 AI Key。
5. 根证书路径可先留空，使用已安装的受信任公共 CA 集合；如果 pooler/项目使用不同证书链，
   程序会严格拒绝连接，再从项目 Dashboard 获取并核对可信根证书，填写本机 PEM/CRT 文件路径。
   不会关闭 TLS 校验或自动降级到不校验服务器身份的模式。

直接运行 CMD 会在配置保存成功后自动进行只读连接检查；单独运行 `configure` 只保存配置。
网络、密码或证书未通过时会显示受控提示，不会打印原始连接串或驱动异常。

如果此前已保存配置，需要更新时请明确运行：

```powershell
.\.venv-server\Scripts\python.exe -m server.cloud_connection configure --replace
```

然后重新运行 `check`。已有文件默认不会被覆盖。不要把密码、完整含密码 URI 或凭据文件发到聊天里。
如果检查失败，只提供不含凭据的中文错误提示即可，不要发送配置文件或完整连接串。

## 本地如何保管数据库凭据

- 配置位置：项目 `.local/supabase-connection.json`；该目录已被 Git 忽略。
- 使用 Windows **当前用户 DPAPI** 加密后才写入磁盘；没有明文回退方式。
- 连接串和数据库密码不进入源代码、桌面 EXE、便携数据备份、日志或聊天。
- 这份加密文件通常只能由本机同一个 Windows 用户解密，**不是跨设备迁移包的一部分**。
  换机器应重新配置。DPAPI 不能抵御已经获得当前 Windows 用户权限的恶意程序。
- `supabase-connection.json` 是管理配置，供只读预检及显式迁移/授权命令使用，不用于 API 日常运行。
- `supabase-runtime.json` 是独立后端运行配置，同样采用 DPAPI，包含低权限连接信息和令牌 pepper。
  `load_runtime_settings` 用于本机开发/试点验收，不是可以直接公开部署的生产配置。
- 本机使用 `.local/certs/prod-ca-2021.crt` 校验数据库证书；原下载文件保留未删除。
  证书不是密码，但整个 `.local` 目录仍不得进入发布包或 Git。
- Render 后续使用单独的受控秘密配置流程，不能使用管理员密码，也不能上传本机 DPAPI 文件代替配置。

## 只读检查实际做什么

1. 核对 URI 为当前项目的官方 Session pooler，或本项目 Direct 主机；检查用户名项目后缀、5432 端口及数据库名。
2. 拒绝额外 `hostaddr`、`service`、`options`、`passfile` 等可能覆盖连接目标的参数。
3. 使用 `sslmode=verify-full`，校验可信证书链和实际主机名；连接超时 8 秒。
4. 首条 SQL 前启用只读事务，语句时限 5 秒，只查询 PostgreSQL 版本、数据库名、当前数据库角色及私有 schema 是否存在。
5. 最后回滚，不读取健康业务行，不运行 `create_all`、建表、导入或删除。

成功结果叫 `connected_readonly`（只读连接成功）。这不代表迁移、权限、文件存储或跨设备同步已经完成。
`private_schema_exists: false` 对尚未初始化的新项目可以是正常结果，不应据此手动乱建表。

## 后续部署与迁移边界

- 新的 pilot 表放入 `medical_app_private`（应用私有数据库命名空间），不默认落到 `public`。
- Alembic（数据库结构版本管理工具）负责单独的初始化与升级，不在应用启动时悄悄建表。
- 迁移凭据与应用运行凭据分离；已有未知对象或非空数据的回退操作受到保护。
- 本地 SQLite 测试、离线 PostgreSQL SQL 生成不等于真实 Supabase 迁移验收。
- 本次初始迁移和运行角色已通过真实检查；四张业务表只覆盖账号、登录会话、对话和文字消息，
  尚未包含其他健康业务。旧 SQLite 真实数据仍保留，正式迁移必须另行确认。
- 当前源码目录不是 Git 仓库；桌面上的 `medical-app` 是独立的既有应用下载仓库。进入 Render
  Git 部署前，必须由用户确认源码使用新仓库还是放入既有仓库，不能擅自覆盖下载内容。
- 还需实现并验证共享认证限流、可信代理及公网 HTTPS，审查数据库级行隔离、日志及恢复方案。
  不得直接把 `SERVER_EXTERNAL_AUTH_LIMITS_CONFIGURED` 设为 true 绕过未完成的验收。
- 在公共 API 验收通过后，再适配 Windows 桌面登录和聊天；本轮不修改安卓或独立网站。

## 官方依据

- [Supabase PostgreSQL 连接方式](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [PostgreSQL TLS 校验说明](https://www.postgresql.org/docs/current/libpq-ssl.html)
- [Supabase Data API 安全](https://supabase.com/docs/guides/api/securing-your-api)

本轮不改 Windows v1.1.0 已交付的桌面包，不改安卓或网站。
