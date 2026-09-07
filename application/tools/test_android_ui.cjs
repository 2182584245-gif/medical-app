/* Test-only mobile browser/real Python integration; never included in the APK. */
"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");
const {chromium} = require("C:/Users/wuyuf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
const root = path.resolve(__dirname, "..");
const origin = "http://127.0.0.1:18765";
const suffix = Date.now().toString().slice(-9);
const out = path.join(root, "outputs", "android-ui", suffix);
const users = {member: `ui_member_${suffix}`, advisor: `ui_advisor_${suffix}`, operator: "ui_test_operator"};
const password = "android-ui-test-password";
const report = {kind: "headless_chrome_mobile_ui_with_real_python_api_not_android_device", viewport: "390x844", timezone: "America/New_York", startedAt: new Date().toISOString(), tests: [], pageErrors: [], consoleErrors: [], rpcErrors: [], browserRequests: [], screenshots: []};
let browser, page, rpcSequence = 100000;
async function rpc(action, params = {}) {
  const response = await fetch(`${origin}/rpc`, {method: "POST", headers: {"Content-Type": "application/json", "X-HealthLife-Test": "disposable-local-ui"}, body: JSON.stringify({id: ++rpcSequence, action, params})});
  assert.equal(response.status, 200);
  return response.json();
}
async function data(action, params = {}) {const result = await rpc(action, params); assert.equal(result.ok, true, JSON.stringify(result)); return result.result;}
async function settle() {await page.locator(".loader").waitFor({state: "hidden"});}
async function nav(name) {await page.getByRole("navigation").getByRole("button", {name, exact: true}).click(); await settle();}
async function button(name, exact = true) {await page.getByRole("button", {name, exact}).click();}
async function menu(name) {await page.getByRole("button", {name: new RegExp(name)}).click(); await settle();}
async function close() {const closeButton = page.getByRole("button", {name: "关闭", exact: true}); if (await closeButton.count()) await closeButton.click();}
async function fill(name, value) {await page.locator(`[name="${name}"]`).fill(value);}
async function save(label = "保存") {await page.getByRole("dialog").getByRole("button", {name: label, exact: true}).click(); await page.getByRole("dialog").waitFor({state: "hidden"}); await settle();}
async function screenshot(name) {const file = path.join(out, `${name}.png`); await page.screenshot({path: file, fullPage: true}); report.screenshots.push(file);}
async function auditPage(name) {
  await settle();
  const audit = await page.evaluate(() => ({text: document.body.innerText, viewport: window.innerWidth, scroll: document.documentElement.scrollWidth,
    overflow: [...document.querySelectorAll("body *")].filter(n => {const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > innerWidth + 2 || r.left < -2);}).slice(0, 12).map(n => ({tag: n.tagName, class: n.className, text: n.innerText?.slice(0, 60)}))}));
  assert.ok(!/\[object (?:HTML\w*Element|Object)\]/.test(audit.text), `${name}: object rendered as text`);
  assert.ok(audit.scroll <= audit.viewport + 2, `${name}: horizontal overflow ${JSON.stringify(audit)}`);
  assert.equal(await page.locator("main .notice.error").count(), 0, `${name}: page data error`);
}
async function test(name, callback) {
  const started = Date.now();
  try {await callback(); report.tests.push({name, ok: true, ms: Date.now() - started}); console.log(`PASS ${name}`);}
  catch (error) {report.tests.push({name, ok: false, ms: Date.now() - started, error: String(error.stack || error)}); console.log(`FAIL ${name}: ${error.message}`); await screenshot(`failure-${report.tests.length}`).catch(() => {}); await close().catch(() => {});}
}
async function logout() {await nav("我的"); await button("退出当前账号"); await page.getByRole("dialog").getByRole("button", {name: "确认", exact: true}).click(); await page.getByRole("button", {name: "登录", exact: true}).waitFor();}
async function login(username) {await fill("username", username); await fill("password", password); await button("登录"); await page.getByRole("navigation").waitFor(); await settle();}
async function workspaceMenu(name) {await nav("工作台"); await menu(name);}

