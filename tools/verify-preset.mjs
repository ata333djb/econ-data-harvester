/**
 * Verification harness for the econ-harvester preset wiring.
 *
 * Checks the wiring WITHOUT booting a DSH server: the plugin's ESM load, a real
 * cordis activation and teardown, the YAML shapes the loader's health check
 * demands, and the preset's relative-path resolution rule.
 *
 * Run: node tools/verify-preset.mjs
 */
import { pathToFileURL } from 'node:url'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'

const APP_NS = 'D:/harness/DeepSeek Harness/resources/app/node_modules/@deepseek-ai'
const YAML_LIB = 'D:/harness/DeepSeek Harness/resources/app/node_modules/yaml/dist/index.js'
const PROJECT = 'D:/universe/econ-data-harvester'
const PRESET_DIR = `${PROJECT}/.dsh/.agent-presets/econ-harvester`
const PRESET_ID = 'econ-harvester'

const url = (p) => pathToFileURL(p).href
let failures = 0
const check = (label, ok, detail = '') => {
  if (!ok) failures += 1
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`)
}
/** Run `body` while capturing everything written to console.log. */
const capture = async (body) => {
  const seen = []
  const original = console.log
  console.log = (...args) => {
    seen.push(args.join(' '))
    original(...args)
  }
  try {
    await body()
  } finally {
    console.log = original
  }
  return seen
}

console.log('=== 1. the plugin module loads as ESM ===')
const hello = await import(url(`${PROJECT}/src/plugins/hello.js`))
console.log(`      exports: ${Object.keys(hello).sort().join(', ')}`)
check('exports name', hello.name === 'econ-hello', String(hello.name))
check('exports inject array', Array.isArray(hello.inject))
check('exports apply as a function', typeof hello.apply === 'function')

console.log('\n=== 2. real cordis activates the plugin ===')
const { Context } = await import(url(`${APP_NS}/cordis/lib/index.js`))
const ctx = new Context()
const applied = await capture(async () => {
  ctx.plugin(hello)
  await new Promise((resolve) => setTimeout(resolve, 250))
})
check(
  'apply() ran and printed the marker',
  applied.some((line) => line.includes('[econ-hello] plugin applied')),
  applied.join(' | ') || 'no output',
)

// `Context` has no stop(); teardown is `ctx.fiber.dispose()` (see Fiber).
const disposed = await capture(async () => {
  ctx.fiber.dispose()
  await new Promise((resolve) => setTimeout(resolve, 250))
})
check(
  'teardown printed the dispose marker',
  disposed.some((line) => line.includes('[econ-hello] plugin disposed')),
  disposed.join(' | ') || 'no output',
)

console.log('\n=== 3. preset id is a usable directory name ===')
check('id matches [a-z0-9][a-z0-9-]*', /^[a-z0-9][a-z0-9-]*$/.test(PRESET_ID), PRESET_ID)

console.log('\n=== 4. agent.cordis.yml is a list of named plugin rows ===')
const yaml = await import(url(YAML_LIB))
const composition = yaml.parse(readFileSync(`${PRESET_DIR}/agent.cordis.yml`, 'utf8'))
// The roster's health check: parseable AND a list of rows each naming a plugin.
check('parses to a YAML array', Array.isArray(composition), typeof composition)
check('every row has an id', composition.every((row) => typeof row?.id === 'string'))
check('every row names a plugin', composition.every((row) => typeof row?.name === 'string'))
console.log(`      rows: ${composition.map((row) => `${row.id} -> ${row.name}`).join(', ')}`)

console.log('\n=== 5. each row path resolves from the PRESET directory ===')
for (const row of composition) {
  if (!row.name.startsWith('.')) {
    console.log(`      ${row.name} is a package row — resolves from the host, skipping`)
    continue
  }
  const resolved = path.resolve(PRESET_DIR, row.name)
  check(`'${row.name}' resolves to an existing file`, existsSync(resolved), resolved)
}

console.log('\n=== 6. the patch overlay registers this preset root ===')
const patch = yaml.parse(readFileSync(`${PROJECT}/.dsh/econ-harvester.patch.yml`, 'utf8'))
check('parses to a YAML array', Array.isArray(patch), typeof patch)
const row = patch.find((entry) => entry?.id === 'agent-presets')
check('targets the agent-presets row by id', row !== undefined)
check(
  'restates default (a patch replaces the whole config)',
  typeof row?.config?.default === 'string',
  String(row?.config?.default),
)
check(
  'default names an existing preset id',
  row?.config?.default === PRESET_ID,
  `${row?.config?.default} vs dir ${PRESET_ID}`,
)
const root = row?.config?.roots?.[0]
check('declares one preset root', root !== undefined)
// Compare normalized paths: the YAML literal is backslash-form, dirname is not.
const expectedRoot = path.resolve(PRESET_DIR, '..')
check(
  'root path is the preset parent directory',
  root !== undefined && path.resolve(root.path) === expectedRoot,
  `${String(root?.path)} vs ${expectedRoot}`,
)
check('root path exists on disk', root !== undefined && existsSync(root.path), String(root?.path))
check('root carries a trust level', root?.trust === 'user', String(root?.trust))

console.log(`\n=== ${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`} ===`)
process.exit(failures === 0 ? 0 : 1)
