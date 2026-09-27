#!/usr/bin/env node
/**
 * smoke-imf-adapter.mjs —— 不启动 DSH，验证 imf-adapter.js 的参数拼装逻辑。
 *
 * 与 tools/smoke-nbs-adapter.mjs / smoke-worldbank-adapter.mjs 同一套路：用桩函数替换
 * execFile，把插件实际要执行的 argv 原样打印出来，从而在不起子进程的前提下验证
 * 命令构造、参数顺序、必填校验、信封解析与 stderr 透传。
 *
 * 为什么必须桩替换：Node 的 execFile 默认 stdio 是管道，在本沙箱下会 spawn EPERM，
 * 插件没法真的把 Python CLI 跑起来（端到端验证由 imf_client --test 与 CLI 直跑覆盖）。
 *
 * 拦截原理：import { execFile } 是快照绑定，必须先经 createRequire 拿到 CJS exports
 * 并篡改，**再**动态 import 插件。因此本文件顶部不得出现 import ... from 'node:child_process'。
 *
 * 用法：node tools/smoke-imf-adapter.mjs
 * 退出码：全部用例通过 0，否则 1。
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// (1) 必须在动态 import 插件之前篡改（见文件头说明）
const cp = require('node:child_process')
const realExecFile = cp.execFile

/** 每次 execFile 的调用记录。 */
const calls = []
/** 置 true 时让桩返回非 JSON，用于验证失败路径。 */
let injectGarbage = false

/** 依据子命令造一个像样的假信封，让 execute 的解析路径也被走到。 */
function stubEnvelope(args) {
  const sub = args[2] // ['-m', 'econ_core.imf_client_cli', '<sub>', ...]
  switch (sub) {
    case 'fetch-indicator':
      return {
        ok: true, command: sub,
        data: [{ period: '1980', value: 303.55 }, { period: '2020', value: 15110.191 }],
        row_count: 52, indicator: 'NGDPD', country: 'CHN',
        period_min: '1980', period_max: '2031',
        fetched_at: '2026-09-27T05:18:33Z',
        raw_cache: 'D:\\universe\\econ-data-harvester\\data\\raw\\_http_cache\\83cb552d2fbbe2fb.bin',
      }
    case 'list-indicators':
      return { ok: true, command: sub, data: [{ id: 'NGDPD', label: 'GDP, current prices' }], row_count: 132 }
    case 'list-countries':
      return { ok: true, command: sub, data: [{ id: 'CHN', year_count: 52 }], row_count: 229, indicator: 'NGDPD' }
    default:
      return { ok: false, command: String(sub), data: null, error: '未知子命令' }
  }
}

cp.execFile = function stubExecFile(file, args, options, callback) {
  calls.push({ file, args: [...args], options })
  const stdout = injectGarbage ? 'this is not json at all' : JSON.stringify(stubEnvelope(args))
  const stderr = '[stub] http_client: 存档 raw -> ...\\_http_cache\\83cb552d2fbbe2fb.bin (55038 bytes)\n'
  process.nextTick(() => callback(null, stdout, stderr))
  return { on() {}, kill() {}, unref() {} }
}

// (2) 现在才 import 插件（facade 未创建，拿到的是桩函数）
const mod = await import(new URL('../src/plugins/imf-adapter.js', import.meta.url))

const reg = []
const fakeCtx = {
  tools: { register: (def) => { reg.push(def); return () => {} } },
  effect: () => {},
}

console.log('='.repeat(78))
console.log('smoke: imf-adapter 参数拼装验证（无子进程）')
console.log('='.repeat(78))
console.log('插件 name=' + mod.name + '  inject=' + JSON.stringify(mod.inject))
console.log('真实 execFile 已被替换: ' + (cp.execFile !== realExecFile))

mod.apply(fakeCtx)
console.log('注册工具数: ' + reg.length + ' -> ' + JSON.stringify(reg.map((t) => t.name)))
console.log('')

// --------------------------------------------------------------------------- //
// 用例
// --------------------------------------------------------------------------- //
const PY = 'D:\\universe\\econ-data-harvester\\.venv\\Scripts\\python.exe'
const PP = 'D:\\universe\\econ-data-harvester\\python'

const cases = [
  ['imf_fetch_indicator', { indicator: 'NGDPD', country: 'CHN' },
    ['fetch-indicator', '--indicator', 'NGDPD', '--country', 'CHN']],
  ['imf_list_indicators', {},
    ['list-indicators']],
  ['imf_list_countries', { indicator: 'NGDPD' },
    ['list-countries', '--indicator', 'NGDPD']],
]

