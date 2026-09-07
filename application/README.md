# 健康生活服务平台（Windows 本地／云端双模式）

## v1.2.0 云端升级

新代码保留本地 SQLite 模式，增加显式选择的桌面云端模式：电脑只连接 HTTPS
FastAPI 后端；当前尝试用 Railway 赠送额度运行后端，Supabase PostgreSQL 保存业务数据。
Railway 无密钥网络探针已通过 HTTPS 和数据库端口连通性测试，测试实例随后已停止；
这不等于正式后端已上线。原 Render 路线因付款信息验证受阻，保留为备选部署说明。
云端与本地账号独立，不自动读取或上传旧 SQLite。AI Key、语音、OCR 仍留在电脑。

完整业务结构为 `medical_app_platform` 私有 schema（数据库命名空间）：24 张业务表，
另有登录会话、共享认证限流与重复请求防重共 3 张支持表。所有 27 表启用强制行级
安全策略（RLS）。旧四表试点 `medical_app_private` 保留；新版不使用试点账号。
运行凭据使用独立最小权限角色，不能随桌面包分发，不放入 Git。源码位于现有
medical-app 仓库 `application/` 子目录；根目录的旧版本下载不会被覆盖。

部署入口：`PYTHONPATH=src python -m server.platform_entrypoint`；依赖单独安装
`requirements-server.txt`，后端不安装 PySide6/OCR/语音模型。正式发布、实际云端验收、
免费服务限制和备份边界以 `server/PLATFORM_RELEASE_CHECKS.md` 的已验证状态为准。

以下为原本地版本功能和使用说明；涉及“本地保存／导出／复制目录”的段落只适用于
本地模式，不等于云端数据库已经下载到电脑或整库备份。

这是一个以 Python、PySide6 和 SQLite 为基础的 Windows 本地优先应用。v1.1.0
完成了会员、家庭生活顾问、运营人员三端的本地业务闭环，并把账户、聊天、结构化
生活数据、图片/PDF 原件和报告统一保存在可迁移数据库中。

当前版本不提供“忘记密码”功能，不提供疾病诊断、治疗方案、处方或用药建议，也
不连接真实支付、库存、物流、硬件或医疗系统。

## 1. 已实现功能

v1.1.0 仅升级 Windows 桌面端：登录/注册密码可显示与隐藏；AI 聊天左侧支持新建、
切换、重命名多个对话；DeepSeek 支持原生图片理解，以及 PDF、TXT、Markdown、CSV、
DOCX 本地文字提取后对话。附件原始内容、提取文字及所属消息一同保存在 SQLite，
关闭重开和完整数据备份后仍保留。数据库在事务中由 v4 升至 v5，保留原有数据。
当前发布包仍是本地版；云端后端基础代码与测试单独准备，不表示已连接 Supabase、
已部署 Render 或已迁移数据。本次不修改或发布 Android，也不开发独立网站。

v1.0.1 时间修复：日期时间默认使用北京时间（UTC+08:00），与电脑的系统时区无关。
提醒、生活记录、会员方案、顾问绑定、上门和复访均可直接编辑并保存，无需用户填写
时区。历史 UTC 时间按真实时刻转换展示；今日、统计与 AI 近期上下文按北京自然日
计算。出生日期和报告日期支持中文日历、键盘输入与清空。数据库仍为 v4，升级不改写
历史记录；先用旧版导出数据，再在新版登录页导入即可保留数据。

- 本机账号密码注册/登录；密码只保存 Argon2id 哈希。
- 首次本地安全配置运营账号，普通注册只能创建会员。
- 会员端：今日概览、五类生活记录、7/30 天事实统计、生活提醒、个人档案、
  会员服务、文件与健康报告、商品推荐与模拟购买、AI 助手。
- 顾问端：仅查看当前绑定会员、档案和近期记录，处理上门任务、填写服务记录、
  安排复访、推荐生活用品、生成待确认 AI 工作摘要。
- 运营端：账号/顾问/会员方案/绑定/上门服务管理、商品和模拟订单管理、顾问工作
  事实统计、结构化 AI 功能开关与上下文范围。
- 文字 AI：本地 Ollama、Ollama Cloud、DeepSeek Cloud（deepseek-v4-flash）。
- 图片 AI：支持能够处理图片的本地或云端 Ollama 模型；DeepSeek 上下文含图片时
  自动使用 deepseek-v4-flash-vision-exp（视觉实验模型），纯文字仍使用 Flash。
