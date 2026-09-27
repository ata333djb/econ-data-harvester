/**
 * nbs-adapter — 国家统计局数据采集工具的 DSH 适配层。
 *
 * 定位：**薄壳**。这里不实现任何采集、解析或规范化逻辑，只做两件事——
 *   1. 把 DSH 工具参数翻译成 `python -m econ_core.nbs_client_cli` 的 argv；
 *   2. 把 CLI 打到 stdout 的 JSON 信封翻译回工具返回值。
 * 所有业务逻辑都在 Python 侧（`nbs_client` 采集 / `normalize` 规范化），
 * 这样"领域算法通过 Python 模块执行，不硬编码在提示词里"这条准则才成立。
 *
 * 为什么不用 `defineTool`
 * ----------------------
 * `@deepseek-ai/dsh-tools` 的 `defineTool` 是运行时唯一权威的服务工厂，但本项目的
 * 插件不依赖任何 npm 包：DSH 的 loader 只对 YAML 里的**裸包名**做宿主侧解析
 * （见 .dsh/.agent-presets/econ-harvester/agent.cordis.yml 顶部注释），插件文件内部
 * 的 `import '@deepseek-ai/dsh-tools'` 会走 Node 自身的解析，而项目根没有 node_modules，
 * 实测报 ERR_MODULE_NOT_FOUND：
 *
 *   $ node -e "import('@deepseek-ai/dsh-tools')"    # 在项目根
 *   ERR_MODULE_NOT_FOUND
 *
 * 而 `defineTool` 的实现只是一层薄包装（把参数 spec 编译成 JSON Schema、加一层
 * 参数校验、返回普通对象），所以这里手写同形状的定义对象，保持插件零依赖 ——
 * 与同目录的 hello.js 一致（其注释也明确写了 loader 不做 TS 工具链、导入即用）。
 * 代价：没有 `ToolArgsError` 的路径级校验，改为在 `execute` 里自行校验必填项。
 *
 * CLI 契约（由 nbs_client_cli.py 保证）
 * ------------------------------------
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
export const name = 'nbs-adapter'

/** 本行需要工具注册表才能注册四个 NBS 工具。 */
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
const CLI_MODULE = 'econ_core.nbs_client_cli'

/** 工具执行超时（毫秒）。 */
const TIMEOUT_MS = 60_000

/** stdout 上限：目录树可能很大，给足空间。 */
const MAX_BUFFER = 32 * 1024 * 1024

/** 默认地区代码：全国。 */
const NATIONAL_REGION = '000000000000'

// --------------------------------------------------------------------------- //
// 子进程执行
// --------------------------------------------------------------------------- //

/**
 * 以项目 venv 的 python 运行 CLI，返回原始 stdout/stderr 与错误。
 *
 * @param {string[]} cliArgs - 传给 nbs_client_cli 的 argv（不含解释器与模块名）。
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
 * @returns {object} `{ok, data, error?, raw_cache?, stderr, ...}`
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
      envelope.error = raw.error ? raw.error.message : `${toolName}: CLI 返回 ok=false 但未给 error`
    }
    return envelope
  }

  const detail = raw.error
    ? `${raw.error.message}${raw.error.code !== undefined ? ` (code=${raw.error.code})` : ''}`
    : `${toolName}: CLI 未返回可解析的 JSON 信封`
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
    throw new Error(`invalid arguments: missing or empty required string(s): ${missing.join(', ')}`)
  }
}

// --------------------------------------------------------------------------- //
// 工具定义（形状对齐 defineTool 的返回值）
// --------------------------------------------------------------------------- //

/** 四个工具共用的输出信封 schema。 */
const OUTPUT_SCHEMA = {
  type: 'object',
  required: ['ok'],
  properties: {
    ok: { type: 'boolean' },
    command: { type: 'string' },
    // `data` 是异构载荷：fetch-indicator / get-catalog-tree / list-provinces 是数组，
    // get-default-indicator 是对象，失败信封里是 null（见 nbs_client_cli._ok/_fail）。
    // DSH 的 schema 子集只认单值 type，没有 `json`，也不支持 ['object','array'] 这类类型数组；
    // 因此这里用 annotation-only 节点——schema 校验器明确把它当作「不受约束的 JSON」标准写法，
    // 渲染出的 TS 类型是 JsonValue。写成 type:'object' 会在数组/null 上运行时校验失败。
    data: { description: '命令载荷：数组或对象；失败时为 null（各子命令形状不同）' },
    error: { type: 'string' },
    row_count: { type: 'integer' },
    raw_cache: { type: 'string' },
    parsed_cache: { type: 'string' },
    fetched_at: { type: 'string' },
    region_code: { type: 'string' },
    region_name: { type: 'string' },
    period_count: { type: 'integer' },
    code: { type: 'integer' },
    cid: { type: 'string' },
    y_series_count: { type: 'integer' },
    x_point_count: { type: 'integer' },
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
    return `失败: ${value?.error ?? '未知错误'}`
  }
  const bits = [`command=${value.command}`]
  if (typeof value.row_count === 'number') bits.push(`条数=${value.row_count}`)
  if (value.raw_cache) bits.push(`raw=${value.raw_cache}`)
  return bits.join(' · ')
}

