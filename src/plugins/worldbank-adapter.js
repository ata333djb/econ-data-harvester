/**
 * worldbank-adapter — World Bank Open Data 采集工具的 DSH 适配层。
 *
 * 定位：**薄壳**，与同目录 nbs-adapter.js 结构逐段对应。这里不实现任何采集、
 * 解析或规范化逻辑，只做两件事——
 *   1. 把 DSH 工具参数翻译成 `python -m econ_core.worldbank_client_cli` 的 argv；
 *   2. 把 CLI 打到 stdout 的 JSON 信封翻译回工具返回值。
 * 所有业务逻辑都在 Python 侧（`worldbank_client` 采集 / `normalize` 规范化）。
 *
 * 为什么不用 `defineTool`
 * ----------------------
 * 与 nbs-adapter.js 完全相同的原因：本项目插件不依赖任何 npm 包（项目根没有
 * node_modules，插件内 import '@deepseek-ai/dsh-tools' 会 ERR_MODULE_NOT_FOUND）。
 * `defineTool` 只是把参数 spec 编译成 JSON Schema 的薄包装，所以这里手写同形状的
 * 定义对象，保持插件零依赖。代价：没有 ToolArgsError 的路径级校验，改为在
 * `execute` 里自行校验必填项。
 *
 * 与 nbs-adapter.js 的唯一实现差异
 * --------------------------------
 * 本文件不使用 JS 模板字符串（`...` 与 `${}`），一律用单引号拼接，
 * 以免 DSH 插件加载器在读取源码时对含反引号的字符串产生歧义。
 *
 * CLI 契约（由 worldbank_client_cli.py 保证）
 * -----------------------------------------
 *   - 成功：stdout 是单行 JSON 信封 `{ok:true, command, data, ...}`，退出码 0
 *   - 失败：stdout 仍是 `{ok:false, command, data:null, error}`，退出码 2
 *   - 人类可读日志走 stderr，不污染 stdout
 * 因此 `execute` 永远把 **stderr 原样带回**给 Agent，绝不吞掉。
 *
 * `name` 是 cordis 树里的插件标识；`inject` 声明本行必须拿到 tools 注册表。
 */

// `node:child_process` 是 Node 内建模块，不经 node_modules 解析，因此在零依赖插件里可安全 import。
import { execFile } from 'node:child_process'

/** Cordis reads the exported `name` as this plugin's identity in the tree. */
export const name = 'worldbank-adapter'

/** 本行需要工具注册表才能注册三个 World Bank 工具。 */
export const inject = ['tools']

// --------------------------------------------------------------------------- //
// 环境常量：全部写死绝对路径，避免命中 PATH 里的其它 python
// --------------------------------------------------------------------------- //

/** 项目根，同时作为 shell 的工作目录。 */
const PROJECT_ROOT = 'D:\\universe\\econ-data-harvester'

/** 项目 venv 的解释器（绝对路径，不用 PATH 里的 python）。 */
const PYTHON = 'D:\\universe\\econ-data-harvester\\.venv\\Scripts\\python.exe'

/** 让 `-m econ_core.*` 可解析：python 包根目录。 */
const PYTHONPATH = 'D:\\universe\\econ-data-harvester\\python'

/** CLI 模块名。 */
const CLI_MODULE = 'econ_core.worldbank_client_cli'

/** 工具执行超时（毫秒）。World Bank 偶发慢响应，比 nbs 放宽到 120s。 */
const TIMEOUT_MS = 120_000

/** stdout 上限：国家列表接近 300 条，给足空间。 */
const MAX_BUFFER = 32 * 1024 * 1024

/** list-indicators 的 per_page 默认值（与 Python 侧默认一致）。 */
const DEFAULT_PER_PAGE = 20

// --------------------------------------------------------------------------- //
// 子进程执行
// --------------------------------------------------------------------------- //

/**
 * 以项目 venv 的 python 运行 CLI，返回原始 stdout/stderr 与错误。
 *
 * @param {string[]} cliArgs - 传给 worldbank_client_cli 的 argv（不含解释器与模块名）。
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
 * 优先级：能解析出 CLI 信封就用它（再补 stderr）；否则构造失败信封，
 * 把 stdout 头部与 stderr 一并带回，方便 Agent 定位问题。
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

// --------------------------------------------------------------------------- //
// 参数校验（替代 defineTool 的路径级校验）
// --------------------------------------------------------------------------- //

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

/**
 * 校验可选的整数字段（未提供则跳过，由调用方回落到默认值）。
 *
 * @param {object} args - 模型传入的参数。
 * @param {string} key - 字段名。
 * @throws {Error} 提供了该字段但不是整数时。
 */
function requireOptionalInt(args, key) {
  const v = args?.[key]
  if (v === undefined || v === null) return
  if (!Number.isInteger(v)) {
    throw new Error('invalid arguments: ' + key + ' must be an integer')
  }
}

// --------------------------------------------------------------------------- //
// 工具定义（形状对齐 defineTool 的返回值）
// --------------------------------------------------------------------------- //

