#!/usr/bin/env node
/**
 * smoke-nbs-adapter.mjs —— 不启动 DSH，验证 nbs-adapter.js 的参数拼装逻辑。
 *
 * 为什么需要它
 * ------------
 * 当前 shell 里的 node **无法创建任何子进程**（连 cmd.exe 都是 `spawn EPERM`，
 * errno -4048），所以插件的 `execute` 没法真的把 Python CLI 跑起来。这个脚本
 * 用桩函数替换 `execFile`，把插件实际要执行的 argv **原样打印出来**，从而在
 * 不起进程的前提下验证：命令构造、参数顺序、必填校验、信封解析、stderr 透传。
 *
 * 拦截原理（实测结论，别改成"先 import 再篡改"）
 * ---------------------------------------------
 * `import { execFile } from 'node:child_process'` 是**快照绑定**：ESM facade 在首次
 * 被 import 时从 CJS 的 module.exports 拷贝，之后篡改 module.exports 无效。
 * 实测：
 *     import cp from 'node:child_process'; const ns = await import('node:child_process');
 *     cp.execFile = patched;  ns.execFile === cp.execFile   // false —— 快照，拦不到
 * 正确顺序是先经 createRequire 拿到 CJS exports 并篡改，**再**动态 import 插件
 * （插件里的 'node:child_process' 此时才创建 facade，于是拿到桩函数）：
 *     const cp = require('node:child_process'); cp.execFile = stub;
 *     const mod = await import('../src/plugins/nbs-adapter.js');   // 之后才 import
 * 因此本文件顶部**不得**出现 `import ... from 'node:child_process'`。
 *
 * 用法
 * ----
 *     node tools/smoke-nbs-adapter.mjs
 * 退出码：全部用例通过 0，否则 1。
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// ① 必须在动态 import 插件之前篡改（见文件头说明）
const cp = require('node:child_process')
const realExecFile = cp.execFile

/** 每次 execFile 的调用记录。 */
const calls = []
/** 置 true 时让桩返回非 JSON，用于验证失败路径。 */
let injectGarbage = false

/** 依据子命令造一个像样的假信封，让 execute 的解析路径也被走到。 */
function stubEnvelope(args) {
  const sub = args[2] // ['-m', 'econ_core.nbs_client_cli', '<sub>', ...]
  switch (sub) {
    case 'list-provinces':
      return { ok: true, command: sub, data: [{ text: '北京市', value: '110000000000' }], row_count: 34 }
    case 'get-default-indicator':
      return { ok: true, command: sub, data: { catalogName: '居民消费价格指数', xData: ['2020年'], yData: [] }, code: 21 }
    case 'get-catalog-tree':
      return { ok: true, command: sub, data: [{ _id: 'f191523975b3423a893efe1fa80a84a3' }], row_count: 15 }
    case 'fetch-indicator':
      return {
        ok: true,
        command: sub,
        data: [{ region_code: '000000000000', period: '2020', value: 1034867.6 }],
        row_count: 10,
        raw_cache: 'D:\\universe\\econ-data-harvester\\data\\raw\\_http_cache\\c87e19df454ee1e4.bin',
      }
    default:
      return { ok: false, command: String(sub), data: null, error: '未知子命令' }
  }
}

cp.execFile = function stubExecFile(file, args, options, callback) {
  calls.push({ file, args: [...args], options })
  let stdout
  if (injectGarbage) {
    stdout = 'this is not json at all'
  } else {
    stdout = JSON.stringify(stubEnvelope(args))
  }
  const stderr = '[stub] http_client: 存档 raw -> ...\\_http_cache\\c87e19df454ee1e4.bin (361 bytes)\n'
  process.nextTick(() => callback(null, stdout, stderr))
  return { on() {}, kill() {}, unref() {} }
}

// --------------------------------------------------------------------------- //
// ② 现在才 import 插件（facade 未创建，拿到的是桩函数）
// --------------------------------------------------------------------------- //
const mod = await import(new URL('../src/plugins/nbs-adapter.js', import.meta.url))

const reg = []
const fakeCtx = {
  tools: { register: (def) => { reg.push(def); return () => {} } },
  effect: () => {},
}

console.log('='.repeat(78))
console.log('smoke: nbs-adapter 参数拼装验证（无子进程）')
console.log('='.repeat(78))
console.log(`插件 name=${mod.name}  inject=${JSON.stringify(mod.inject)}`)
console.log(`真实 execFile 已被替换: ${cp.execFile !== realExecFile}`)