let failures = 0
const byName = (n) => reg.find((t) => t.name === n)

for (const [toolName, args, wantArgv] of cases) {
  const tool = byName(toolName)
  calls.length = 0
  console.log('-'.repeat(78))
  console.log('CASE  ' + toolName + '  args=' + JSON.stringify(args))

  if (!tool) {
    console.log('  X 未注册该工具')
    failures++
    continue
  }

  const env = await tool.execute(args, {})
  const call = calls[0]
  if (!call) {
    console.log('  X 未捕获到 execFile 调用')
    failures++
    continue
  }

  console.log('  argv[0]    : ' + call.file)
  console.log('  完整 argv  : ' + JSON.stringify([call.file, ...call.args]))
  console.log('  cliArgs    : ' + JSON.stringify(call.args.slice(2)))
  console.log('  options    : cwd=' + call.options.cwd + '  timeout=' + call.options.timeout + '  maxBuffer=' + call.options.maxBuffer + '  windowsHide=' + call.options.windowsHide)
  console.log('               env.PYTHONPATH=' + call.options.env.PYTHONPATH + '  env.PYTHONIOENCODING=' + call.options.env.PYTHONIOENCODING)
  console.log('  execute -> : ok=' + env.ok + ' command=' + env.command + ' row_count=' + (env.row_count ?? '-') + ' fetched_at=' + (env.fetched_at ?? '-'))
  console.log('  stderr 透传: ' + (typeof env.stderr === 'string' && env.stderr.length > 0 ? '有' : '无'))

  const venvOk = call.file === PY
  const cwdOk = call.options.cwd === 'D:\\universe\\econ-data-harvester'
  const timeoutOk = call.options.timeout === 120000
  const pyPathOk = call.options.env.PYTHONPATH === PP
  const argvOk = JSON.stringify(call.args.slice(2)) === JSON.stringify(wantArgv)
  const stderrOk = typeof env.stderr === 'string' && env.stderr.length > 0
  const okAll = venvOk && cwdOk && timeoutOk && pyPathOk && argvOk && stderrOk
  if (!okAll) {
    console.log('  X 约束不符: venv=' + venvOk + ' cwd=' + cwdOk + ' timeout=' + timeoutOk + ' PYTHONPATH=' + pyPathOk + ' argv=' + argvOk + ' stderr=' + stderrOk)
    failures++
  } else {
    console.log('  OK 全部约束通过')
  }
  console.log('')
}

// 必填校验（fetch 缺 country）
console.log('-'.repeat(78))
console.log('CASE  必填参数缺失（fetch-indicator 缺 country，应抛错，不 spawn）')
calls.length = 0
try {
  await byName('imf_fetch_indicator').execute({ indicator: 'NGDPD' }, {})
  console.log('  X 未抛错')
  failures++
} catch (e) {
  console.log('  OK ' + e.message)
  console.log('  spawn 次数: ' + calls.length + '（应为 0）')
  if (calls.length !== 0) failures++
}

// 必填校验（list-countries 缺 indicator）
console.log('')
console.log('-'.repeat(78))
console.log('CASE  必填参数缺失（list-countries 缺 indicator，应抛错，不 spawn）')
calls.length = 0
try {
  await byName('imf_list_countries').execute({}, {})
  console.log('  X 未抛错')
  failures++
} catch (e) {
  console.log('  OK ' + e.message)
  console.log('  spawn 次数: ' + calls.length + '（应为 0）')
  if (calls.length !== 0) failures++
}

// 失败路径：CLI 输出非 JSON
console.log('')
console.log('-'.repeat(78))
console.log('CASE  CLI 输出非 JSON（验证失败信封与 stderr 不吞）')
injectGarbage = true
calls.length = 0
const bad = await byName('imf_fetch_indicator').execute({ indicator: 'NGDPD', country: 'CHN' }, {})
console.log('  ok         : ' + bad.ok)
console.log('  error      : ' + bad.error)
console.log('  stdout_head: ' + JSON.stringify(bad.stdout_head))
console.log('  stderr     : ' + JSON.stringify(bad.stderr))
if (bad.ok !== false || !bad.stderr || !bad.stdout_head) {
  console.log('  X 失败信封不完整')
  failures++
} else {
  console.log('  OK 失败信封完整且 stderr 未丢')
}
injectGarbage = false

console.log('')
console.log('='.repeat(78))
console.log(failures === 0 ? '全部用例通过 OK' : ('失败用例数: ' + failures + ' X'))
console.log('='.repeat(78))
process.exit(failures === 0 ? 0 : 1)
