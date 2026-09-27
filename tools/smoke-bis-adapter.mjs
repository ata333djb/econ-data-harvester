#!/usr/bin/env node
/**
 * smoke-bis-adapter.mjs —— 不启动 DSH，验证 bis-adapter.js 的参数拼装逻辑。
 *
 * 与 tools/smoke-{nbs,worldbank,imf,fred}-adapter.mjs 同一套路：用桩函数替换 execFile，
 * 把插件实际要执行的 argv 原样打印出来，从而在不起子进程的前提下验证命令构造、
 * 参数顺序、必填校验、信封解析与 stderr 透传。
 *
 * 为什么必须桩替换：Node 的 execFile 默认 stdio 是管道，在本沙箱下会 spawn EPERM，
 * 插件没法真的把 Python CLI 跑起来（端到端验证由 bis_client --test 与 CLI 直跑覆盖）。
 *
 * 拦截原理：import { execFile } 是快照绑定，必须先经 createRequire 拿到 CJS exports
 * 并篡改，**再**动态 import 插件。因此本文件顶部不得出现 import ... from 'node:child_process'。
 *
 * BIS 特有覆盖点（与 fred 那份的差异）：
 *   - fetch-cpi 的三个参数**全部可选**，所以既验证「全缺省 -> argv 只有子命令」，
 *     也验证「全给 -> 三个 flag 按 unit/freq/country 顺序拼上」；
 *   - fetch-series 的 key 要**原样透传**（含点号），不在插件侧做形状校验。
 *
 * 用法：node tools/smoke-bis-adapter.mjs
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
  const sub = args[2] // ['-m', 'econ_core.bis_client_cli', '<sub>', ...]
  switch (sub) {
    case 'fetch-cpi':
      return {
        ok: true, command: sub,
        data: [{ period: '1996-01', value: 9.0 }, { period: '2026-08', value: 0.8 }],
        row_count: 368, dataset: 'WS_LONG_CPI', key: 'M.CN.771', frequency: 'monthly',
        unit: '771', unit_meaning: '同比变化（%）', country: 'CN',
        period_min: '1996-01', period_max: '2026-08',
        fetched_at: '2026-09-27T12:14:49Z',
        raw_cache: 'D:\\universe\\econ-data-harvester\\data\\raw\\_http_cache\\fb559f0d3c32de9e.bin',
      }
    case 'fetch-series':
      return {
        ok: true, command: sub,
        data: [{ period: '1978', value: 18.664853 }, { period: '2025', value: 132.804382 }],
        row_count: 48, dataset: 'WS_LONG_CPI', key: 'A.CN.628', frequency: 'annual',
        period_min: '1978', period_max: '2025',
        fetched_at: '2026-09-27T12:14:52Z',
        raw_cache: 'D:\\universe\\econ-data-harvester\\data\\raw\\_http_cache\\93a052c553d92eff.bin',
      }
    default:
      return { ok: false, command: String(sub), data: null, error: '未知子命令' }
  }
}

cp.execFile = function stubExecFile(file, args, options, callback) {
  calls.push({ file, args: [...args], options })
  const stdout = injectGarbage ? 'this is not json at all' : JSON.stringify(stubEnvelope(args))
  const stderr = '[stub] [gunzip] BIS 回了小写 content-encoding: gzip，http_client 未解压，本模块就地解压\n'
  process.nextTick(() => callback(null, stdout, stderr))
  return { on() {}, kill() {}, unref() {} }
}

// (2) 现在才 import 插件（facade 未创建，拿到的是桩函数）
const mod = await import(new URL('../src/plugins/bis-adapter.js', import.meta.url))

const reg = []
const fakeCtx = {
  tools: { register: (def) => { reg.push(def); return () => {} } },
  effect: () => {},
}

console.log('='.repeat(78))
console.log('smoke: bis-adapter 参数拼装验证（无子进程）')
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
  // fetch-cpi 三个参数全缺省 -> argv 只有子命令（插件不替调用方补默认值）
  ['bis_fetch_cpi', {},
    ['fetch-cpi']],
  ['bis_fetch_cpi', { unit: '628' },
    ['fetch-cpi', '--unit', '628']],
  ['bis_fetch_cpi', { unit: '771', freq: 'A', country: 'CN' },
    ['fetch-cpi', '--unit', '771', '--freq', 'A', '--country', 'CN']],
  ['bis_fetch_series', { dataset: 'WS_LONG_CPI', key: 'M.CN.771' },
    ['fetch-series', '--dataset', 'WS_LONG_CPI', '--key', 'M.CN.771']],
  ['bis_fetch_series', { dataset: 'WS_LONG_CPI', key: 'A.CN.628' },
    ['fetch-series', '--dataset', 'WS_LONG_CPI', '--key', 'A.CN.628']],
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
  console.log('  execute -> : ok=' + env.ok + ' command=' + env.command + ' row_count=' + (env.row_count ?? '-') + ' key=' + (env.key ?? '-') + ' frequency=' + (env.frequency ?? '-'))
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

// 必填校验（fetch-series 缺 key）
console.log('-'.repeat(78))
console.log('CASE  必填参数缺失（fetch-series 缺 key，应抛错，不 spawn）')
calls.length = 0
try {
  await byName('bis_fetch_series').execute({ dataset: 'WS_LONG_CPI' }, {})
  console.log('  X 未抛错')
  failures++
} catch (e) {
  console.log('  OK ' + e.message)
  console.log('  spawn 次数: ' + calls.length + '（应为 0）')
  if (calls.length !== 0) failures++
}

// 必填校验（fetch-series 缺 dataset）
console.log('')
console.log('-'.repeat(78))
console.log('CASE  必填参数缺失（fetch-series 缺 dataset，应抛错，不 spawn）')
calls.length = 0
try {
  await byName('bis_fetch_series').execute({ key: 'M.CN.771' }, {})
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
const bad = await byName('bis_fetch_cpi').execute({ unit: '771' }, {})
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
