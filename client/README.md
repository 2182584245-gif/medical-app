# 凡思 Android · try1.2（11873）

这是凡思 11873 的 Android 客户端工程。原生代码、打包后的 WebView 页面及移动端 Python 桥位于 `android/app/src/main/`，复用的 Python 业务代码位于 `src/`。

## 构建

使用 JDK 21、Python 3.13、Android SDK API 36 Extension 19 和 Android Build Tools 36.1.0。Gradle Wrapper 固定使用 8.13，项目使用 AGP 8.13.2 和 Chaquopy 17.0.0。设置 `JAVA_HOME` 和 `ANDROID_HOME`，并在首次构建时允许 Gradle、Android Maven 和 Chaquopy 访问依赖仓库。Gradle Wrapper 校验分发包的 SHA-256。

Windows PowerShell 从此目录运行：

```powershell
./build-android.ps1 -Python (py -3.13 -c 'import sys; print(sys.executable)')
```

也可从 `android/` 使用项目自带的 Wrapper。将 `HEALTH_BUILD_PYTHON` 设为 Python 3.13 解释器路径。

```sh
sh ./gradlew --no-daemon :app:assembleDebug
```

调试 APK 默认生成在 `android/app/build/outputs/apk/debug/`，使用本机调试签名。发布构建以环境变量 `HEALTH_SIGNING_STORE` 指向正式密钥库的绝对路径，以 `HEALTH_SIGNING_PASSWORD` 提供口令，再运行 `./build-android.ps1 -Variant Release -Check`。发布签名使用别名 `fawnsee-mobile`，原应用升级时继续使用原签名证书。

公开源码省略仅获内部使用授权的资料卡、文章与原文。线上阅读仍通过云端接口加载；公开源码构建可选配拥有相应许可的资料库，见 [本地资料库说明](assets/ai_library/README.md)。网页字体的 SIL Open Font License 文件见 [第三方字体许可](third-party-notices/fonts/)。

## 测试

```sh
python -m pip install -r requirements-test.txt
python -m pytest mobile/tests/test_record_task_bridge.py
npm ci
npx playwright install chromium
npm test
```

已安装 Google Chrome 的机器上，可运行 `python -m pytest mobile/tests/test_account_entry11868.py` 检查登录注册三页。浏览器测试使用 mock 原生桥和合成记录，不依赖正式账号。也可将 `FAWNSEE_CHROME` 设为本机 Chrome 路径。

`tools/verify_android_apk.py` 检查 APK 版本、签名、打包的页面与 Python 代码。联网的记录助手、照片保存和待完成草稿需要配套的云端 API。

