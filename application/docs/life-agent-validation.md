# 桌面生活智能体验收记录

验收日期：2026-09-10。基线：`bfa8281`；工作分支：`codex/life-agent-no-key`。
环境：macOS Apple Silicon、Python 3.12、PySide6 6.11.2；使用独立临时数据库与虚构会员/顾问。未读取正式会员健康数据。

## 已验证

| 场景 | 方法和结果 |
|---|---|
| 日常聊天进入智能体 | 普通“发送”走 `read_life_data` → `propose_action` → 回复，生成草稿；确认前业务记录不增加 |
| 记录/偏好/提醒闭环 | 离线 CLI 与真实 Qt 组件验收：饮水记录、饮食偏好、散步提醒，确认后能重新读取；覆盖重启、重复确认与账号隔离 |
| DeepSeek 活动 | 真实 API：`deepseek-v4-flash` 接收“我现在出去散步了”，产生开始散步的待确认记录，未填运动时长 |
| DeepSeek 餐食照片 | 桌面“拍下这一餐”选择公开测试照片 → 上传提示 → `deepseek-v4-flash-vision-exp` → 饮食草稿 → 人工确认保存。识别米饭、两个蛋、南瓜和绿色蔬菜，未填写重量/热量。烹饪方式和小配料仍有猜测，不能把单图结果当作准确食物清单或营养测量 |
| DeepSeek 睡眠 | 桌面晨间表单提交自报 2026-09-09 22:00 至 2026-09-10 07:00，真实 Flash 生成 9 小时待确认作息记录，明确不是设备检测 |
| 离线中文转写 | 本机合成测试音频经 16 kHz、单声道 PCM 输入原有 Vosk 模块，输出“我现在出去散步了”。Mac 使用 Vosk 0.3.44；Windows 原锁定版本未改 |
| 顾问摘要 | 虚构绑定顾问可生成待确认摘要，包含有限生活记录、会员表达和服务事项；无绑定顾问和跨账号请求被拒绝 |
| 手机/手环协议 | 单元测试覆盖自报/手环来源区分、错误参数、时区、屏幕未使用不能转换成睡眠时长 |
| 失败与取消 | 未知工具、跨账号参数、非法 JSON、重复调用、超限、取消均受控。仅 JSON 语法错误允许预算内纠正一次；失败不写入生活业务表 |

公开餐食测试图来自 [FAO Nauru school food 页面](https://www.fao.org/platforms/school-food/around-the-world/asia-and-the-pacific/nauru/en)，只用于临时测试，未纳入仓库。真实 Key 由用户授权从已打开浏览器取得，测试时仅放在进程内存，不写入代码、数据库或报告。

## 联调发现并修复的问题

1. 真实模型最终回答常是正常中文，旧代码要求 JSON 导致失败。现在由程序组装最终数据并继续走原校验器。
2. Vision Exp 曾返回非法 JSON 工具参数。补充工具字段说明，并加入一次受限语法纠正；越权参数不进入自动纠错。
3. 旧单次结构化提示词与新工具模式相冲突，睡眠场景曾直接返回草稿 JSON。现在两种模式使用不同输出契约；修复后真实睡眠调用成功。
4. Mac 无法使用 Windows DPAPI 持久保存 Key。密钥弹窗现在默认本次会话内存使用，Windows 可主动选择加密保存。

## 自动化回归

本轮相关回归 **142 passed**（包含真实 HTTP 适配器的模拟传输测试，测试本身不消耗 Key）：

```bash
PYTHONPATH=src:. QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/test_life_agent.py tests/test_life_agent_ui.py \
  tests/test_agent_tool_loop.py tests/test_multimodal_life_inputs.py \
  tests/test_deepseek_provider.py tests/test_deepseek_vision.py \
  tests/test_ai_assistant_service.py tests/test_ai_operator_config.py \
  tests/test_chat_workspace_ui.py tests/server/test_platform_ai.py \
  -o addopts='' -q
```

修改文件 Ruff 检查通过；`git diff --check` 通过；离线闭环 CLI 通过。

早期扩大到原有非 server 全套时结果为 **636 passed、58 failed、1 skipped**，不能称全项目全绿。将这 58 个失败测试放到 `git archive HEAD` 导出的未修改原版源码上，用相同 Mac 环境重新运行，仍为 **58 failed**；涉及 Windows DPAPI、加密离线镜像/同步及相关界面。该对照说明这些失败不由本轮引入，不代表已经修复它们。基线日志保存在本机 `outputs/life-agent-baseline.log`。

## 尚未验证或未交付

- 没有做真实麦克风、摄像头和系统朗读的端到端硬件测试；中文口音、噪声和老年人可用性需要实机试用。
- 单张公开照片只验证链路，未测识别准确率；不做热量、盐油或医学判断。
- 手机屏幕权限、Health Connect、手环 SDK、蓝牙和设备去重尚未实现；本轮只提供后续接入的数据适配协议。
- 没有新 Windows EXE、Android APK 或线上部署；根目录原发行包没有被替换。当前验收的是桌面源码和演示入口。
