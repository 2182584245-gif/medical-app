# 1.5.0：只组装新示例体验版

本轮不交付空白版、不复用旧 EXE；内部干净构建源不等于另交一份空白包。
真实成品构建、Defender 检查、签名状态和 EXE 启动验收由发布负责人另行确认。
这些步骤完成前，源码测试、合成惰性 EXE 测试不能冒充最终成品验收。

## 已生成的新示例资料

`outputs/synthetic-demo-20260909` 是从经过严格虚构契约核验的旧示例复制而来。
原 `outputs/synthetic-demo-20260908` 及其全部 payload 文件 hash 保持不变。
原全部业务行逐行保留，五个账号和独立随机密码完全沿用；没有生成或接触真实账号。

- schema 7，5 个明确命名的虚构账号，2 个会员资料，6 件生活商品及原本地 PNG。
- 156 条生活记录：保留原 140 条；两会员各新增今日旧五类 5 条和最近三天医疗 3 条。
- 15 天日期跨度、6 类平级；2026-09-09 两会员合计 12 条当日记录。
- 209 条审计：原 193 条完整保留，新增 16 条记录创建审计。
- 原 16 个上门任务、8 个工作记录、2 个订单、4 行购物车、4 行收藏、2 个顾问有效期等均不变。
- 医疗只填既有看病/用药/其他事项的虚构名称和事实说明，不含药品选择、剂量或处方方案。

数据库 SHA256：`e051bdcb2872d408f27f8dcdf0e5d062dd7f82bd43301676b00ece5e66da6c6e`。
`PAYLOAD_SHA256SUMS.txt` 覆盖十个发行 payload 文件（不含清单自身）。
`DEMO_ACCOUNTS.md` 和新增 `DEMO_ACCOUNTS.txt` 只有这五个合成初始凭据；不在日志/测试输出打印密码。
物料生成时**尚未云导入**，不意味着账号已获云端访问授权；后续以管理员真实部署/导入验收为准。

若需重做，必须另选新的不存在目录，而不是覆盖本次或旧目录：

```powershell
$env:PYTHONPATH="$PWD\src;$PWD"
.venv\Scripts\python.exe -m tools.upgrade_synthetic_demo_v7 --source "$PWD\outputs\synthetic-demo-20260908" --destination "<新的不存在的绝对目录>"
```

## 源码冻结后组装目录与 ZIP

由发布负责人把 `$freshBuild` 设为**本轮通过内嵌源码逐模块校验的 1.5.0 构建根目录**；
根级应只有 EXE 与 `_internal`。本工具自己会再次核验，旧版本或缺医疗、分段时间、共享 AI、
提醒调度等新模块立即拒绝。以下输出须全部不存在，且不得互相包含：

```powershell
.venv\Scripts\python.exe packaging\assemble_demo_release.py --source "$freshBuild" --demo "$PWD\outputs\synthetic-demo-20260909" --destination "D:\medical-app-release-20260909-v150\示例体验版\健康生活服务平台" --zip "D:\medical-app-release-20260909-v150\健康生活服务平台-1.5.0-示例体验版.zip"
```

故意不传 `--clean-destination` / `--clean-zip`。通用工具保留旧可选参数兼容，但本次不使用。
严格当前示例校验仅接受 schema 7 / 156记录 / 209审计 / 精确五账号，旧 140 条示例不能直接当新版包。
原 `verify_release` 的空数据库要求未降低，也不会把示例版当成空白版通过。

## 成品独立复验

```powershell
.venv\Scripts\python.exe packaging\verify_demo_release.py "D:\medical-app-release-20260909-v150\示例体验版\健康生活服务平台" --expected-exe-sha256 "$freshExeSha256"
.venv\Scripts\python.exe packaging\verify_demo_release.py "D:\medical-app-release-20260909-v150\健康生活服务平台-1.5.0-示例体验版.zip" --expected-exe-sha256 "$freshExeSha256"
.venv\Scripts\python.exe packaging\verify_embedded_source.py "D:\medical-app-release-20260909-v150\示例体验版\健康生活服务平台\健康生活服务平台.exe"
```

目录 hash 清单必须覆盖每个成品文件（除清单自身）；ZIP 路径、CRC、单成员 512 MiB / 总解压
2 GiB 限制保留，解压到隔离临时目录重验，不允许 DPAPI、连接串、API Key 或额外数据库混入。
哈希不替代签名或恶意软件检查；真实启动须单独隔离测试，不能从惰性 EXE 合成测试推断已启动。

## 本机测试

`tests/test_synthetic_demo_v7.py` 验证原每行/每文件/密码保留、今日六类、精确计数和拒绝覆盖；
`tests/test_demo_release.py` 用惰性源码档案测试组装、ZIP、全量 hash、旧 EXE / 非示例 / 密钥 /
账号 / 数量 / 路径篡改拒绝，绝不启动惰性档案或接触真实账号。
