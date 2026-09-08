# 1.3.0 双版本组装与验收

本工具不执行 PyInstaller，也不会选择或恢复旧 EXE。等待源码冻结和本轮干净构建完成后，使用构建所用的 Python 运行。所有输出必须是新的独立路径；既有目标、输入输出互相包含或符号链接均拒绝。

## 同一份新构建，一次组装两个目录和 ZIP

在 application 根目录执行；以下是已核对的最终构建目录（根级只能有 EXE 和 `_internal`）。输出路径必须尚不存在；重做时选择新的输出根目录，不覆盖已交付文件：

```powershell
.venv\Scripts\python.exe packaging\assemble_demo_release.py --source "D:\medical-app-release-20260908\dist-final\健康生活服务平台" --demo "outputs\synthetic-demo-20260908" --clean-destination "D:\medical-app-release-20260908\release\空白版\健康生活服务平台" --destination "D:\medical-app-release-20260908\release\示例体验版\健康生活服务平台" --clean-zip "D:\medical-app-release-20260908\release\健康生活服务平台-1.3.0-空白版.zip" --zip "D:\medical-app-release-20260908\release\健康生活服务平台-1.3.0-示例体验版.zip"
```

组装前直接读取最终 EXE 的唯一内嵌 PYZ，与当前源码逐模块校验，额外要求新版偏好、预约、两种工作区、会员页、云同步、快照刷新、DPAPI 适配模块存在。缺少模块或使用旧源码立即拒绝，不运行 EXE。两个版本的 EXE 和 `_internal` 每个运行文件都与同一个输入文件哈希逐一核对。

纯净版仍由原 `assemble_release.py` / `verify_release.py` 创建并验证空库；没有改变原空库门槛。示例版仅复制九个受控资料文件：`data/app.db`、六幅商品 PNG、`demo_manifest.json`、`DEMO_ACCOUNTS.md`，不复制示例截图、日志、开发配置或其他文件。

实际固定示例契约为：5 个明确命名的 demo 账号、2 个会员资料、140 条生活记录（2会员×5类×14天）、16 个上门任务、8 个工作记录、6 个商品、4 行购物车、4 行收藏、2 个顾问有效期；其余每张表也校验精确计数。原生成器标记为 `synthetic_demo:true`，发布元数据另声明 `synthetic:true`。每个初始密码只与数据库 Argon2id 哈希进行验证，不打印密码或哈希；初始密码仅存在发行包明确标注的虚构账号说明中。

示例库必须为 schema 6，完整性与外键检查通过，没有额外表、真实联系方式、二进制附件、任意设置、API Key、数据库连接串或明文初始密码。头像和天气/AI 上下文同意均保持关闭。上述检查结合生成器来源约束，不能替代对任何不明来源数据库的人为批准；未知数据库不能当作示例源。

两个目录分别有覆盖每个文件的 `SHA256SUMS.txt`（清单自身除外）；两个 ZIP 均自动解压到临时目录重验。哈希用于发现变化，不等同于代码签名或恶意软件扫描。合法的 certifi 公开 CA 集合可保留，私钥和 DPAPI 文件不得进入包。

## 独立复验

```powershell
.venv\Scripts\python.exe packaging\verify_release.py "D:\medical-app-release-20260908\release\空白版\健康生活服务平台"
.venv\Scripts\python.exe packaging\verify_release.py "D:\medical-app-release-20260908\release\健康生活服务平台-1.3.0-空白版.zip"
.venv\Scripts\python.exe packaging\verify_demo_release.py "D:\medical-app-release-20260908\release\示例体验版\健康生活服务平台"
.venv\Scripts\python.exe packaging\verify_demo_release.py "D:\medical-app-release-20260908\release\健康生活服务平台-1.3.0-示例体验版.zip"
.venv\Scripts\python.exe packaging\verify_embedded_source.py "D:\medical-app-release-20260908\release\示例体验版\健康生活服务平台\健康生活服务平台.exe"
```

示例校验可添加 `--expected-exe-sha256 <同轮纯净版报告的EXE哈希>`，额外绑定两份结果。所有 ZIP 路径预检后才解压，拒绝越界、重复/大小写冲突、链接、Windows 设备名和数据流路径。

## 实际 EXE 隔离启动

使用 PowerShell 7 执行下列命令；省略 `-ConfirmRun` 时只校验哈希和版本，不创建目录、不启动程序。`-TestRoot` 必须为本轮 `startup` 目录下不存在的新子目录；如果已运行，请换一个新名称。工具只针对刚启动的进程观察标题、发送正常关闭请求和必要的失败清理，绝不关闭其他同名实例。

```powershell
pwsh -NoProfile -File tools\check_windows_release_startup.ps1 -ExecutablePath "D:\medical-app-release-20260908\dist-final\健康生活服务平台\健康生活服务平台.exe" -ExpectedSha256 ef7b9063c434156ca130c59d2143d5f6876240d59831b38c624e4eac0cec6c86 -PythonPath ".venv\Scripts\python.exe" -TestRoot "D:\medical-app-release-20260908\startup\final-20260908-a1" -ConfirmRun
```

2026-09-08 已对上述最终 EXE 实际运行一次并通过：主窗口为“健康生活服务平台 — 本地模式”，文件版本 1.3.0.0、产品版本 1.3.0，正常关闭退出码 0，隔离日志无错误；临时数据库 schema 6、integrity ok、users 0、外键错误 0，EXE 前后哈希不变。证据保存于 `D:\medical-app-release-20260908\startup\final-20260908-a1\startup-report.json`。程序数字签名状态为 NotSigned，与 Defender 扫描结果是不同检查。

该检查没有登录或触发 AI，也没有进行抓包，因此不声称实测网络流量为零。它不替代实际成品的三角色交互、偏好隔离和示例图片检查；先前源码级界面测试和截图也不应冒充这些操作已在最终 EXE 中执行。实际发布目录/ZIP 验证及 Defender 扫描结果由最终发布报告分别列明。
