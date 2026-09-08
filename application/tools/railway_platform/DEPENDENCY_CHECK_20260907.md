# 2026-09-07 电脑应用依赖补齐与离线检查

本记录是依赖准备结果，不是最终 EXE 已构建、已扫描或已通过云端联调的证明。

- 独立环境：`D:\medical-app-release-20260907\.venv-build\Scripts\python.exe`，Python 3.13.15。
- 官方 PyPI 与本任务 D 盘缓存，按 `requirements-lock.txt` 的版本和 SHA-256
  校验值完整重装；保留原开发环境，未修改锁文件。
- 锁文件 SHA-256：`ada3eaef1d0866c06fe87363d50d63c7e2f86a988707c45c33468009f2272be7`。
- 56 / 56 个锁定版本逐项一致，`pip check` 无依赖冲突。服务端原环境的
  `pip check` 也通过；未将桌面 Qt、OCR、语音模型安装到云端后端。
- 17 个核心模块实际导入成功，来源均为新环境；包括 pypdf 6.17.0、
  PySide6 6.11.2、PyInstaller 6.22.2、RapidOCR 3.9.2、ONNX Runtime 1.29.0、
  Vosk 0.3.45，以及 Qt 多媒体/PDF、HTTPX、Ollama、Argon2、NumPy、OpenCV、Pillow。
- Qt 离屏窗口打开、事件处理、关闭成功；UI / voice / chat attachments
  （界面 / 语音 / 聊天附件）29 项离线测试通过。
- OCR 三个 ONNX 模型与安装包 RECORD 中的 SHA-256 匹配，合成文字
  `TEST 123` 被实际离线识别。
- Vosk 中文模型 14 个必需文件齐全，4 个关键模型文件与项目固定哈希一致；
  实际模型加载与合成静音识别成功。
- 上述离线功能验证禁止网络，不使用真实麦克风或用户资料，不调用收费 AI。

剩余发布检查：最终 EXE 依赖完整性、正常桌面显示、真实设备/麦克风场景、
安全软件扫描及正式云服务联调。当前不能以离线测试代替这些验收。

Railway 当前实际账户状态仍为 Limited Trial；项目令牌创建要求账户先验证。
这不是缺少 Python 依赖。没有新增支付方式、付费资源或绕过平台安全限制。
