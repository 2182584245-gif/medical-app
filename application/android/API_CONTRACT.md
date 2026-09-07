# Android JSON API 契约

界面发出 `{id, action, params:{}}`；Python `dispatch` 已完整返回 `{id,ok:true,result}` 或 `{id,ok:false,error:"中文"}`，Java 不再重复包装。登录状态和密钥只保存在 Python 进程内。所有服务当前操作者 ID 从会话注入，前端禁止传入 `user_id` / `actor_user_id` / `operator_user_id`。后台逐次检查账号仍启用。返回数据的日期时间均为北京时间 +08:00；日期字符串保持 YYYY-MM-DD。前端日期时间输入无需写时区，Python 按北京时间解释。

## 登录与状态

- `auth.state {}` → `{user:null|{id,username,role_code,...},has_operator,beijing_now,can_backup,can_import}`。
- `auth.register {username,password}` / `auth.bootstrap_operator {username,password}` → 新用户对象（注册后自动登录）。只能初始化一次运营员。
- `auth.login {username,password}` → 用户对象。
- `auth.logout {}` → null，清除会话与全部 API Key。
- `auth.create_staff {username,password,role_code}` → 用户，仅运营员。
- `system.check {}` → 本地 SQLite integrity_check / foreign_key_check、schema_version、table_count，无云网络请求。

## 服务方法映射

以下动作的 params 与桌面同名公开方法签名一致，但省略第一个当前操作者/用户参数。目标 `member_user_id`、`advisor_user_id`、`target_user_id` 仍保留。未知字段拒绝；返回类型完全沿用服务结果（列表、对象、整数 ID，或 null）。

- `health.get_dashboard`、`health.list_life_records`、`health.add_life_record`、`health.get_record_statistics`、`health.delete_life_record`、`health.get_profile`、`health.save_profile`、`health.list_reminders`、`health.add_reminder`、`health.update_reminder`、`health.pause_reminder`、`health.resume_reminder`、`health.toggle_reminder`、`health.delete_reminder`、`health.get_service_summary`。仅会员。
- `management.get_dashboard`、`management.create_advisor`、`management.list_members`、`management.get_member_overview`、`management.list_advisors`、`management.get_advisor_work_statistics`、`management.save_advisor_profile`、`management.save_membership`、`management.bind_advisor`、`management.create_visit_task`、`management.start_visit_task`、`management.complete_visit_task`、`management.list_visit_tasks`、`management.list_visit_records`、`management.set_account_enabled`。顾问/运营员，具体权限仍由原服务检查。
- `commerce.list_products`、`commerce.create_product`、`commerce.update_product`、`commerce.set_product_active`、`commerce.delete_product`、`commerce.recommend_product`、`commerce.list_recommendations`、`commerce.record_product_view`、`commerce.mark_interested`、`commerce.mark_product_interested`、`commerce.simulate_purchase`、`commerce.reorder`、`commerce.list_orders`、`commerce.mark_order_delivered`。模拟下单，无真实支付；商品 image_path 不允许传入本地路径。
- `files.list_files`、`files.delete_file`、`files.extract_pdf_text`、`files.extract_text`、`files.create_report_draft`、`files.add_report_item_draft`、`files.confirm_report_item`、`files.confirm_report`、`files.archive_report`、`files.delete_report`、`files.list_reports`、`files.get_report`、`files.storage_usage`。仅会员；图片 extract_text 要先通过 native OCR，PDF文字层可直接读取。
- `ai.get_ai_config`、`ai.update_ai_config`、`ai.build_member_context`、`ai.list_drafts`、`ai.get_draft`、`ai.confirm_proposal`、`ai.dismiss_proposal`。同原服务参数与权限。

## AI 连接、聊天与草稿

- `provider.status {}` → `{provider,model,local_url,has_api_key,supports_images,notice}`。初始 ollama_cloud，model 空表示首次手动请求时获取账号模型列表并选第一个。
- `provider.configure {provider:'ollama_cloud'|'deepseek'|'local',api_key?:string,model?:string,local_url?:string}` → status。不联网、不保存明文key、不同云服务key独立；空 api_key 表示清除该服务key，省略则保留。DeepSeek 模型固定 deepseek-v4-flash。local_url只允许回环或私网IP，不接受公共域名，不自动检测。手机 localhost 指手机自身，电脑需填同网段地址。
- `provider.models {}` → `{models:[string],selected:string,notice:string}`，仅用户手动执行联网；DeepSeek直接返回固定模型，不额外消耗额度。
- `chat.list {limit?:200}` → Message数组（最多500条）。`chat.send {content}` → 完成的assistant Message。用户发言/失败状态持久化。云请求发送已完成上下文（最多20条），禁止网页给自定义外网地址。发送失败中文说明不包含密钥或原始HTTP错误体。
- `ai.create_member_draft {prompt,member_user_id?:int,file_ids?:[int],context_days?:int}` → 完整draft对象。图片仅发送本人已上传图片，DeepSeek不支持图；将个人已确认档案及近期记录作为云端上下文前，UI应明确提示用户并由用户主动点击生成。
- `ai.create_advisor_summary_draft {member_user_id,context_days?:int}` → draft，仅当前绑定顾问。

## Java 可信内部方法（禁止 WebView 直接指定文件路径）

以下函数不接受JS action；Java在系统文件选择器回调后用可信临时路径调用。均返回 `{ok,result}` / `{ok:false,error}`，Java添加对应请求id再转发。

- `initialize(data_dir)` → auth.state式结果（带ok/result）。
- `import_attachment(path,filename,ocr_text=None)` → 文件元数据；可选原生识别文字只存内存待核对；不自动建报告。
- `attachment_export(file_id,path)` → 文件元数据，文件内容写到原生受控临时文件，供 SAF 导出/预览。
- `attachment_text(file_id,ocr_text)` → 与files.extract_text同形识别结果，待确认且不写入档案。
- `backup_export(path)` → `{path,file_count,size_bytes,created_at,bundle_id,...}`；全库备份含全部账号和附件，应由用户明确确认。
- `backup_import(path)` → `{...,safety_backup_path,user_count,message_count,state}`；严格验证后自动生成旧数据安全备份、覆盖导入、退出当前登录。空库允许登录前导入；有运营账号时仅运营员可全库迁移；无运营账号且仅一个账号时该账号登录后可迁移，防止普通会员取走其他账号数据。

原生 native.* action（附件选择、OCR、SAF、语音、官方外链）由 Android Activity 独立白名单处理；不得把任意Java对象、路径或异常堆栈传给网页。
