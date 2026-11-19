# 凡思手机版

凡思健康生活服务的两个手机客户端共用同一个后端。安卓版为 `1.6.20-try1.2`（11873），鸿蒙版为 `1.6.19`（11871），以 HarmonyOS 7.0.0 为验收基准。同一会员账号可以在两端查看已同步的生活记录和餐照。

| 目录 | 内容 |
| --- | --- |
| [client](client/) | Android、WebView 与嵌入式 Python 源码、打包资源和客户端回归 |
| [FAWNSEEHARMONY](FAWNSEEHARMONY/) | HarmonyOS ArkUI 与 ArkWeb 客户端、构建依赖和功能测试 |
| [backend](backend/) | 手机配套 API、数据库迁移和记录助手后端回归 |
| [update-tools](update-tools/) | 安卓静态更新包的生成和校验工具 |
| [必须要看的辅助参考资料](必须要看的辅助参考资料/) | 项目工作流、UI 设计及资料卡示例 |

Windows 调试构建在 `client/` 下运行，使用 JDK 21、Android SDK 和 Python 3.13。完整准备步骤见 [客户端说明](client/README.md)。

```powershell
./build-android.ps1 -Python (py -3.13 -c 'import sys; print(sys.executable)')
```

后端运行、数据库和环境变量见 [后端说明](backend/README.md)。生产部署还需要对应环境的运行参数和凭据。

首次授权、AI 对话、记录和设备限制见[本版更新说明](FAWNSEEHARMONY/验收/11871/更新说明.md)和[上一版逐项验收结果](FAWNSEEHARMONY/验收/11870/公开验收摘要.md)。

对应版本的软件包和安装条件见 [GitHub Releases](https://github.com/2182584245-gif/medical-app/releases)。安卓版也支持应用内更新。生产凭据、签名材料和用户数据由各自的运行或发布环境管理。

