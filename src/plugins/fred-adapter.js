/**
 * fred-adapter — FRED（St. Louis Fed）采集工具的 DSH 适配层。
 *
 * 定位：**薄壳**，与同目录 nbs-adapter.js / worldbank-adapter.js / imf-adapter.js 结构逐段对应。
 * 只做两件事：
 *   1. 把 DSH 工具参数翻译成 'python -m econ_core.fred_client_cli' 的 argv；
 *   2. 把 CLI 打到 stdout 的 JSON 信封翻译回工具返回值。
 *
 * FRED 特有注意点：fredgraph.csv 端点会把 Chrome UA 直接掐断连接（RemoteDisconnected），
 * 只有朴素 UA 可用。这件事在 Python 侧处理（fred_client.FRED_HEADERS），插件这里不要再覆盖 UA。
 *
 * 为什么不用 defineTool：同 nbs-adapter.js —— 项目根没有 node_modules，
 * 插件内 import '@deepseek-ai/dsh-tools' 会 ERR_MODULE_NOT_FOUND。
 *
 * 本文件不使用 JS 模板字符串（反引号 / 美元花括号插值），一律用单引号拼接。
 *
 * CLI 契约（由 fred_client_cli.py 保证）
 * ------------------------------------
 *   - 成功：stdout 是单行 JSON 信封 {ok:true, command, data, ...}，退出码 0
 *   - 失败：stdout 仍是 {ok:false, command, data:null, error}，退出码 2
 *   - 人类可读日志走 stderr，execute 永远把 stderr 原样带回
 */

// node:child_process 是 Node 内建模块，不经 node_modules 解析，零依赖插件可安全 import。
import { execFile } from 'node:child_process'

/** Cordis reads the exported name as this plugin's identity in the tree. */
export const name = 'fred-adapter'

/** 本行需要工具注册表才能注册两个 FRED 工具。 */
export const inject = ['tools']

// --------------------------------------------------------------------------- //
// 环境常量：全部写死绝对路径，避免命中 PATH 里的其它 python
// --------------------------------------------------------------------------- //

/** 项目根，同时作为 shell 的工作目录。 */
const PROJECT_ROOT = 'D:\\universe\\econ-data-harvester'

/** 项目 venv 的解释器（绝对路径，不用 PATH 里的 python）。 */
const PYTHON = 'D:\\universe\\econ-data-harvester\\.venv\\Scripts\\python.exe'

/** 让 -m econ_core.* 可解析：python 包根目录。 */
const PYTHONPATH = 'D:\\universe\\econ-data-harvester\\python'

/** CLI 模块名。 */
const CLI_MODULE = 'econ_core.fred_client_cli'

/** 工具执行超时（毫秒）。 */
const TIMEOUT_MS = 120_000

/** stdout 上限：单序列 CSV 只有几十 KB，给足空间。 */
const MAX_BUFFER = 32 * 1024 * 1024

// --------------------------------------------------------------------------- //
// 子进程执行
// --------------------------------------------------------------------------- //

/**
 * 以项目 venv 的 python 运行 CLI，返回原始 stdout/stderr 与错误。
 *
 * @param {string[]} cliArgs - 传给 fred_client_cli 的 argv（不含解释器与模块名）。
 * @param {AbortSignal} [signal] - 调用方取消信号，透传给子进程。
 * @returns {Promise<{error: Error|null, stdout: string, stderr: string}>}
 */
function runCli(cliArgs, signal) {
  return new Promise((resolve) => {
    execFile(
      PYTHON,
      ['-m', CLI_MODULE, ...cliArgs],
      {
        cwd: PROJECT_ROOT,
        timeout: TIMEOUT_MS,
        killSignal: 'SIGKILL',
        maxBuffer: MAX_BUFFER,
        windowsHide: true,
        signal,
        env: {
          ...process.env,
          PYTHONPATH,
          PYTHONIOENCODING: 'utf-8',
        },
      },
      (error, stdout, stderr) => {
        resolve({
          error: error ?? null,
          stdout: typeof stdout === 'string' ? stdout : String(stdout ?? ''),
          stderr: typeof stderr === 'string' ? stderr : String(stderr ?? ''),
        })
      },
    )
  })
}

/**
 * 把子进程结果规整成工具返回值信封。
 *
 * @param {{error: Error|null, stdout: string, stderr: string}} raw - runCli 的输出。
 * @param {string} toolName - 工具名，用于错误信息。
 * @returns {object} {ok, data, error?, raw_cache?, stderr, ...}
 */
function toEnvelope(raw, toolName) {
  const stderr = raw.stderr.trim()
  const stdout = raw.stdout.trim()
  let parsed = null
  if (stdout) {
    try {
      parsed = JSON.parse(stdout)
    } catch {
      parsed = null
    }
  }

  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const envelope = { ...parsed }
    if (stderr) envelope.stderr = stderr
    if (envelope.ok !== true && !envelope.error) {
      envelope.error = raw.error ? raw.error.message : toolName + ': CLI 返回 ok=false 但未给 error'
    }
    return envelope
  }

  const detail = raw.error
    ? raw.error.message + (raw.error.code !== undefined ? ' (code=' + raw.error.code + ')' : '')
    : toolName + ': CLI 未返回可解析的 JSON 信封'
  return {
    ok: false,
    command: toolName,
    data: null,
    error: detail,
    stderr,
    stdout_head: stdout.slice(0, 2000),
  }
}