- DeepSeek 聊天附件：JPG/JPEG、PNG、WEBP 图片及 PDF、TXT、MD、CSV、DOCX。
  文件先本地解析，发送前提示数据将离开电脑；扫描 PDF 请转换为图片后附加。
- 多对话：左栏按用户保存独立会话，各会话有独立历史和附件上下文，发送中禁止切换。
- 语音输入：Vosk 包内离线中文模型，音频不上传。
- 文件处理：JPG、PNG、WEBP、PDF 原件保存到 SQLite BLOB；数字 PDF 读取文字层，
  图片使用包内 RapidOCR/ONNX 模型提取文字。
- AI/OCR 结果均先形成草稿；生活记录、档案事实、提醒和顾问摘要必须逐项确认。
- 商品模块仅模拟本地业务流程，不发生真实付款、扣款、发货、物流或商家通信。
- 应用内导出/导入数据备份；复制整个便携目录也可带走程序和数据。

## 2. 开发环境

- Windows 10/11 64 位。
- Python 3.12 或 3.13，正式构建使用 Python 3.13。
- PyCharm 打开项目根目录，解释器选择 .venv\Scripts\python.exe。

主要运行依赖：

- PySide6 6.11.2：桌面 UI、录音、PDF 预览。
- argon2-cffi 25.1.0：密码哈希。
- ollama 0.6.2：本地 Ollama 与 Ollama Cloud。
- httpx 0.28.1：DeepSeek 官方 HTTPS API。
- vosk 0.3.45：离线中文语音识别。
- pypdf 6.17.0：读取数字 PDF 文字层。
- rapidocr 3.9.2、onnxruntime 1.29.0：本地图片 OCR。
- pytest、pytest-qt、ruff、pyinstaller：测试、检查与打包。

PyCharm Terminal 开发命令：

    python -m pip install -r requirements-dev.txt
    python -m pip install -e .
    python -m ollama_chat_app

应用启动、登录或切换模式时不会探测本地 Ollama；只有发送消息后才连接
http://localhost:11434。

## 3. AI 与密钥

- Ollama Cloud：用户从 https://ollama.com/settings/keys 自行创建并粘贴 Key，
  应用优先使用该账户模型列表中的可用模型。
- DeepSeek：用户自行粘贴 Key，可从 Key 页面打开用量和官方文档；纯文字使用
  deepseek-v4-flash，有图片的当前或历史上下文使用 deepseek-v4-flash-vision-exp。
  视觉模型处于实验阶段，可能调整或下线；文字和图片调用均可能消耗用户额度。
  不使用 Files API 持久化上传文件；文档的提取文字、图片和本对话上下文通过请求发送。
- 云端 Key 按本机用户与提供方隔离，只保存在本次进程内存中；不会写入 SQLite、
  日志、源码、便携目录或数据备份，关闭应用后清除。
- 运营端只能配置结构化 AI 总开关、会员助手开关、顾问摘要开关和 1～90 天上下文；
  该配置入口没有 API Key 字段。
- 结构化 AI 只读取已确认档案事实、指定范围内生活记录、当前有效提醒，以及顾问
  工作摘要所需的有效绑定/任务信息。功能关闭时在请求前停止，不发送数据、不消耗额度。

## 4. 本地数据库与迁移

SQLite 结构版本为 v5。主要表：

- users（用户）、conversations（会话）、messages（消息）。
- chat_attachments（聊天附件）：message_id（所属消息）、original_name（原文件名）、
  media_type（内容类型）、content（原始二进制内容）、extracted_text（提取文字）、
  extraction_method（解析方法）、sha256（完整性摘要）、created_at（创建时间）。
- member_profiles（会员档案）、profile_facts（已确认事实）、
  life_records（生活记录）、reminders（生活提醒）。
- memberships（会员方案）、advisor_profiles（顾问档案）、
  advisor_bindings（绑定关系）、visit_tasks（上门任务）、
  visit_records（上门记录）、advisor_summaries（顾问摘要）。
- user_files（文件元数据）、user_file_contents（文件原件 BLOB）、
  medical_reports（健康报告）、report_items（报告项目）。
- ai_insights（AI 草稿）、app_settings（非密钥配置）、audit_logs（审计）。
- products（商品）、product_recommendations（推荐/兴趣）、
  orders（本地模拟订单与价格快照）。

v1、v2、v3、v4 数据库可在事务中逐级升级到 v5。v5 移除每用户只能一个会话的限制，
不更改历史账号、会话、消息或业务记录的 ID。备份导入会校验文件清单、SHA-256、
SQLite 完整性、外键和精确结构，并先保存当前数据库的恢复副本。

