# 凡思手机版

凡思健康生活服务的 Android 客户端与配套服务代码。客户端源自 `1.6.13-try1.2`（11864），包括记录助手、对话补答、多草稿和餐照记录功能。

| 目录 | 内容 |
| --- | --- |
| [client](client/) | Android、WebView 与嵌入式 Python 源码、打包资源和客户端回归 |
| [backend](backend/) | 手机配套 API、数据库迁移和记录助手后端回归 |
| [update-tools](update-tools/) | 安卓静态更新包的生成和校验工具 |

Windows 调试构建在 `client/` 下运行，使用 JDK 21、Android SDK 和 Python 3.13。完整准备步骤见 [客户端说明](client/README.md)。

```powershell
./build-android.ps1 -Python (py -3.13 -c 'import sys; print(sys.executable)')
```

后端运行、数据库和环境变量见 [后端说明](backend/README.md)。本仓库的默认配置用于本地开发或测试。部署与 11864 相同的生产服务时，需要核对现网运行基线及私有环境参数。

正式安装包由应用内更新渠道分发。生产凭据、正式签名文件和用户数据由各自的运行或发布环境管理。

