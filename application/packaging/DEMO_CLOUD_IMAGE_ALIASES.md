# 已审核的六张示例商品图片：云路径别名

这是发行组装的窄扩展，不是动态云商品图片下载、公开图片网关或跨端文件同步功能。它不修改 EXE、业务源码、原示例数据库、原六张图片或既有发行目录。

云端完整迁移会将商品图片引用改为内容地址，例如 `transfer-assets/<命名空间>/<图片 SHA256>.png`。目前桌面图片读取仍按应用目录解析相对路径。此可选步骤在**新的示例发行目录**补放这六张已确认图片的字节相同副本，以匹配本次实际云迁移的引用。未来新商品、换图或另一命名空间不因此自动得到支持。

## 输入契约

管理员从本次**实际已审阅**迁移计划的 `external_assets` 中提取六项，保存为位于来源包和待发行包之外的独立 JSON 文件。不要传完整迁移计划、数据库、加密包、密钥或凭据。

JSON 顶层必须是恰好六项的列表；每项仅有四个字段：

- `source_path`：来源相对路径，精确覆盖 `assets/products/demo-1.png` 至 `demo-6.png`，不重复。
- `sha256`：小写 SHA256，必须等于本次已硬锁的原图内容指纹。
- `bytes`：原图精确字节数，不接受布尔值、近似大小或其他图片。
- `destination`：只允许 `transfer-assets/<单个32位小写十六进制命名空间>/<对应64位小写SHA256>.png`。

映射顺序可以不同，组装时会按来源路径规范化。来源与去重后目标均逐文件核对固定 SHA256 与大小。第 3、4 张原图内容相同，因此**六项来源映射对应五个去重 PNG 文件**；不得为凑“六文件”引入另一幅图。

只锁定 `outputs/synthetic-demo-20260909` 的本次图片合同；后续图片版本需要另行审核并更新代码，而不是利用参数放行任意图片。绝对路径、反斜杠、URL、遍历、额外字段、疑似秘密字段、重复 JSON 字段、多命名空间、额外文件、额外空目录、符号链接、junction、重解析点及硬链接均拒绝。

## 组装与验证

在源码根目录，用本轮全新且已核验的构建源。下面尖括号必须由负责人填入，**目标和 ZIP 均必须不存在**；不要指定已经组好的最终包。

```powershell
$env:PYTHONPATH="$PWD\src;$PWD"
& .venv\Scripts\python.exe packaging\assemble_demo_release.py --source "<已验证本轮fresh构建目录>" --demo "outputs\synthetic-demo-20260909" --destination "<全新示例发行目录>" --zip "<全新示例ZIP路径>" --cloud-assets-plan "<包外独立审阅的六项JSON>"
& .venv\Scripts\python.exe packaging\verify_demo_release.py "<全新示例发行目录>" --expected-exe-sha256 "<本轮EXE完整SHA256>" --cloud-assets-plan "<同一个独立审阅的六项JSON>"
& .venv\Scripts\python.exe packaging\verify_demo_release.py "<全新示例ZIP路径>" --expected-exe-sha256 "<本轮EXE完整SHA256>" --cloud-assets-plan "<同一个独立审阅的六项JSON>"
```

组装额外产生且只允许以下内容：

```text
示例发行目录
├── cloud-assets-plan.json：规范化的六项非敏感映射
└── transfer-assets
    └── 已审阅的一个命名空间
        └── 五个固定原图 SHA256 命名的 PNG
```

这些文件均进入 `SHA256SUMS.txt`。`portable.json` 另记录映射文件名、规范化映射 SHA256、命名空间、六项映射／五文件数量，以及“仅内置合成别名”的范围。验证时对比**包外独立审阅文件**、包内映射、元数据、实际目录精确成员和每张图字节；不能将包内 `cloud-assets-plan.json` 当作外部审阅证据自证。

不传 `--cloud-assets-plan` 时，原示例发行根目录精确白名单保持原样，含别名目录的包会被拒绝。原 `verify_release.py` 的纯净包／空数据库门槛完全未放宽。即使改图或添加文件后重写总 SHA 清单，固定六图合同及独立计划比较仍会拒绝。

## 使用说明中的必要边界

只保证本次已内置的六张合成示例图能按已审核路径读取；不是任意云商品图下载功能。云端后续新增或更换图片不会自动出现在旧发行包里。别名不证明示例已经云导入、不证明公网图片服务上线，也不影响本地示例仍引用原 `assets/products/...` 路径。

本轮补丁只经过隔离的惰性 EXE 测试夹具、合成数据库与临时发行目录验证；正式 staging、实际最终 ZIP 和公开云账号图片展示，由负责人拿到实际云映射并明确组装后另行验收。