(async () => {
  await fs.mkdir(out, {recursive: true});
  await rpc("auth.logout");
  browser = await chromium.launch({headless: true, executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe"});
  const context = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 1, isMobile: true, hasTouch: true, timezoneId: "America/New_York", locale: "zh-CN"});
  // A browser asks for favicon.ico, unlike the Android offline asset loader.
  await context.route("**/favicon.ico", route => route.fulfill({status: 204, body: ""}));
  page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on("pageerror", error => report.pageErrors.push(String(error)));
  page.on("console", message => {if (message.type() === "error") report.consoleErrors.push({text: message.text(), location: message.location()});});
  page.on("request", request => report.browserRequests.push(request.url()));
  page.on("response", async response => {if (response.url() === `${origin}/app.js`) report.loadedAppJsSha256 = crypto.createHash("sha256").update(await response.body()).digest("hex");});
  await page.exposeBinding("testNativeDispatch", async (_, text) => {
    const request = JSON.parse(text);
    if (request.action.startsWith("native.")) {
      if (request.action === "native.getInfo") return {id: request.id, ok: true, result: {platform: "浏览器集成测试，不是安卓设备", version: "1.0.2-test", timezone: "北京时间 UTC+8"}};
      return {id: request.id, ok: false, error: "浏览器测试不模拟安卓系统文件选择、麦克风或外部网站。"};
    }
    const response = await fetch(`${origin}/rpc`, {method: "POST", headers: {"Content-Type": "application/json", "X-HealthLife-Test": "disposable-local-ui"}, body: text});
    const result = await response.json();
    if (!result.ok) report.rpcErrors.push({action: request.action, error: result.error});
    return result;
  });
  await page.addInitScript(() => {window.AndroidBridge = {postMessage: text => {window.testNativeDispatch(text).then(result => window.onNativeResult(result));}};});
  await page.goto(origin); await page.getByRole("button", {name: "登录", exact: true}).waitFor();
  await test("01 真实注册、Argon2账号、今日页面", async () => {
    await screenshot("01-login"); await button("没有账号？注册会员");
    await fill("username", users.member); await fill("password", password); await fill("repeat", password); await button("注册并登录");
    await page.getByRole("heading", {name: "今天，从一件小事开始"}).waitFor(); await auditPage("今日");
    assert.equal((await data("auth.state")).user.username, users.member); await screenshot("02-today");
  });
  await test("02 五类记录通过实际表单创建并跨日北京存取", async () => {
    const categories = [{name: "饮食", inputs: {calories_kcal: "450"}}, {name: "饮水", inputs: {amount_ml: "200"}}, {name: "活动", inputs: {activity_type: "散步", duration_minutes: "25", steps: "1500"}}, {name: "睡眠", inputs: {duration_hours: "7.5", quality: "4"}}, {name: "环境", inputs: {temperature_c: "25.5", humidity_percent: "50", air_quality: "通风良好"}}];
    for (const category of categories) {
      await nav("今日"); await page.locator(".quick-grid").getByRole("button", {name: new RegExp(`${category.name}$`)}).click();
      await fill("occurred_at", "2026-09-05T00:20"); await fill("content", `真实UI测试${category.name}`);
      for (const [key, value] of Object.entries(category.inputs)) await fill(key, value);
      await save("保存事实");
    }
    const records = await data("health.list_life_records"); assert.equal(records.length, 5);
    records.forEach(record => {assert.equal(record.occurred_at, "2026-09-05T00:20:00+08:00"); assert.equal(record.local_date, "2026-09-05");});
    await nav("记录"); await auditPage("五类记录列表"); await screenshot("03-five-records");
  });
  await test("03 提醒新增、编辑、暂停恢复，无需额外时区", async () => {
    await nav("记录"); await button("生活提醒"); await settle(); await button("＋ 新增提醒");
    await fill("title", "测试饮水提醒"); await page.locator('[name="reminder_type"]').selectOption("water"); await fill("scheduled_at", "2026-09-06T08:30"); await save();
    await button("修改时间 / 内容"); assert.equal(await page.locator('[name="scheduled_at"]').inputValue(), "2026-09-06T08:30");
    await fill("scheduled_at", "2026-09-06T09:45"); await fill("title", "已修改饮水提醒"); await screenshot("04-beijing-reminder-editor"); await save();
    await page.getByText("2026-09-06 09:45", {exact: false}).waitFor(); await button("暂停"); await button("恢复");
    const reminders = await data("health.list_reminders"); assert.equal(reminders[0].scheduled_at, "2026-09-06T09:45:00+08:00"); assert.equal(reminders[0].status, "active"); await auditPage("提醒");
  });
  await test("04 档案表单生日、数值与偏好保存", async () => {
    await nav("我的"); await menu("个人档案"); await fill("display_name", `测试会员${suffix}`); await fill("birth_date", "1990-05-20");
    await page.locator('[name="gender"]').selectOption("female"); await fill("height_cm", "165.5"); await fill("living_situation", "独居，定期与家人联系"); await fill("health_goals", "记录生活事实"); await button("保存档案");
    await page.getByRole("status").filter({hasText: "档案已保存"}).waitFor(); const profile = await data("health.get_profile"); assert.equal(profile.birth_date, "1990-05-20"); assert.equal(profile.height_cm, 165.5); await auditPage("档案"); await screenshot("05-profile");
  });
  await test("05 统计周期、日期筛选与全部会员子页", async () => {
    await nav("记录"); await button("事实统计"); await settle(); await fill("as_of", "2026-09-05"); await page.locator('[name="days"]').selectOption("30"); await button("更新统计"); await page.getByRole("heading", {name: "共 5 条记录"}).waitFor(); await auditPage("事实统计"); await screenshot("06-statistics");
    await nav("我的"); await menu("附件与报告"); await auditPage("附件"); await button("查看报告"); await auditPage("报告");
    await nav("服务"); await auditPage("会员服务"); await nav("AI 助手"); await auditPage("AI助手"); await screenshot("07-ai-no-key");
    await nav("我的"); await menu("检查数据库"); await page.getByRole("dialog").waitFor(); await auditPage("数据库检查"); await close();
  });
  await test("06 非法输入和无密钥错误为可读提示，未联网AI", async () => {
    await nav("今日"); await page.locator(".quick-grid").getByRole("button", {name: /饮水$/}).click();
    await fill("content", "非法饮水量测试"); await fill("amount_ml", "-3"); await page.getByRole("button", {name: "保存事实", exact: true}).click();
    assert.equal(await page.locator('[name="amount_ml"]').evaluate(element => element.validity.valid), false); await close();
    await nav("AI 助手"); await page.getByLabel("对话内容", {exact: true}).fill("测试缺少密钥"); await button("发送"); await page.getByRole("status").filter({hasText: "API Key"}).waitFor();
    assert.equal((await data("chat.list")).length, 0); await screenshot("08-missing-key-error");
  });
  await test("07 登录退出与初始化运营账号真实表单", async () => {
    await logout(); const state = await data("auth.state");
    if (!state.has_operator) {await button("首次使用 · 初始化运营账号"); await fill("username", users.operator); await fill("password", password); await fill("repeat", password); await button("创建运营账号");}
    else await login(users.operator);
    await page.getByRole("heading", {name: "运营工作台", exact: true}).waitFor(); await auditPage("运营工作台"); await screenshot("09-operator");
  });
  await test("08 创建顾问、维护资料与运营子页", async () => {
    await workspaceMenu("顾问与员工账号"); await button("＋ 新增顾问"); await fill("username", users.advisor); await fill("password", password); await fill("display_name", `测试顾问${suffix}`); await fill("organization", "本地测试机构"); await fill("specialty", "生活记录整理"); await save();
    await auditPage("顾问名单"); const advisors = await data("management.list_advisors"); assert.ok(advisors.some(a => a.username === users.advisor));
    await workspaceMenu("工作事实统计"); await auditPage("顾问工作统计"); await workspaceMenu("结构化 AI 设置"); await fill("context_days", "21"); await button("保存配置"); await page.getByRole("status").filter({hasText: "AI 配置已保存"}).waitFor(); assert.equal((await data("ai.get_ai_config")).context_days, 21);
  });
  await test("09 顾问绑定、会员方案与安排任务表单", async () => {
    await nav("会员"); await fill("search", users.member); await button("绑定顾问");
    const advisor = (await data("management.list_advisors")).find(a => a.username === users.advisor); await page.locator('[name="advisor_user_id"]').selectOption(String(advisor.id)); await fill("started_at", "2026-09-01T09:00"); await save();
    await fill("search", users.member); await button("会员方案"); await fill("plan_code", "本地测试方案"); await fill("starts_at", "2026-09-01T00:00"); await fill("ends_at", "2027-09-01T00:00"); await fill("benefit_notes", "日常记录整理服务"); await save();
    await fill("search", users.member); await button("安排服务"); await fill("title", `测试回访${suffix}`); await fill("scheduled_at", "2026-09-05T10:00"); await save();
    const target = (await data("management.list_members")).find(m => m.username === users.member); assert.equal(target.advisor_user_id, advisor.id); assert.equal(target.plan_code, "本地测试方案"); await screenshot("10-member-management");
  });
  await test("10 商品创建、价格与上下架表单", async () => {
    await workspaceMenu("商品目录管理"); await button("＋ 新建商品"); await fill("sku", `UI-${suffix}`); await fill("name", `测试水杯${suffix}`); await fill("category", "日常生活"); await fill("price_yuan", "12.50"); await page.locator('[name="is_active"]').check(); await save();
    const product = (await data("commerce.list_products", {include_inactive: true})).find(p => p.sku === `UI-${suffix}`); assert.equal(product.price_cents, 1250); assert.equal(product.is_active, true); await auditPage("运营商品目录");
    await workspaceMenu("模拟订单"); await auditPage("运营模拟订单");
  });
  await test("11 顾问登录、开始服务、完成回访和下次提醒", async () => {
    await logout(); await login(users.advisor); await page.getByRole("heading", {name: "顾问工作台", exact: true}).waitFor(); await auditPage("顾问工作台");
    await nav("服务任务"); const item = page.locator("article").filter({has: page.getByRole("heading", {name: `测试回访${suffix}`, exact: true})}); await item.getByRole("button", {name: "开始服务", exact: true}).click(); await item.getByRole("button", {name: "完成与回访", exact: true}).click();
    await fill("summary", "已核对会员近期生活记录"); await fill("visited_at", "2026-09-05T10:30"); await fill("duration_minutes", "25"); await fill("next_visit_at", "2026-09-10T10:00"); await save();
    await page.locator(".tabs").getByRole("button", {name: "回访历史", exact: true}).click(); await settle(); await page.getByText("已核对会员近期生活记录", {exact: true}).waitFor(); await auditPage("回访历史"); await screenshot("11-advisor-visits");
  });
  await test("12 顾问商品推荐和成员档案边界", async () => {
    await nav("会员"); await fill("search", users.member); await button("查看档案"); await page.getByRole("dialog").waitFor(); await auditPage("顾问会员档案"); await close();
    await button("推荐商品"); const product = (await data("commerce.list_products")).find(p => p.sku === `UI-${suffix}`); await page.locator('[name="product_id"]').selectOption(String(product.id)); await fill("reason", "会员主动提出关注日常饮水用具"); await save();
    await nav("AI 助手"); await auditPage("顾问AI与授权");
  });
  await test("13 会员查看推荐、真实模拟购买与订单", async () => {
    await logout(); await login(users.member); await nav("服务"); await auditPage("会员服务与回访");
    const productButton = page.getByRole("button", {name: /生活商品|商品目录|查看商品/}).first();
    if (await productButton.count()) await productButton.click();
    const products = page.locator("article").filter({has: page.getByRole("heading", {name: `测试水杯${suffix}`, exact: true})});
    await products.first().getByRole("button", {name: "模拟购买", exact: true}).click(); await fill("quantity", "2"); await save("确认生成模拟订单");
    const orders = await data("commerce.list_orders"); assert.ok(orders.some(o => o.quantity === 2));
    await button("查看模拟订单"); await auditPage("会员模拟订单"); await screenshot("12-member-orders");
  });
  await test("14 运营订单送达、编辑价格与上下架", async () => {
    await logout(); await login(users.operator); await workspaceMenu("模拟订单");
    const order = page.locator("article").filter({has: page.getByRole("heading", {name: `测试水杯${suffix}`, exact: true})});
    await order.getByRole("button", {name: "标记模拟送达", exact: true}).click(); await settle();
    const product = (await data("commerce.list_products", {include_inactive: true})).find(p => p.sku === `UI-${suffix}`);
    const orders = await data("commerce.list_orders"); assert.ok(orders.some(o => o.product_id === product.id && o.status === "delivered"));
    await workspaceMenu("商品目录管理");
    const productCard = page.locator("article").filter({has: page.getByRole("heading", {name: `测试水杯${suffix}`, exact: true})});
    await productCard.getByRole("button", {name: "下架", exact: true}).click(); await productCard.getByRole("button", {name: "上架", exact: true}).waitFor();
    await productCard.getByRole("button", {name: "编辑", exact: true}).click(); await fill("price_yuan", "13.20"); await save();
    await productCard.getByRole("button", {name: "上架", exact: true}).click();
    const changed = (await data("commerce.list_products", {include_inactive: true})).find(p => p.id === product.id); assert.equal(changed.price_cents, 1320); assert.equal(changed.is_active, true);
    await screenshot("13-operator-products");
  });
  await test("15 AI设置密钥按服务隔离且只保存内存、不联机", async () => {
    await nav("我的"); await menu("AI 连接设置"); await page.locator('[name="provider"]').selectOption("deepseek");
    await fill("api_key", "sk-public-ui-fixture"); await save("保存本次连接");
    let provider = await data("provider.status"); assert.equal(provider.provider, "deepseek"); assert.equal(provider.model, "deepseek-v4-flash"); assert.equal(provider.has_api_key, true);
    await menu("AI 连接设置"); await fill("api_key", "sk-unsaved-public-ui-fixture"); await page.locator('[name="provider"]').selectOption("ollama_cloud");
    assert.equal(await page.locator('[name="api_key"]').inputValue(), "", "切换服务应清空未提交的前一服务密钥"); await save("保存本次连接");
    assert.equal((await data("provider.status")).has_api_key, false);
    await menu("AI 连接设置"); await page.locator('[name="provider"]').selectOption("local");
    assert.equal(await page.locator('[name="api_key"]').isVisible(), false);
    await fill("local_url", "http://192.168.1.2:11434"); await save("保存本次连接"); assert.equal((await data("provider.status")).provider, "local");
    await logout(); await login(users.operator); assert.equal((await data("provider.status")).has_api_key, false);
  });
  await test("16 三角色页面无脚本错误、对象占位和外部请求", async () => {
    assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.consoleErrors, []);
    assert.ok(report.browserRequests.every(url => url.startsWith(origin)), "页面尝试加载外部资源");
    const unexpected = report.rpcErrors.filter(error => !(error.action === "chat.send" && error.error.includes("API Key")));
    assert.deepEqual(unexpected, [], "出现不应有的后端参数或权限错误");
    const currentHash = crypto.createHash("sha256").update(await fs.readFile(path.join(root, "android/app/src/main/assets/www/app.js"))).digest("hex");
    assert.equal(report.loadedAppJsSha256, currentHash, "测试期间UI源码变化，需重跑验证最终版本");
  });
})().catch(error => {report.fatal = String(error.stack || error); console.error(report.fatal); process.exitCode = 1;}).finally(async () => {
  report.finishedAt = new Date().toISOString(); report.passed = report.tests.filter(test => test.ok).length; report.failed = report.tests.filter(test => !test.ok).length;
  if (browser) await browser.close(); await fs.mkdir(out, {recursive: true}); await fs.writeFile(path.join(out, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({output: path.join(out, "report.json"), passed: report.passed, failed: report.failed, fatal: report.fatal || null})); if (report.failed) process.exitCode = 1;
});
