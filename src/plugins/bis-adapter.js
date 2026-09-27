/**
 * bis-adapter — BIS（国际清算银行）采集工具的 DSH 适配层。
 *
 * 定位：**薄壳**，与同目录 nbs-adapter.js / worldbank-adapter.js / imf-adapter.js /
 * fred-adapter.js 结构逐段对应。只做两件事：
 *   1. 把 DSH 工具参数翻译成 'python -m econ_core.bis_client_cli' 的 argv；
 *   2. 把 CLI 打到 stdout 的 JSON 信封翻译回工具返回值。
 *
 * BIS 特有注意点
 * --------------
 * 1. BIS 返回的是**小写** `content-encoding: gzip`，http_client 的大小写敏感查找取不到
 *    该头，因此漏解压 —— 这一层在 Python 侧就地绕开（bis_client._decode_body）。
 *    插件这里**不要**再动 Accept-Encoding 之类的头，那是 Python 侧的事。
 * 2. BIS 的 `?format=jsondata` 恒 406，只认 `format=sdmx-json`（同样在 Python 侧固定）。
 * 3. SDMX 密钥必须给满三位 FREQ.REF_AREA.UNIT_MEASURE（如 M.CN.771），少一位会 404。
 *    插件侧只透传 key，不做形状校验 —— 校验逻辑只有一处，在 Python 侧。
 *
 * 为什么不用 defineTool：同 nbs-adapter.js —— 项目根没有 node_modules，
 * 插件内 import '@deepseek-ai/dsh-tools' 会 ERR_MODULE_NOT_FOUND。
 *
 * 本文件不使用 JS 模板字符串（反引号 / 美元花括号插值），一律用单引号拼接。
 *
 * CLI 契约（由 bis_client_cli.py 保证）
 * ------------------------------------
 *   - 成功：stdout 是单行 JSON 信封 {ok:true, command, data, ...}，退出码 0
 *   - 失败：stdout 仍是 {ok:false, command, data:null, error}，退出码 2
 *   - 人类可读日志走 stderr，execute 永远把 stderr 原样带回
 */

// node:child_process 是 Node 内建模块，不经 node_modules 解析，零依赖插件可安全 import。
import { execFile } from 'node:child_process'

/** Cordis reads the exported name as this plugin's identity in the tree. */
export const name = 'bis-adapter'

/** 本行需要工具注册表才能注册两个 BIS 工具。 */
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
const CLI_MODULE = 'econ_core.bis_client_cli'

/** 工具执行超时（毫秒）。 */
const TIMEOUT_MS = 120_000

/** stdout 上限：单条序列的 SDMX-JSON 解压后约 47 KB，给足空间。 */
const MAX_BUFFER = 32 * 1024 * 1024

// --------------------------------------------------------------------------- //
// 子进程执行
// --------------------------------------------------------------------------- //

/**
 * 以项目 venv 的 python 运行 CLI，返回原始 stdout/stderr 与错误。
 *
 * @param {string[]} cliArgs - 传给 bis_client_cli 的 argv（不含解释器与模块名）。
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
    // data 是异构载荷：fetch-cpi / fetch-series 都是 [{period, value}] 数组，
    // 失败信封里是 null。DSH 的 schema 子集只认单值 type，没有 json，也不支持类型数组；
    // 因此这里用 annotation-only 节点（schema 校验器把它当作不受约束的 JSON）。
    data: { description: '命令载荷：数组或对象；失败时为 null' },
    error: { type: 'string' },
    row_count: { type: 'integer' },
    dataset: { type: 'string' },
    key: { type: 'string' },
    frequency: { type: 'string' },
    unit: { type: 'string' },
    unit_meaning: { type: 'string' },
    country: { type: 'string' },
    note: { type: 'string' },
    raw_cache: { type: 'string' },
    parsed_cache: { type: 'string' },
    fetched_at: { type: 'string' },
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
  if (value.key) bits.push('key=' + value.key)
  else if (value.unit) bits.push('unit=' + value.unit)
  if (value.frequency) bits.push('频率=' + value.frequency)
  if (value.period_min) bits.push('范围=' + value.period_min + '~' + value.period_max)
  if (value.raw_cache) bits.push('raw=' + value.raw_cache)
  return bits.join(' · ')
}

/**
 * 注册两个 BIS 工具。
 *
 * @param {object} ctx - 携带 tools 注册表的 cordis 上下文。
 */
export function apply(ctx) {
  const marker = '[' + name + ']'
  console.log(marker + ' plugin applied — 注册 BIS 工具（Python CLI 转发）')

  ctx.tools.register(
    makeTool({
      name: 'bis_fetch_cpi',
      description:
        '采集 BIS（国际清算银行）的中国 CPI，返回 [{"period": "1996-01", "value": 9.0}, ...]。' +
        'unit="771" 返回同比变化 %（实测 1996-01 起，368 期）；unit="628" 返回指数（2010=100，' +
        '实测 1995-01 起，380 期）—— 比项目现有中国 CPI 序列（2015 起）往前扩了 20 年。' +
        'freq="M" 月度（默认）/ "A" 年度。注意：BIS 是**转载方**（原始数据来自中国国家统计局），' +
        '只对序列做了拼接与重定基，因此这不是独立验证；实测与 NBS「上年=100」折算后差异 ' +
        'max 0.174 pp / mean 0.09 pp。用途是扩长序列与拼接检查。',
      parameters: {
        type: 'object',
        properties: {
          unit: {
            type: 'string',
            description: '771=同比变化%（默认）；628=指数（2010=100）',
          },
          country: { type: 'string', description: 'ISO2 地区代码，默认 CN' },
          freq: { type: 'string', description: 'M=月度（默认）；A=年度' },
        },
        required: [],
      },
      buildCliArgs(args) {
        const out = ['fetch-cpi']
        if (typeof args.unit === 'string' && args.unit.trim()) out.push('--unit', args.unit)
        if (typeof args.freq === 'string' && args.freq.trim()) out.push('--freq', args.freq)
        if (typeof args.country === 'string' && args.country.trim()) out.push('--country', args.country)
        return out
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'BIS CPI ' + (args?.country ?? 'CN') + ' unit=' + (args?.unit ?? '771'),
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'bis_fetch_series',
      description:
        '通用 BIS SDMX-JSON 拉取，返回 [{"period": "1978", "value": 18.66}, ...]。' +
        'dataset 是 dataflow id（如 WS_LONG_CPI = 长期消费价格统计；' +
        '另可用 WS_CBPOL 央行政策利率 / WS_EER 有效汇率 / WS_XRU 美元汇率等）。' +
        'key 是 SDMX 密钥，**必须按 FREQ.REF_AREA.UNIT_MEASURE 给满三位**（如 "M.CN.771"、' +
        '"A.CN.628"），少给一位 BIS 会返回 404 而不是 400。' +
        '注意 BIS 只认 format=sdmx-json（jsondata 恒 406），该细节在 Python 侧固定。',
      parameters: {
        type: 'object',
        properties: {
          dataset: { type: 'string', description: 'dataflow id，如 WS_LONG_CPI' },
          key: {
            type: 'string',
            description: 'SDMX 密钥，三位 FREQ.REF_AREA.UNIT_MEASURE，如 M.CN.771',
          },
        },
        required: ['dataset', 'key'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['dataset', 'key'])
        return ['fetch-series', '--dataset', args.dataset, '--key', args.key]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'BIS ' + (args?.dataset ?? '') + ' ' + (args?.key ?? ''),
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.effect(
    () => () => console.log(marker + ' plugin disposed — BIS 工具已卸载'),
    'bis-adapter lifetime',
  )
}