/** 三个工具共用的输出信封 schema。 */
const OUTPUT_SCHEMA = {
  type: 'object',
  required: ['ok'],
  properties: {
    ok: { type: 'boolean' },
    command: { type: 'string' },
    // data 是异构载荷：fetch-indicator 是长表数组，list-indicators / list-countries
    // 是指标/国家数组，失败信封里是 null（见 worldbank_client_cli._ok/_fail）。
    // DSH 的 schema 子集只认单值 type，没有 json，也不支持类型数组；
    // 因此这里用 annotation-only 节点——schema 校验器明确把它当作不受约束的 JSON 标准写法。
    data: { description: '命令载荷：数组或对象；失败时为 null（各子命令形状不同）' },
    error: { type: 'string' },
    row_count: { type: 'integer' },
    raw_cache: { type: 'string' },
    parsed_cache: { type: 'string' },
    fetched_at: { type: 'string' },
    country: { type: 'string' },
    indicator: { type: 'string' },
    date_range: { type: 'string' },
    query: { type: 'string' },
    per_page: { type: 'integer' },
    search_hits: { type: 'integer' },
    catalog_scanned: { type: 'integer' },
    region: { type: 'string' },
    stderr: { type: 'string' },
    stdout_head: { type: 'string' },
  },
}

/**
 * 组装一个工具定义对象。
 *
 * @param {object} spec - 工具描述。
 * @param {string} spec.name - 模型可见的工具名。
 * @param {string} spec.description - 模型可见的描述。
 * @param {object} spec.parameters - 参数 spec（已是 JSON Schema 形状）。
 * @param {(args: object) => string[]} spec.buildCliArgs - 参数 -> argv 的翻译。
 * @param {(args: object, value: object) => Array<object>} spec.render - 结果渲染。
 * @param {(args: object) => object} spec.presentCall - 调用卡片。
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
  if (value.raw_cache) bits.push('raw=' + value.raw_cache)
  return bits.join(' · ')
}

/**
 * 注册三个 World Bank 工具。
 *
 * @param {object} ctx - 携带 tools 注册表的 cordis 上下文。
 */
export function apply(ctx) {
  const marker = '[' + name + ']'
  console.log(marker + ' plugin applied — 注册 World Bank 工具（Python CLI 转发）')

  ctx.tools.register(
    makeTool({
      name: 'wb_fetch_indicator',
      description:
        '采集 World Bank 指标的观测值，返回规范化后的长表行（与 NBS 同形）。' +
        '每行含 region_code/region_name/indicator_id/tree_node_id/indicator_name/' +
        'period/period_type/value/unit/source/fetched_at/raw_cache/row_sha16/raw_fields。' +
        '注意 World Bank 的 unit 字段通常为空串，真实单位写在 indicator_name 的括号里' +
        '（如 "GDP (current LCU)" 表示现价本币）。',
      parameters: {
        type: 'object',
        properties: {
          country: { type: 'string', description: '国家代码，如 CHN（3 位）或 CN（2 位），也支持 CN;US' },
          indicator: { type: 'string', description: '指标代码，如 NY.GDP.MKTP.CN（GDP 现价本币）、NY.GDP.MKTP.CD（GDP 现价美元）' },
          date_range: { type: 'string', description: '年份区间，如 2015:2024；单年 2020' },
        },
        required: ['country', 'indicator', 'date_range'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['country', 'indicator', 'date_range'])
        return [
          'fetch-indicator',
          '--country', args.country,
          '--indicator', args.indicator,
          '--date-range', args.date_range,
        ]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'World Bank ' + (args.indicator ?? '') + ' / ' + (args.country ?? ''),
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'wb_list_indicators',
      description:
        '搜索 World Bank 指标。注意：服务端 GET /v2/indicator 不支持搜索' +
        '（实测 search 参数被忽略，带与不带返回的 total 与首条 id 完全相同），' +
        '因此本工具改为拉全量指标目录后在客户端对 id/name 做大小写不敏感子串过滤。' +
        '信封语义：row_count 为本次返回条数，per_page 为返回条数上限，' +
        'search_hits 为全量命中总数，catalog_scanned 为被扫描的全量指标条数。',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '搜索关键词，如 GDP' },
          per_page: { type: 'integer', description: '每页条数，默认 20' },
        },
        required: ['query'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['query'])
        requireOptionalInt(args, 'per_page')
        return [
          'list-indicators',
          '--query', args.query,
          '--per-page', String(args.per_page ?? DEFAULT_PER_PAGE),
        ]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'World Bank 指标搜索 ' + (args.query ?? ''),
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'wb_list_countries',
      description:
        '列出 World Bank 国家/地区（GET /v2/country），可按 region 过滤。' +
        'region 做大小写不敏感精确匹配（region.id / iso2code / value），如 EAS、' +
        'Z4 或 East Asia & Pacific；region.id 为 NA 的记录是聚合体（World、' +
        'Arab World 等，实测 295 条里 78 条），不是真实国家。',
      parameters: {
        type: 'object',
        properties: {
          region: { type: 'string', description: '区域过滤（region.id / iso2code / value 大小写不敏感精确匹配），如 EAS 或 East Asia & Pacific；缺省不过滤' },
        },
      },
      buildCliArgs(args) {
        const out = ['list-countries']
        if (typeof args.region === 'string' && args.region.trim().length > 0) {
          out.push('--region', args.region)
        }
        return out
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'World Bank 国家列表 ' + (args?.region ?? '(全部)'),
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.effect(
    () => () => console.log(marker + ' plugin disposed — World Bank 工具已卸载'),
    'worldbank-adapter lifetime',
  )
}


