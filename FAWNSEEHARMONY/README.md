# 凡思鸿蒙手机版

版本为 1.6.15-try1.2（11867），开发与验收以 HarmonyOS 7.0.0（SDK 26.0.0）为基准，最低兼容 HarmonyOS 6.1.1（API 24）。华为开发者中心登记的包名为 `com.fawnsee.health.mobile`。今日、记录、助手、社区和我的均从原生 ArkUI 页面进入。[安卓客户端](../client/)和鸿蒙客户端共用同一个阿里云服务。同一会员登录后，可查看已同步的记录和原图。

已安装 11865 早期开发包的测试设备会把 11866 及以后的版本识别为独立应用。旧包里的离线草稿留在旧包内，如需保留到账号，请先在旧包中联网确认保存。

## 构建和安装

1. 使用 DevEco Studio 26.0.0 Release 打开本目录，并安装项目指定的 SDK。运行 `ohpm install` 或使用 IDE 的项目同步功能。测试用的两个 HAR 包在 `libs/test/` 中，版本由锁文件固定。
2. 在兼容的手机模拟器上运行 `entry`。使用真机调试时，在 DevEco 的 Signing Configs 为自己的已登记设备配置开发签名。
3. 使用 DevEco 附带的 Hvigor 在本目录运行 `hvigorw assembleApp -p product=default -p buildMode=release`。默认生成未签名的 APP 和 HAP。APP 位于 `build/outputs/default/`，HAP 位于 `entry/build/default/outputs/default/`。
4. 面向普通客户分发时，通过 DevEco 的发布流程完成华为发布签名，然后选择适合的邀请测试或应用市场渠道。[华为发布说明](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-publish-app/)

## 验证

原生页面在 `entry/src/main/ets/pages/`，设备和云服务接口在 `entry/src/main/ets/services/`。使用 Node.js 24 运行离线缓存、提醒、更新检测、局部加载失败和系统分享回归。

```sh
node --test tests/records-cache.mjs tests/reminders.mjs tests/reminder-calendar.mjs tests/app-update.mjs tests/mine-partial-load.mjs tests/assistant-load.mjs tests/text-share.mjs tests/community-detail.mjs
```

本版本的验收和软件包校验值见 [11867 验收](验收/11867.md)。七类记录、账号隔离与设备操作的复测方法见[记录测试说明](tests/records.md)。此前的测试见 [11866 构建验证](验收/11866.md)和 [11865 功能验收](验收/11865.md)。