/**
 * 校验必填字符串参数，缺失则抛错（错误信息列出全部缺失项）。
 *
 * @param {object} args - 模型传入的参数。
 * @param {string[]} required - 必填参数名。
 * @throws {Error} 任一必填项缺失或不是非空字符串时。
 */
function requireStrings(args, required) {
  const missing = required.filter((key) => {
    const v = args?.[key]
    return typeof v !== 'string' || v.trim().length === 0
  })
  if (missing.length > 0) {
    throw new Error('invalid arguments: missing or empty required string(s): ' + missing.join(', '))
  }
}

// --------------------------------------------------------------------------- //
// 工具定义（形状对齐 defineTool 的返回值）
// --------------------------------------------------------------------------- //

/** 两个工具共用的输出信封 schema。 */
const OUTPUT_SCHEMA = {
  type: 'object',
  required: ['ok'],
  properties: {
    ok: { type: 'boolean' },
    command: { type: 'string' },
    // data 是异构载荷：fetch-series 是 [{period, value}] 数组，list-search 恒为 []，
    // 失败信封里是 null。DSH 的 schema 子集只认单值 type，没有 json，也不支持类型数组；
    // 因此这里用 annotation-only 节点（schema 校验器把它当作不受约束的 JSON）。
    data: { description: '命令载荷：数组或对象；失败时为 null' },
    error: { type: 'string' },
    row_count: { type: 'integer' },
    series_id: { type: 'string' },
    frequency: { type: 'string' },
    raw_cache: { type: 'string' },
    parsed_cache: { type: 'string' },
    fetched_at: { type: 'string' },
    // 无数据时 CLI 会**省略**这两个字段（而不是写 null），所以声明 string 是安全的。
    period_min: { type: 'string' },
    period_max: { type: 'string' },
    query: { type: 'string' },
    note: { type: 'string' },
    hint: { type: 'string' },
    stderr: { type: 'string' },
    stdout_head: { type: 'string' },
  },
}

/**
 * 组装一个工具定义对象。
 *
 * @param {object} spec - 工具描述（name / description / parameters / buildCliArgs / render / presentCall）。
 * @returns {object} 与 defineTool 返回值同形状的定义对象。
 */
function makeTool(spec) {
  return {
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    timeoutMs: TIMEOUT_MS,
    output: {
      schema: OUTPUT_SCHEMA,
      render: spec.render,
    },
    async execute(args, exec) {
      const raw = await runCli(spec.buildCliArgs(args ?? {}), exec?.signal)
      return toEnvelope(raw, spec.name)
    },
    presentCall: spec.presentCall,
  }
}

/** 渲染用的短摘要，避免把整张表塞进卡片。 */
function shortSummary(value) {
  if (!value || value.ok !== true) {
    return '失败: ' + (value?.error ?? '未知错误')
  }
  const bits = ['command=' + value.command]
  if (typeof value.row_count === 'number') bits.push('条数=' + value.row_count)
  if (value.frequency) bits.push('频率=' + value.frequency)
  if (value.period_min) bits.push('范围=' + value.period_min + '~' + value.period_max)
  if (value.raw_cache) bits.push('raw=' + value.raw_cache)
  return bits.join(' · ')
}

/**
 * 注册两个 FRED 工具。
 *
 * @param {object} ctx - 携带 tools 注册表的 cordis 上下文。
 */
export function apply(ctx) {
  const marker = '[' + name + ']'
  console.log(marker + ' plugin applied — 注册 FRED 工具（Python CLI 转发）')

  ctx.tools.register(
    makeTool({
      name: 'fred_fetch_series',
      description:
        '采集一条 FRED（圣路易斯联储）序列，返回 [{"period": "2020-01", "value": 102.3}, ...]。' +
        '免密钥端点 fredgraph.csv；period 粒度自动识别（月度 YYYY-MM / 季度 YYYY-Qn / 年度 YYYY）。' +
        '常用中国 CPI 序列：CHNCPIALLMINMEI（全项指数，2015=100，月度，1993 起）、' +
        'CPALTT01CNM659N（同比增速 %，月度）、CPALTT01CNA659N（同比增速 %，年度）。' +
        '注意：FRED 是二次汇编方（本例源自 OECD），许可取决于上游。',
      parameters: {
        type: 'object',
        properties: {
          series_id: { type: 'string', description: 'FRED 序列 ID，如 CHNCPIALLMINMEI' },
          start: { type: 'string', description: '起始（含），如 2015 或 2015-01；缺省不限' },
          end: { type: 'string', description: '结束（含），格式同上；缺省不限' },
        },
        required: ['series_id'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['series_id'])
        const out = ['fetch-series', '--series-id', args.series_id]
        if (typeof args.start === 'string' && args.start.trim()) out.push('--start', args.start)
        if (typeof args.end === 'string' && args.end.trim()) out.push('--end', args.end)
        return out
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'FRED ' + (args.series_id ?? ''),
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'fred_list_search',
      description:
        'FRED 的搜索接口需要 api_key，本工具**恒返回空列表**（不联网），' +
        '信封里的 note / hint 说明原因与替代做法：请直接给出 series_id。' +
        '保留这个工具是为了让契约与其它源一致，而不是为了搜索。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '搜索词（仅用于日志），如 china cpi' },
        },
        required: ['query'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['query'])
        return ['list-search', '--query', args.query]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'FRED 搜索 ' + (args?.query ?? ''),
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.effect(
    () => () => console.log(marker + ' plugin disposed — FRED 工具已卸载'),
    'fred-adapter lifetime',
  )
}
