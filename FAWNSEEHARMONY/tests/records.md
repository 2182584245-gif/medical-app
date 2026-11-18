# 生活记录回归测试

在鸿蒙工程根目录运行。准备一个专用测试会员，用同一账户登录待测鸿蒙安装包。接口测试需 Node.js 24 或更高版本，设备测试需 Python 3 和 DevEco Studio 附带的 hdc。

## 七类记录及照片

以下是 PowerShell 示例。将地址换为测试后端地址。

```powershell
$env:FAWNSEE_QA_URL = 'http://127.0.0.1:18764'
$qaMember = Get-Credential -Message '输入用于本次验收的测试会员'
$env:FAWNSEE_QA_USERNAME = $qaMember.UserName
$env:FAWNSEE_QA_PASSWORD = $qaMember.GetNetworkCredential().Password
node tests/records-integration.mjs
```

脚本加载鸿蒙页面使用的 `RecordsData.ets`。七类记录分别验证创建、重复提交、详情、修改、陈旧版本冲突、删除和刷新。饮食、环境和医疗记录另外核对编辑前后照片字节。其余检查覆盖照片单独记录、必填和数值范围、断网暂存及账号隔离。全部通过时输出 `31 checks passed.`。云端操作使用真实 HTTP 接口。本机草稿检查使用 Node 内置 SQLite 代替设备数据库驱动，因此该项验证 SQL 和数据恢复逻辑。测试结束后脚本清理本轮的记录和上传文件。

## 断网查看及授权边界

在鸿蒙工程根目录运行 `node tests/records-cache.mjs`，需要 Node.js 24 或更高版本。脚本加载鸿蒙工程的缓存与账号清理代码，用本地临时 SQLite 文件模拟设备存储，用可控制的网络请求模拟断网、恢复和权限失效。覆盖按日期查看已同步列表、详情与历史、超出游标窗口的原图、账号与服务器隔离、过期授权、服务端错误、登出清理、云端删除和并发旧响应。通过时输出 `12 offline checks passed.`。脚本检查鸿蒙数据库配置的加密开关，实际设备的数据加密由鸿蒙系统提供。

离线读缓存取决于当前登录账号的有效服务端授权。列表保留上次完整同步的数据，最近 160 条已打开的详情保留修改历史，最近 24 张已下载的原图可重开。联网刷新会清理已移除的记录和照片。离线提交会将新增或修改的图文保存在本机，用户恢复联网后可核对草稿并再次保存。

## 模拟器或真机点击验收

启动待测设备，打开鸿蒙应用，在已登录状态停在任一显示五项底栏的页面。该安装包需要能访问上述后端。通过 `hdc list targets` 获取设备编号。hdc 已加入 PATH 时，只需设置设备编号。

```powershell
$env:HDC_TARGET = '<hdc list targets 显示的设备编号>'
python tests/records-device.py
```

若 hdc 未加入 PATH，将 `HDC_EXECUTABLE` 设为 DevEco Studio 中 hdc 的路径。脚本先临时创建一条带随机标记的饮水记录，在设备上查到该标记后才开始填写。它逐一核对七类表单，并在页面上执行 **新增 500 毫升 → 编辑为 200 毫升 → 确认删除**。每一步从后端核对同一条记录及修改原因。可传 `--artifacts <目录>` 保存本次测试会员的截图。

要复测 AI 草稿的图片，先在同一测试账号准备一个独立对话，留下一张带原照片、可确认且尚未保存的饮食草稿。在设备上登录该账号后运行下列命令。脚本核对原文件摘要，检查草稿里的图片，再点击确认、打开已保存的饮食记录和全屏照片。该可选流程保留确认后的饮食记录以供安卓端对照。

```powershell
python tests/records-device.py --photo-conversation <会话数字编号> --photo-sha256 <原始上传文件的64位SHA256>
```

这项点击测试从已有草稿开始。模型如何从图文生成草稿，需要另行发起对话验证。