/**
 * 注册四个 NBS 工具。
 *
 * @param {object} ctx - 携带 tools 注册表的 cordis 上下文。
 */
export function apply(ctx) {
  const marker = `[${name}]`
  console.log(`${marker} plugin applied — 注册 NBS 工具（Python CLI 转发）`)

  ctx.tools.register(
    makeTool({
      name: 'nbs_fetch_indicator',
      description:
        '采集国家统计局（NBS）指标数据，返回规范化后的长表行。' +
        '每行含 region_code/region_name/indicator_id/tree_node_id/indicator_name/' +
        'period/period_type/value/unit/source/fetched_at/raw_cache/row_sha16/raw_fields。' +
        '注意 indicator_id 与 tree_node_id 语义不同：前者是响应里的语义标识（i），' +
        '后者是取数实际使用的树节点 id。',
      parameters: {
        type: 'object',
        properties: {
          cid: { type: 'string', description: '数据集 UUID，如 GDP 用 f7fd25aaad184414875632cf2327da60' },
          indicator_id: { type: 'string', description: '响应里的 i 值（语义标识），如 db8e5a86c08246e79b1b11251927e740' },
          tree_node_id: { type: 'string', description: '请求里的 id 值（取数用），如 7dc6a2ee6c614960b7059991e0cc4d96' },
          root_id: { type: 'string', description: '指标树首个一级类目 _id，如 71d41888d5a44bb2a67402ef4e60003e' },
          region_code: { type: 'string', description: '地区代码，默认 000000000000（全国）' },
          periods: { type: 'string', description: '逗号分隔的时间码，如 "2015YY,2016YY,...,2024YY"' },
        },
        required: ['cid', 'indicator_id', 'tree_node_id', 'root_id', 'periods'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['cid', 'indicator_id', 'tree_node_id', 'root_id', 'periods'])
        return [
          'fetch-indicator',
          '--cid', args.cid,
          '--indicator-id', args.indicator_id,
          '--tree-node-id', args.tree_node_id,
          '--root-id', args.root_id,
          '--region-code', args.region_code ?? NATIONAL_REGION,
          '--periods', args.periods,
        ]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: `采集 NBS 指标 ${args.indicator_name ?? args.tree_node_id ?? ''}`,
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'nbs_get_catalog_tree',
      description: '按 cid 取 NBS 的「指标 -> 数据表」目录树（GET getDaCatalogTreeByIndicatorCid）。',
      parameters: {
        type: 'object',
        properties: {
          cid: { type: 'string', description: '指标标识，即指标记录里的 i 值，如 db8e5a86c08246e79b1b11251927e740' },
        },
        required: ['cid'],
      },
      buildCliArgs(args) {
        requireStrings(args, ['cid'])
        return ['get-catalog-tree', '--cid', args.cid]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: `NBS 目录树 ${args.cid ?? ''}`,
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'nbs_get_default_indicator',
      description: '取 NBS 默认推荐指标数据（如 CPI）。年度 code=21、季度=20、月度=19。',
      parameters: {
        type: 'object',
        properties: {
          code: { type: 'integer', description: '指标代码：年度=21、季度=20、月度=19（可后续校准）' },
        },
        required: ['code'],
      },
      buildCliArgs(args) {
        if (!Number.isInteger(args?.code)) {
          throw new Error('invalid arguments: `code` must be an integer')
        }
        return ['get-default-indicator', '--code', String(args.code)]
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: `NBS 默认指标 code=${args.code ?? ''}`,
        kind: 'other',
        rawInput: args,
      }),
    }),
  )

  ctx.tools.register(
    makeTool({
      name: 'nbs_list_provinces',
      description: '列出 NBS 全部省级地区代码（实测 34 条 = 31 省区市 + 台湾/香港/澳门），' +
        '返回项的 value 可直接作为 nbs_fetch_indicator 的 region_code。',
      parameters: { type: 'object', properties: {} },
      buildCliArgs() {
        return ['list-provinces']
      },
      render: (_args, value) => [{ type: 'text', text: shortSummary(value) }],
      presentCall: (args) => ({
        card: 'generic',
        title: 'NBS 省级地区列表',
        kind: 'other',
        rawInput: args ?? {},
      }),
    }),
  )

  ctx.effect(
    () => () => console.log(`${marker} plugin disposed — NBS 工具已卸载`),
    'nbs-adapter lifetime',
  )
}
