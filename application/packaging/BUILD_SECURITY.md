# Windows 安全构建约束

本说明用于构建“健康生活服务平台”1.2.0 的 Windows `onedir` 版本。
这是构建与验收要求，不是本次成品已构建、已扫描或已通过公网验收的证明；必须以对应产物的实际报告为准。

## 构建前

- 必须使用全新、只为本项目创建的虚拟环境。
- 必须从锁定版本和哈希的依赖清单安装依赖。
- 构建进程的 `PATH` 不应包含 Codex、Poppler、其他 Python 环境或开发工具自带的 DLL 目录。
- 不关闭杀毒软件，不添加排除项，不恢复旧版已隔离的 EXE。
- 云端入口与服务是否已经部署是两回事。仅在真实服务建立并验证后填写公开 HTTPS 地址；没有可用服务时保持默认地址为空，不用虚构地址替代。

## 固定的打包边界

- 保持 `onedir`；不改为 `onefile`。
- 保持 `upx=False`，不压缩或加壳。
- 保持 `uac_admin=False`、`uac_uiaccess=False`，以普通用户权限 `asInvoker` 运行。
- 不添加自定义 runtime hook。
- 不收集 `keyring`；Ollama Cloud API Key 只允许保存在当前应用会话的内存中。
- DeepSeek API Key 和云端登录令牌同样只存在会话内存；`.local`、管理员/运行数据库配置、数据库密码、平台 token pepper 和私有证书配置不得进入发行包。
- `app.spec` 会按二进制的源路径排除 `.cache/codex-runtimes` 中的所有文件。

## 构建后必须验证

- 验证 `Analysis-00.toc` 时应按字段检查：运行时 Hook、实际收集的 Python 模块、二进制和数据中不得包含 `keyring`、`multiprocessing`、`setuptools` 或 `pkg_resources`；`excludes` 配置字段中出现这些名称是正常的。任何二进制或数据的来源路径均不得包含 `.cache\\codex-runtimes`、`.codex` 或其他宿主工具缓存。
- 成品中不应包含来自其他开发工具运行时的 OpenSSL、Poppler、ICU 或 MSVC DLL。
- EXE 的文件属性应显示产品名、说明和 `1.2.0` 版本。
- EXE 的清单必须是 `requestedExecutionLevel="asInvoker"`、`uiAccess="false"`。
- `verify_build.py` 必须确认构建目录包含五个云端模块：`cloud_config`、`services.cloud_client`、`services.cloud_rpc_codec`、`services.remote_services`、`workers.cloud_bridge`。
- 使用构建所用的 Python 运行 `python packaging\verify_embedded_source.py "<最终发布目录>\健康生活服务平台.exe"`，直接读取最终 EXE 中唯一的 PYZ，确认上述云端模块和主入口/登录窗口模块实际存在，且全部应用字节码与当前已测试源码一致。报告记录该 EXE 的 SHA-256；这是代码归档检查，不会启动 EXE。
- 工具仍支持独立 `.pyz` 作为开发检查输入，但这种报告不能替代最终 EXE 检查。缺模块、多个/无内嵌 PYZ、源码不一致或校验期间成品变化都必须失败。
- 在新建的测试目录中运行单元测试、应用启动测试和 Microsoft Defender 自定义扫描。
- 记录 EXE、完整便携目录及最终 ZIP 的 SHA-256。
- 对组装后的目录和 ZIP 分别运行 `verify_release.py`；说明必须准确区分本地整库便携迁移与云端账号同步，不得宣称未部署的服务已经可用。

版本资源只提供可读的文件身份信息，不等同于 Authenticode 数字签名；未购买或配置代码签名证书时，Windows 仍可能显示“未知发布者”。
