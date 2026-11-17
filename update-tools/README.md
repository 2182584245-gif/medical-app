# 手机 App 更新渠道工具

这里的工具按 11864 客户端沿用的静态更新协议生成和核对 `latest.json` 与 `FAWNSEE-MOBILE-<version_code>.apk`。需要 Python 3.10 或更高版本，工具只依赖 Python 标准库。正式发布需在 Linux 更新服务器运行，操作对象是已有的静态目录。

## 验证签名与 APK

用客户端的 `tools/verify_android_apk.py` 生成新 APK 的检查报告。独立使用 Android Build Tools 获取新旧 APK 的签名信息和新版包信息。

```sh
apksigner verify --verbose --print-certs candidate.apk > candidate-signature.txt
apksigner verify --verbose --print-certs previous.apk > previous-signature.txt
aapt2 dump badging candidate.apk > candidate-badging.txt
python prepare_release_audit.py \
  --apk candidate.apk --build-report apk-check.json \
  --candidate-signature candidate-signature.txt --previous-signature previous-signature.txt \
  --candidate-badging candidate-badging.txt --output release-audit.json
```

用正式发布原件作为 `previous.apk`。发布审计会核对 APK 字节、应用标识、版本、真实最低系统版本以及签名一致性。诊断签名文本、报告和 APK 保持在私有准备目录中，公开服务器目录仅存更新 APK 和清单。

## 预检和发布

先准备本次发布说明文件 `release-notes.json`，内容为 UTF-8 编码的 JSON 字符串数组。确认服务器静态路由、同一文件系统的独立私有状态目录，以及客户端编译时配置的更新 URL。下文的环境变量 `FAWNSEE_UPDATE_BASE_URL` 指向该 HTTPS 目录，包含末尾斜杠。11864 客户端对更新源有固定校验，更换源时需要同步调整客户端并发布新版。

```sh
python publish_static_release.py \
  --base-url "$FAWNSEE_UPDATE_BASE_URL" \
  --release-root /path/to/update-root --state-dir /path/to/private-state \
  --staged-apk /path/to/private-stage/candidate.apk \
  --audit-json /path/to/private-stage/release-audit.json \
  --release-notes /path/to/private-stage/release-notes.json \
  --version-code 11864 --version-name 1.6.13-try1.2
```

默认仅打印计划。审核后使用相同命令加 `--apply`。工具先放置不可覆写的 APK，再完整下载读取并核对 SHA-256 与缓存策略，最后替换更新清单并回读验证。清单回读失败时工具会恢复旧清单；保留已放置的版本 APK，便于检查或重试。服务器应为 APK 返回 `Cache-Control: public, max-age=31536000, immutable`，为清单返回 `Cache-Control: no-store`，并对两者使用 identity 编码和可信 HTTPS。

从客户端网络另做独立回读。省略 `--run` 时只记录计划；带上它才发起两次 HTTPS 请求。

```sh
python verify_public_release.py --base-url "$FAWNSEE_UPDATE_BASE_URL" \
  --audit-json release-audit.json --report public-check.json --run
```

本地回归使用 `python -B -m unittest discover -v`。测试使用合成文件和本地替代传输，不需要连接真实更新服务器。