mod.apply(fakeCtx)
console.log(`注册工具数: ${reg.length}\n`)

// --------------------------------------------------------------------------- //
// ③ 用例
// --------------------------------------------------------------------------- //
const GDP = {
  cid: 'f7fd25aaad184414875632cf2327da60',
  indicator_id: 'db8e5a86c08246e79b1b11251927e740',
  tree_node_id: '7dc6a2ee6c614960b7059991e0cc4d96',
  root_id: '71d41888d5a44bb2a67402ef4e60003e',
  periods: '2015YY,2016YY,2017YY,2018YY,2019YY,2020YY,2021YY,2022YY,2023YY,2024YY',
}

const cases = [
  ['nbs_list_provinces', {}],
  ['nbs_get_default_indicator', { code: 21 }],
  ['nbs_get_catalog_tree', { cid: 'db8e5a86c08246e79b1b11251927e740' }],
  ['nbs_fetch_indicator', GDP],
  ['nbs_fetch_indicator', { ...GDP, region_code: '110000000000' }], // 非默认地区
]

let failures = 0
const byName = (n) => reg.find((t) => t.name === n)

for (const [toolName, args] of cases) {
  const tool = byName(toolName)
  calls.length = 0
  console.log('─'.repeat(78))
  console.log(`CASE  ${toolName}`)
  console.log(`  args       : ${JSON.stringify(args)}`)

  const env = await tool.execute(args, {})
  const call = calls[0]

  if (!call) {
    console.log('  ✘ 未捕获到 execFile 调用')
    failures++
    continue
  }

  console.log(`  argv[0]    : ${call.file}`)
  console.log(`  argv[1..]  : ${JSON.stringify(call.args)}`)
  console.log(`  完整 argv  : ${JSON.stringify([call.file, ...call.args])}`)
  console.log(`  options    : cwd=${call.options.cwd}`)
  console.log(`               timeout=${call.options.timeout}  maxBuffer=${call.options.maxBuffer}  windowsHide=${call.options.windowsHide}`)
  console.log(`               env.PYTHONPATH=${call.options.env.PYTHONPATH}`)
  console.log(`               env.PYTHONIOENCODING=${call.options.env.PYTHONIOENCODING}`)
  console.log(`  execute -> : ok=${env.ok} command=${env.command} row_count=${env.row_count ?? '-'}`)
  console.log(`  stderr 透传: ${typeof env.stderr === 'string' && env.stderr.length > 0 ? '✔ 有' : '✘ 无'}`)

  // 约束断言
  const venvOk = call.file === 'D:\\universe\\econ-data-harvester\\.venv\\Scripts\\python.exe'
  const cwdOk = call.options.cwd === 'D:\\universe\\econ-data-harvester'
  const timeoutOk = call.options.timeout === 60000
  if (!venvOk || !cwdOk || !timeoutOk) {
    console.log(`  ✘ 约束不符: venv=${venvOk} cwd=${cwdOk} timeout=${timeoutOk}`)
    failures++
  }
  console.log()
}

// 必填校验
console.log('─'.repeat(78))
console.log('CASE  必填参数缺失（应抛错，不 spawn）')
calls.length = 0
try {
  await byName('nbs_fetch_indicator').execute({ cid: 'only-cid' }, {})
  console.log('  ✘ 未抛错')
  failures++
} catch (e) {
  console.log(`  ✔ ${e.message}`)
  console.log(`  spawn 次数: ${calls.length}（应为 0）`)
  if (calls.length !== 0) failures++
}

// 失败路径：CLI 输出非 JSON
console.log()
console.log('─'.repeat(78))
console.log('CASE  CLI 输出非 JSON（验证失败信封与 stderr 不吞）')
injectGarbage = true
calls.length = 0
const bad = await byName('nbs_list_provinces').execute({}, {})
console.log(`  ok       : ${bad.ok}`)
console.log(`  error    : ${bad.error}`)
console.log(`  stdout_head: ${JSON.stringify(bad.stdout_head)}`)
console.log(`  stderr   : ${JSON.stringify(bad.stderr)}`)
if (bad.ok !== false || !bad.stderr || !bad.stdout_head) {
  console.log('  ✘ 失败信封不完整')
  failures++
} else {
  console.log('  ✔ 失败信封完整且 stderr 未丢')
}
injectGarbage = false

console.log()
console.log('='.repeat(78))
console.log(failures === 0 ? '全部用例通过 ✔' : `失败用例数: ${failures} ✘`)
console.log('='.repeat(78))
process.exit(failures === 0 ? 0 : 1)
