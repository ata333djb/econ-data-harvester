/**
 * imf-adapter — IMF DataMapper (WEO) 采集工具的 DSH 适配层。
 *
 * 定位：**薄壳**，与同目录 nbs-adapter.js / worldbank-adapter.js 结构逐段对应。
 * 只做两件事：
 *   1. 把 DSH 工具参数翻译成 `python -m econ_core.imf_client_cli` 的 argv；
 *   2. 把 CLI 打到 stdout 的 JSON 信封翻译回工具返回值。
 *
 * IMF 特有注意点：Akamai 会 403 掉 Chrome UA，只有朴素 UA 能拿到数据。
 * 这件事在 Python 侧处理（imf_client.IMF_HEADERS），插件这里**不要**再覆盖 UA。
 *
 * 为什么不用 `defineTool`：同 nbs-adapter.js —— 项目根没有 node_modules，
 * 插件内 import '@deepseek-ai/dsh-tools' 会 ERR_MODULE_NOT_FOUND。
 *
 * 本文件不使用 JS 模板字符串（反引号 / 美元花括号插值），一律用单引号拼接。
 *
 * CLI 契约（由 imf_client_cli.py 保证）
 * ------------------------------------
 *   - 成功：stdout 是单行 JSON 信封 {ok:true, command, data, ...}，退出码 0
 *   - 失败：stdout 仍是 {ok:false, command, data:null, error}，退出码 2
 *   - 人类可读日志走 stderr，execute 永远把 stderr 原样带回
 */

// node:child_process 是 Node 内建模块，不经 node_modules 解析，零依赖插件可安全 import。
import { execFile } from 'node:child_process'

/** Cordis reads the exported name as this plugin's identity in the tree. */
export const name = 'imf-adapter'

/** 本行需要工具注册表才能注册三个 IMF 工具。 */
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
const CLI_MODULE = 'econ_core.imf_client_cli'

/** 工具执行超时（毫秒）。 */
const TIMEOUT_MS = 120_000

/** stdout 上限：单指标响应约 150KB 解码后，给足空间。 */
const MAX_BUFFER = 32 * 1024 * 1024

// --------------------------------------------------------------------------- //
// 子进程执行
// --------------------------------------------------------------------------- //

/**
 * 以项目 venv 的 python 运行 CLI，返回原始 stdout/stderr 与错误。
 *
 * @param {string[]} cliArgs - 传给 imf_client_cli 的 argv（不含解释器与模块名）。
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

/** 三个工具共用的输出信封 schema。 */
const OUTPUT_SCHEMA = {
  type: 'object',
  required: ['ok'],
  properties: {
    ok: { type: 'boolean' },
    command: { type: 'string' },
    // data 是异构载荷：fetch-indicator 是 [{period, value}] 数组，
    // list-indicators / list-countries 是目录数组，失败信封里是 null。
    // DSH 的 schema 子集只认单值 type，没有 json，也不支持类型数组；
    // 因此这里用 annotation-only 节点（schema 校验器把它当作不受约束的 JSON）。
    data: { description: '命令载荷：数组或对象；失败时为 null（各子命令形状不同）' },
    error: { type: 'string' },
    row_count: { type: 'integer' },
    raw_cache: { type: 'string' },
    parsed_cache: { type: 'string' },
    fetched_at: { type: 'string' },
    indicator: { type: 'string' },
    country: { type: 'string' },
    // 无数据时 CLI 会**省略**这两个字段（而不是写 null），所以声明 string 是安全的。
    period_min: { type: 'string' },
    period_max: { type: 'string' },
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
  if (value.period_min) bits.push('范围=' + value.period_min + '~' + value.period_max)
  if (value.raw_cache) bits.push('raw=' + value.raw_cache)
  return bits.join(' · ')
}

/**
 * 注册三个 IMF 工具。
 *
 * @param {object} ctx - 携带 tools 注册表的 cordis 上下文。
 */
export function apply(ctx) {
  const marker = '[' + name + ']'
  console.log(marker + ' plugin applied — 注册 IMF 工具（Python CLI 转发）')

  ctx.tools.register(
    makeTool({
      name: 'imf_fetch_indicator',
      description:
        '采集 IMF WEO 指标的年度序列，返回 [{"period": "1980", "value": 7.8}, ...]。' +
        '注意：返回值**含 IMF 预测值**（实测 NGDPD/CHN 覆盖 1980-2031），' +
        '做实际值比对时请按 period 自行截年份；period_min / period_max 给出实际范围。' +
        '常用指标：NGDPD（GDP 现价美元，十亿）、NGDP_RPCH（实际 GDP 增速 %）、' +
        'NGDPDPC（人均 GDP 现价美元）、PCPIPCH（通胀 %）、LUR（失业率 %）。',
      parameters: {
        type: 'object',
        properties: {
          indicator: { type: 'string', description: '指标代码，如 NGDPD' },
          country: { type: 'string', description: 'ISO3 国家代码，如 CHN' },
        },
        required: ['indicator', 'country'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['indicator', 'country'])
        return [
          'fetch-indicator',
          '--indicator', args.indicator,
          '--country', args.country,
        ]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'IMF ' + (args.indicator ?? '') + ' / ' + (args.country ?? ''),
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'imf_list_indicators',
      description:
        '列出 IMF DataMapper 的全部指标（实测 132 条），每条含 id / label / unit / ' +
        'source / dataset / description。用它能发现可用的指标代码。',
      parameters: { type: 'object', properties: {} },
      buildCliArgs() {
        return ['list-indicators']
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: () => ({
        card: 'generic',
        title: 'IMF 指标目录',
        kind: 'other',
        rawInput: {},
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'imf_list_countries',
      description:
        '列出某 IMF 指标有数据的国家/地区（实测 NGDPD 有 229 个）。' +
        'IMF 没有独立的国家目录端点，这是从该指标的数据里反推出来的；' +
        '每条含 id（ISO3）、year_count、first_year、last_year。',
      parameters: {
        type: 'object',
        properties: {
          indicator: { type: 'string', description: '指标代码，如 NGDPD' },
        },
        required: ['indicator'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['indicator'])
        return ['list-countries', '--indicator', args.indicator]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'IMF 国家列表 ' + (args?.indicator ?? ''),
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.effect(
    () => () => console.log(marker + ' plugin disposed — IMF 工具已卸载'),
    'imf-adapter lifetime',
  )
}