## 5. 便携版与数据迁移

    健康生活服务平台\
    ├── 健康生活服务平台.exe
    ├── _internal\
    │   ├── 离线语音模型
    │   └── 离线 OCR 模型与运行库
    └── data\
        └── app.db

必须先完整解压 ZIP，再双击 EXE；不能只复制 EXE。换电脑有两种方式：

1. 完全关闭应用后复制整个“健康生活服务平台”目录。
2. 在应用中导出数据 ZIP，在另一份应用登录页导入。

数据备份包含账户、聊天、档案、生活记录、提醒、图片/PDF 原件、报告、服务、
商品与模拟订单，但不含云端 API Key、本地 Ollama 程序或本地 Ollama 模型。备份
未额外加密，只应保存到可信磁盘、U 盘或私有网盘。

## 6. 本地演示顺序

1. 在空数据库中首次配置运营账号；会员在登录页自行注册。
2. 运营员创建顾问，设置会员方案并绑定顾问，安排首次上门任务。
3. 顾问查看绑定会员，完成任务、保存事实记录并安排下一次复访。
4. 会员录入五类生活事实、设置提醒、上传图片/PDF、提取文字并逐项确认报告。
5. 会员选择本地 Ollama、Ollama Cloud 或 DeepSeek 进行聊天；可使用离线语音输入。
6. AI 根据稳定数据结构生成草稿，会员/顾问逐项确认或忽略。
7. 运营员维护生活用品；顾问推荐；会员创建模拟订单；运营员标记模拟交付。
8. 运营员查看顾问工作事实统计和 AI 配置；统计不产生自动分数或排名。
9. 导出完整数据备份，在另一份空便携目录中导入并核对数据。

## 7. 权限和安全边界

- 会员只能访问自己的数据和文件。
- 顾问只能访问当前有效绑定会员；换绑后立即失去旧会员访问权。
- 运营员管理本地业务关系，但不能在 AI 配置中读取或保存用户云端 Key。
- 上传文件同时校验扩展名、文件魔数和 MIME；单文件不超过 15 MiB，每会员不超过
  100 MiB；已关联报告的原件不会被直接删除。此项是原有“文件与报告”模块限制。
- 新的聊天附件独立限制：每个 10 MiB，每次最多 4 个且合计 20 MiB，每用户合计
  100 MiB；文档文字上限 50,000 字符，拒绝不支持类型、损坏文件与超限内容。
- AI Provider 输出按不可信输入处理，必须通过严格 JSON、字段白名单、长度/日期和
  非医疗边界校验。
- 应用自身不提供自动下载入口，也不调用系统命令；发布包不使用 UPX 壳，且不申请管理员权限。

## 8. 测试与构建

    python -m ruff check src tests packaging
    python -m pytest -p no:cacheprovider tests --ignore=tests/server
    python -m pip check
    python -I -m PyInstaller --clean --noconfirm packaging\app.spec
    python packaging\verify_build.py build-v1.1.0\app\Analysis-00.toc "dist-v1.1.0\健康生活服务平台"
    python packaging\assemble_release.py "dist-v1.1.0\健康生活服务平台" "release-v1.1.0\健康生活服务平台"
    python packaging\verify_release.py "release-v1.1.0\健康生活服务平台"

真实视觉接口的可选小额自检（没有 --run 时不读取密钥、不发请求）：

    python tools\test_deepseek_vision_live.py
    python tools\test_deepseek_vision_live.py --run

第二条命令从环境变量 DEEPSEEK_API_KEY 或隐藏输入读取用户自己的密钥，仅发送一张
内存生成的几何图，不读取用户健康资料、不自动重试；会消耗少量额度。离线模拟
测试通过不等于已验证当前账户额度、网络或线上模型实际返回。

正式发布应从锁定版本与哈希的依赖清单在全新环境中构建，并在成品上验证首次启动、
PDF 预览/提取、图片 OCR、离线录音转文字、结构化 AI 草稿、数据库备份往返与
Microsoft Defender 扫描。

本个人项目没有购买 Authenticode 代码签名证书，因此 Windows 可能显示“未知发布者”；
它与 Defender 明确报告威胁不是一回事。如 Defender 明确检出威胁，不要关闭防护或
添加白名单，应停止运行并核对发布 SHA-256。

## 9. 明确不包含

- 忘记密码/在线找回密码。
- 疾病诊断、治疗方案、处方、药物提醒或用药建议。
- 真实支付、扣款、购物车、库存、物流、商家通信。
- 社区、直播、短视频、课程、积分。
- 硬件直连、复杂 IoT、医疗咨询、疾病预测或药物服务。
