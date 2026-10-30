import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'

// 加载真实状态模块和状态库，仅隔离后端接口、浏览器资源及持久化存储。
async function createLogoutHarness(root) {
  const require = createRequire(new URL('package.json', root))
  let token = 'saved-token'
  let timezone = 'Asia/Tokyo'
  let resolveLogout, rejectLogout
  let logoutCalls = 0
  const remoteLogout = new Promise((resolve, reject) => {
    resolveLogout = resolve
    rejectLogout = reject
  })
  const lock = () => ({ unlockScreen() {} })
  const mocks = {
    '@/api/login': {
      login: async () => ({ token: 'new-token' }),
      getInfo: async () => ({}),
      logout: () => {
        logoutCalls++
        return remoteLogout
      },
    },
    '@/utils/auth': {
      getToken: () => token,
      setToken: (value) => (token = value),
      removeToken: () => (token = ''),
    },
    '@/utils/time': {
      setBusinessTimezone() {},
      setUserTimezone: (value) => (timezone = value),
    },
    '@/utils/validate': { isHttp: () => false, isEmpty: () => true },
    '@/plugins/cache': { default: { session: { set() {} } } },
    '@/router': { default: { push() {} } },
    '@/assets/images/profile.jpg': { default: '/default-avatar.jpg' },
    '@/store/modules/lock': { default: lock },
    'element-plus': { ElMessageBox: {} },
  }
  const pinia = require('pinia')
  pinia.setActivePinia(pinia.createPinia())
  const context = createContext({ console, defineStore: pinia.defineStore })
  const module = new SourceTextModule(
    await readFile(new URL('src/store/modules/user.js', root), 'utf8'),
    { context }
  )
  await module.link((specifier) => {
    const namespace = mocks[specifier] || require(specifier)
    return new SyntheticModule(
      Object.keys(namespace),
      function () {
        for (const [name, value] of Object.entries(namespace)) this.setExport(name, value)
      },
      { context }
    )
  })
  await module.evaluate()
  const store = module.namespace.default()
  const getState = () => store
  const logout = () => store.logOut()
  Object.assign(getState(), {
    token,
    roles: ['operator'],
    permissions: ['system:user:list'],
    timeZone: timezone,
  })
  return {
    getState,
    logout,
    resolveLogout,
    rejectLogout,
    getToken: () => token,
    getTimezone: () => timezone,
    getLogoutCalls: () => logoutCalls,
  }
}

export function runLogoutTests(root, framework) {
  if (framework !== 'vue3') throw new Error('Logout tests only support vue3')
  for (const outcome of ['success', 'offline', 'timeout', 'unauthorized']) {
    test(`${framework}: logout clears local session after ${outcome}`, async () => {
      const app = await createLogoutHarness(root)
      let navigated = false
      const completed = app.logout().then(() => {
        navigated = true
      })
      assert.equal(app.getToken(), 'saved-token')
      if (outcome === 'success') app.resolveLogout({ code: 200 })
      else app.rejectLogout(new Error(outcome))
      await completed

      assert.equal(app.getLogoutCalls(), 1)
      assert.equal(navigated, true)
      assert.equal(app.getToken(), '')
      assert.equal(app.getState().token, '')
      assert.equal(app.getState().roles.length, 0)
      assert.equal(app.getState().permissions.length, 0)
      assert.equal(app.getState().timeZone, 'auto')
      assert.equal(app.getTimezone(), 'auto')
    })
  }
}
