import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

const source = await readFile(new URL('../entry/src/main/ets/services/AppUpdate.ets', import.meta.url), 'utf8')
const compiled = stripTypeScriptTypes(source.replace(/^import .*$/gm, ''), {mode: 'transform'}).replace(/^export /gm, '')

function setup(checkResult, dialogResult) {
  const ctx = {}
  let dialogs = 0
  const bundle = {
    BundleFlag: { GET_BUNDLE_INFO_DEFAULT: 0 },
    getBundleInfoForSelfSync: () => ({versionName:'1.6.13-test',versionCode:35101})
  }
  const market = {
    UpdateAvailableCode: { LATER_VERSION_EXIST: 1, LATER_VERSION_NOT_EXIST: 0 },
    ShowUpdateResultCode: { SHOW_DIALOG_SUCCESS: 0, SHOW_DIALOG_FAILURE: 1 },
    async checkAppUpdate(context) {
      assert.equal(context, ctx)
      if (checkResult instanceof Error) throw checkResult
      return checkResult
    },
    async showUpdateDialog(context) {
      assert.equal(context, ctx)
      dialogs++
      if (dialogResult instanceof Error) throw dialogResult
      return dialogResult
    }
  }
  const service = Function('bundleManager','updateManager',compiled +
    '\nreturn {installedVersion,checkMarketUpdate,openMarketUpdate,updateFailure}')(bundle,market)
  return { ctx, service, dialogs: () => dialogs }
}

test('installed version is read from the running Harmony bundle', () => {
  assert.equal(setup().service.installedVersion(), '1.6.13-test（35101）')
})

test('no eligible higher version does not launch an installer', async () => {
  const {service, ctx, dialogs} = setup({updateAvailable:0})
  assert.equal(await service.checkMarketUpdate(ctx), 'no-offer')
  assert.equal(dialogs(), 0)
})

test('a phone can offer an update without optional atomic-service version fields', async () => {
  const {service, ctx, dialogs} = setup({updateAvailable:1}, 0)
  assert.equal(await service.checkMarketUpdate(ctx), 'available')
  assert.equal(dialogs(), 0)
  assert.equal(await service.openMarketUpdate(ctx), 'opened')
  assert.equal(dialogs(), 1)
})

test('invalid responses and update dialog failures cannot be mistaken for success', async () => {
  const {service, ctx} = setup({updateAvailable:77},1)
  await assert.rejects(service.checkMarketUpdate(ctx), /未知的版本状态/)
  await assert.rejects(service.openMarketUpdate(ctx), /更新窗口未能打开/)
})

test('failed checks propagate to retryable status without disclosing system messages', async () => {
  for (const [code, expected] of [[1009400001,'连接'],[1009400003,'网络'],[1009400004,'前台'],[1009400005,'隐私'],[1009400006,'稍后'],[-1,'暂时无法']]) {
    const fault = Object.assign(new Error('private system details'), {code})
    const {service,ctx,dialogs} = setup(fault, fault)
    await assert.rejects(service.checkMarketUpdate(ctx), error =>
      error === fault && service.updateFailure(error).includes(expected) && !service.updateFailure(error).includes('private'))
    assert.equal(dialogs(), 0)
    await assert.rejects(service.openMarketUpdate(ctx), error => error === fault)
  }
})
