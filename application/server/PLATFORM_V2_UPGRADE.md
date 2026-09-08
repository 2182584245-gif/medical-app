# 桌面体验增量升级：platform_0002

本工具不会在导入模块或启动桌面时自动连接云数据库、自动执行迁移。
云端管理员须核实具体目标、备份和维护窗口后，显式执行升级。桌面商品目前的本地体验，
不因为后端已具备购物接口就代表已开启双云同步；以界面的真实范围提示为准。

2026-09-08 实际执行记录：对已授权的 Supabase 项目完成升级前私有平台加密备份与本机隔离恢复验证后，
显式执行 `migrate` 和 `provision`。现网已核实为 `platform_0002`、32 表，专用运行角色真实连接和
生产就绪检查通过，原运行密码未轮换。升级前 27 张平台表及一张版本表的逐表行数与备份一致，
五张新增表为空；没有导入本地历史或示例账号。此记录不代表阿里云业务后端已部署或公网 HTTPS 已验收。

## 改动与保持不变的内容

旧 `platform_schema.py` 和 `platform_0001` 迁移保持冻结。新版本增加五个空表：

| 表 | 用途 | 访问限制 |
| --- | --- | --- |
| user_preferences | 称呼、字号、AI 资料分享选择等 | 仅本人读写；不存密码或 AI Key |
| member_cart | 虚拟购物车数量 | 仅会员本人读写删除 |
| member_favorites | 商品收藏 | 仅会员本人读写删除 |
| staff_account_terms | 顾问有效起止时间 | 本人或运营读；仅运营写 |
| visit_task_details | 预约类型、地址、位置、未完成状态 | 已有任务可见范围；本人新申请，运营维护 |

数据库仍使用私有 `medical_app_platform`，32 张表均开启并强制 RLS；
`anon`、`authenticated`、`service_role` 不获该 schema 的访问权。
固定服务端接口安装经过认证的事务级身份，不接受客户端 SQL。
RLS 是纵深防护，不声称能抵御运行数据库凭据泄露后的任意 SQL。

为保持桌面服务契约，新增时间仍为带数据库校验的规范 UTC 文本，JSON 仍为经格式校验的文本。
生活记录统计继续使用原有 `details_json`；不创建重复健康事实字段。
新增外键反向查询索引、数量范围与坐标成对校验。旧业务行不重写、不清空、不重置 ID。

原表仅调整三项策略：会员可提交本人待处理预约；购物车/收藏拥有者可看到相关下架商品，
以便明确拒绝结算并允许移除；运营重置密码时可撤销目标账号旧会话。
到期限制为业务表上的额外 restrictive policy，不扩大原有可见范围。

## 管理员升级顺序

1. 先核实加密管理配置对应的项目、数据库、TLS 证书和备份。不要把连接串或密码粘入命令历史。
2. 运行 `python -m server.platform_admin inspect`，只接受归属、列、权限和 RLS 完整匹配的
   `platform_0001`，或之前审核通过的空平台初始化前状态；未知对象会被拒绝接管。
3. 经独立批准后运行
   `python -m server.platform_admin migrate --confirm medical_app_platform --pilot-confirm medical_app_private`。
   当前目标为 `platform_0002`，从已核实 v1 只新增五表及上述策略，不复制用户数据。
4. 运行 `python -m server.platform_admin provision --confirm medical_app_platform`。
   该步骤再次核实同名角色归属和最小权限，只补新表权限；已有密码/pepper 不旋转。
   迁移和授权为两个步骤，二者完成前不要启动新版公开服务。
5. 重新只读检查，然后在获准的合成账号范围做 HTTPS、跨账号和到期验收。
   若迁移提交状态不明，先检查，不盲目重试。

禁止自动降级删除新表；回退必须保留已产生的数据并经人工恢复评审。
历史无有效期行的顾问维持原权限，运营应逐一设置有效期；新建顾问由服务层写入明确期限。
有效期为 `starts_at <= 当前时刻 < ends_at`；新登录、已有 token、RPC 读取及缓存写入重放都重新检查。

## 新接口边界

- `preferences.get_preferences / update_preferences`：本人设置；桌面 facade 提供 `get / update`。
- 管理新增有效期、密码重置、申请/查看/更新预约、筛选工作统计六个固定接口。
- 购物新增购物车、数量、加入购物车、收藏、收藏切换、虚拟结算六个固定接口；不产生真实支付。
- `RemoteAppointmentService` 只通过已有管理服务请求，不构造本机数据库。
- `RemoteAiAssistantService.propose_life_records` 只远程读取功能配置并核实本人身份，
  使用本机选定 AI 提取本段描述。提供方对象、API Key、历史健康事实不进入该 RPC；
  提议仅供用户编辑确认，本方法不创建记录或草稿。
- 同步服务通过既有 RPC 接线，沿用请求 UUID 幂等和角色校验；具体首导规则由同步模块定义。

## 可重复的隔离验收

普通后端测试不接网。`tests/server/test_platform_experience_pg.py` 默认跳过；
只接受显式 `MEDICAL_APP_V2_TEST_DATABASE_URL` 指向 `127.0.0.1`、
数据库名 `medical_app_v2_test` 且不存在平台 schema 的一次性 PostgreSQL。
使用独立容器、内存临时数据目录和纯合成密码；测试后销毁该精确容器。
不复用本机实验库、真实用户数据、云数据库或生产凭据。

检查项包括：v1 合成用户升级后保留、32 表强制 RLS、Data API 角色拒绝、偏好跨账号隔离、
预约本人归属、收藏幂等、下架商品结算拒绝、密码重置撤销会话、到期顾问旧身份不可见业务行。

设计依据：[Supabase 行级安全说明](https://supabase.com/docs/guides/database/postgres/row-level-security)、
[Supabase API 访问限制](https://supabase.com/docs/guides/api/securing-your-api)及
[PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/current/sql-altertable.html)。
本项目沿用已有 Alembic 私有 schema 管理链，不把这些表暴露为 Supabase Data API。

## 桌面只读镜像适配

`SyncedRemoteService(remote, sync)` 包装同一 client 的现有远程实例，不创建本地领域数据库。
仅本人 profile、生活记录、提醒、服务概况、会话/消息，以及已授权成员/顾问/预约列表使用完整镜像。
生活记录日期筛选复用服务的北京时间日界，消息 limit 取最后 N 条后保持序号正序，默认会话为最早 ID。
未知过滤条件、无完整快照和未缓存方法仍调用原远程接口，不用推测结果替代权限判断。

认证和源站/实例/账号范围不匹配时禁止使用镜像；云端登录仍必须在线完成。
成功写入或结果不确定的写入立即使镜像失去新鲜状态，保留已验证身份但禁止读取旧记录；
随后通过 Qt 主线程排队刷新，写入前在途的旧世代响应不得重新生效。
认证/权限错误立即撤销授权；不建立离线写入队列，不在本机模拟云端写入。
