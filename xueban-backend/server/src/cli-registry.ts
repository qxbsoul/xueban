/**
 * cli-registry.ts — Static registry of known AI CLIs (Story 15.19 v2)
 *
 * The registry is the single source of truth for:
 *   - which binaries we probe on PATH (`cli-detector.ts`)
 *   - how to invoke each CLI from the spawn bridge (`skill-runners/cli.ts`)
 *   - which line-stream parser to use to normalize stdout into Xueban's
 *     `<sf:*>` text-protocol (`parsers/cli-streams/index.ts`)
 *
 * Adding a new CLI = one entry here + (optionally) one parser in
 * `parsers/cli-streams/`. No other file needs to change.
 */

export type StreamFormat =
  | 'claude-stream-json'
  | 'codex-stream-json'
  | 'gh-copilot'
  | 'plain-line'
  | 'cursor-acp';

/**
 * ClawHub（2026-06）— 结构化安装规格。
 * install_cmd 是自由文本（可能是「参见官方文档」），无法安全执行；
 * install_spec 是显式声明的 argv 数组，spawn 时 shell:false 无注入面。
 */
export interface InstallSpec {
  /** 包管理器二进制名（必须命中白名单，见 lib/claw-installer.ts：npm/pnpm/yarn/pip/pip3/pipx/uv/brew/cargo/go/gh/hermes）。 */
  pm: 'npm' | 'pnpm' | 'yarn' | 'pip' | 'pip3' | 'pipx' | 'uv' | 'brew' | 'cargo' | 'go' | 'gh' | 'hermes' | 'node';
  /** 传给 pm 的参数数组。 */
  args: string[];
}

/**
 * 卸载规格（ClawHub 2026-10-30 生命周期闭环）。缺省 = 该 CLI 不可自动卸载
 * （copy-only 引导）。与 InstallSpec 同白名单；npm/pip 系加 -y 类跳过确认参数。
 */
export interface UninstallSpec {
  pm: InstallSpec['pm'];
  args: string[];
}

export interface CliDescriptor {
  /** Stable id used in `executor: cli:<id>` skill frontmatter. */
  id: string;
  /** Binary name on PATH. Probed via `which` (POSIX) / `where` (Windows). */
  binary: string;
  /** Argument that prints version + exits — used during detection. */
  version_arg: string;
  /** Optional env var that must be set before the CLI is usable. */
  needs_env?: string;
  /** Human-readable installation hint shown in the UI when missing. */
  install_cmd: string;
  /** Selects the stdout parser. */
  stream_format: StreamFormat;
  /** Extra args to spawn the CLI with (prompt is sent via stdin). */
  extra_args?: string[];
  /** Alternative binary names tried if primary `binary` not found on PATH. */
  fallback_bins?: string[];
  /** Maps "--flag-string" → "camelCaseKey"; detector scans --help output for each. */
  capability_flags?: Record<string, string>;
  /** Static model IDs shown in UI when dynamic fetch unavailable. */
  fallback_models?: string[];
  /** Short human-readable auth note shown in the UI. */
  auth_hint?: string;
  /**
   * Prompt 传递方式（ClawHub 2026-10-30）：缺省 'stdin'（历史行为，prompt 写
   * 子进程 stdin 后 end）。'argv-<flag>' = 以 `<flag> <prompt>` 追加到命令行
   * （如 hermes 的 `-z`）。TUI 型 CLI（hermes 等）读 stdin 会进交互界面，
   * 必须 argv 传。约定：spawn 前 prompt 以参数形式追加，不走 stdin。
   */
  prompt_mode?: string;
  // ── ClawHub 增量字段（全部 optional，既有 21 条零改动向后兼容）──────────
  /** true = ClawHub 门面中的「Claw」（目录页置顶高亮 + 聊天选择器收录）。 */
  is_claw?: boolean;
  /** 一句话简介（目录卡片）。 */
  description?: string;
  /** 分类：'assistant' | 'research' | 'coding' | 'other'（目录筛选）。 */
  category?: string;
  /** 官网/文档链接（详情抽屉 + copy-only 安装引导）。 */
  homepage?: string;
  /** 结构化安装规格；缺省时按 install_cmd 前缀白名单解析。 */
  install_spec?: InstallSpec;
  /** 结构化卸载规格（2026-10-30 生命周期闭环）；缺省 = 不可自动卸载。 */
  uninstall_spec?: UninstallSpec;
  /** 卸载引导文案（不可自动卸载时展示，如 gh extension remove …）。 */
  uninstall_cmd?: string;
}

/**
 * 20 known AI CLIs (AC-2). Order matters for `cli:auto` — first detected wins.
 */
export const KNOWN_CLIS: CliDescriptor[] = [
  {
    id: 'claude',
    binary: 'claude',
    version_arg: '--version',
    // 2026-05-11 — local `claude login` is the primary auth path for the CLI;
    // ANTHROPIC_API_KEY env is a fallback. The CLI manages its own credential
    // store, so the daemon does NOT need a sk-ant- key in Xueban settings
    // when the user picks executor=cli:claude.
    needs_env: undefined,
    install_cmd: 'npm i -g @anthropic-ai/claude-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@anthropic-ai/claude-cli'] },
    stream_format: 'claude-stream-json',
    // 2026-05-11 bug fix — align with OpenDesign's verified spawn args
    // (nexu-io/open-design apps/daemon/src/agents.ts):
    //   • `--verbose` REQUIRED so stream-json mode emits per-token deltas;
    //     without it the parser sees nothing until EOF and SSE looks hung.
    //   • `--permission-mode bypassPermissions` REQUIRED so claude doesn't
    //     pause for "Y/N" tool prompts; without it the child hangs on stdin
    //     and the front-end reconnects forever.
    //   • `-p` (short form of --print) keeps the child non-interactive.
    extra_args: [
      '-p',
      '--output-format', 'stream-json',
      '--verbose',
      '--permission-mode', 'bypassPermissions',
    ],
    fallback_bins: ['openclaude'],
    capability_flags: {
      '--permission-mode': 'permissionMode',
      '--output-format': 'outputFormat',
      '--verbose': 'verbose',
      '--model': 'model',
    },
    fallback_models: ['claude-opus-4-7', 'claude-sonnet-4-6', 'claude-haiku-4-5'],
    auth_hint: 'Run `claude login`',
  },
  {
    id: 'codex',
    binary: 'codex',
    version_arg: '--version',
    needs_env: 'OPENAI_API_KEY',
    install_cmd: 'npm i -g @openai/codex',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@openai/codex'] },
    stream_format: 'codex-stream-json',
    extra_args: ['--stream'],
    fallback_bins: [],
    capability_flags: {
      '--stream': 'stream',
      '--model': 'model',
      '--no-project-doc': 'noProjectDoc',
    },
    fallback_models: ['o4-mini', 'o3', 'gpt-4o'],
    auth_hint: 'Set OPENAI_API_KEY env var',
  },
  {
    id: 'gh-copilot',
    binary: 'gh',
    version_arg: '--version',
    install_cmd: 'gh extension install github/gh-copilot',
    install_spec: { pm: 'gh', args: ['extension', 'install', 'github/gh-copilot'] },
    uninstall_spec: { pm: 'gh', args: ['extension', 'remove', 'github/gh-copilot'] },
    stream_format: 'gh-copilot',
    // gh extensions read prompt from argv; we still pipe via stdin and pass `-`.
    extra_args: ['copilot', 'suggest', '-t', 'shell', '-'],
    fallback_bins: ['gh'],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: ['gpt-4o', 'claude-sonnet-4-6'],
    auth_hint: 'Run `gh auth login`',
  },
  {
    id: 'gemini',
    binary: 'gemini',
    version_arg: '--version',
    needs_env: 'GEMINI_API_KEY',
    install_cmd: 'npm i -g @google/gemini-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@google/gemini-cli'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
      '--sandbox': 'sandbox',
    },
    fallback_models: ['gemini-2.5-pro', 'gemini-2.0-flash'],
    auth_hint: 'Set GEMINI_API_KEY env var',
  },
  {
    id: 'openclaw',
    binary: 'openclaw',
    version_arg: '--version',
    install_cmd: 'npm i -g openclaw',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'openclaw'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
      '--stream': 'stream',
    },
    fallback_models: ['openclaw-default'],
    auth_hint: 'See openclaw docs',
    // ── ClawHub（2026-06）：openclaw 是 Claw 之一，补展示字段（增量，不改既有行）
    is_claw: true,
    description: 'OpenClaw — 开源 AI 助手 CLI，支持模型切换与流式输出',
    category: 'assistant',
    homepage: 'https://openclaw.ai',
  },
  {
    // Polynoia 三大 CLI 之一（claude/codex/opencode）。`opencode run` 无位置参
    // 时从 stdin 读消息（spawner 正是把 prompt 写到 stdin），stdout 逐行输出
    // 回复文本 → plain-line 解析即可，无需专用 parser。extra_args 见
    // KNOWN_CLIS 消费方（transport/spawners/cli.ts 静态拼接）。
    id: 'opencode',
    binary: 'opencode',
    version_arg: '--version',
    install_cmd: 'npm i -g opencode-ai',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'opencode-ai'] },
    // `run` 子命令进非交互模式；消息经 stdin 传入（spawner 行为）。
    extra_args: ['run'],
    stream_format: 'plain-line',
    fallback_bins: ['opencode-ai'],
    capability_flags: {
      '--model': 'model',
      '--format': 'format',
    },
    fallback_models: ['opencode/default'],
    auth_hint: 'Run `opencode` once to complete auth (opencode.ai/docs)',
  },
  {
    id: 'cursor-agent',
    binary: 'cursor-agent',
    version_arg: '--version',
    install_cmd: '参见 cursor.com/cli',
    stream_format: 'cursor-acp', // Phase C — falls through to plain-line in dispatcher; full ACP parser is Story 15.23.
    fallback_bins: [],
    capability_flags: {},
    fallback_models: ['cursor-small', 'gpt-4o'],
    auth_hint: 'Sign in via Cursor app',
  },
  {
    id: 'qwen-coder',
    binary: 'qwen-coder',
    version_arg: '--version',
    needs_env: 'DASHSCOPE_API_KEY',
    install_cmd: 'npm i -g qwen-coder-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'qwen-coder-cli'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: ['qwen-coder-32b'],
    auth_hint: 'Set DASHSCOPE_API_KEY env var',
  },
  {
    id: 'cline',
    binary: 'cline',
    version_arg: '--version',
    install_cmd: 'npm i -g cline',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'cline'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'Configure via VS Code extension',
  },
  {
    id: 'aider',
    binary: 'aider',
    version_arg: '--version',
    needs_env: 'OPENAI_API_KEY',
    install_cmd: 'pip install aider-chat',
    uninstall_spec: { pm: 'pip', args: ['uninstall', '-y', 'aider-chat'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
      '--stream': 'stream',
    },
    fallback_models: ['gpt-4o', 'claude-sonnet-4-6'],
    auth_hint: 'Set OPENAI_API_KEY or ANTHROPIC_API_KEY',
  },
  {
    id: 'cursor',
    binary: 'cursor',
    version_arg: '--version',
    install_cmd: '从 cursor.com 下载',
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'Sign in via Cursor app',
  },
  {
    id: 'windsurf-cli',
    binary: 'windsurf',
    version_arg: '--version',
    install_cmd: '从 windsurf 官网下载',
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'Sign in via Windsurf app',
  },
  {
    id: 'devin',
    binary: 'devin',
    version_arg: '--version',
    install_cmd: 'npm i -g @cognition/devin',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@cognition/devin'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'Set DEVIN_API_KEY env var',
  },
  {
    id: 'hermes',
    binary: 'hermes',
    version_arg: '--version',
    install_cmd: '参见 hermes 官方文档',
    // 安装走官方安装脚本（多步、含 venv 创建），copy-only 引导；卸载用自带
    // 子命令可直接执行（--yes 跳确认；不加 --full 保留配置/数据，重装不丢）。
    uninstall_spec: { pm: 'hermes', args: ['uninstall', '--yes'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: [],
    auth_hint: 'See hermes docs',
    // ── ClawHub（2026-10-30 修复）：hermes 读 stdin 会拉起交互 TUI（横幅+
    // 等待输入），单次执行必须用 `-z <prompt>` 参数式传 prompt（实测验证：
    // `hermes -z "回复两个字:你好"` → "你好"）。
    prompt_mode: 'argv--z',
    // ── ClawHub（2026-06）：hermes 是 Claw 之一，补展示字段（增量，不改既有行）
    is_claw: true,
    description: 'Hermes — 消息驱动的 AI 助手 CLI（安装需参见官方文档，copy-only）',
    category: 'assistant',
  },
  {
    id: 'kimi',
    binary: 'kimi',
    version_arg: '--version',
    needs_env: 'MOONSHOT_API_KEY',
    install_cmd: 'npm i -g kimi-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'kimi-cli'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: ['kimi-latest'],
    auth_hint: 'Set MOONSHOT_API_KEY env var',
  },
  {
    id: 'qoder',
    binary: 'qoder',
    version_arg: '--version',
    install_cmd: 'npm i -g qoder',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'qoder'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'See qoder docs',
  },
  {
    id: 'pi',
    binary: 'pi',
    version_arg: '--version',
    install_cmd: 'npm i -g pi-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'pi-cli'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'See pi docs',
  },
  {
    id: 'kiro',
    binary: 'kiro',
    version_arg: '--version',
    install_cmd: 'npm i -g @aws/kiro',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@aws/kiro'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: [],
    auth_hint: 'Sign in via kiro CLI',
  },
  {
    id: 'kilo',
    binary: 'kilo',
    version_arg: '--version',
    install_cmd: 'npm i -g kilo',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', 'kilo'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: 'See kilo docs',
  },
  {
    id: 'vibe',
    binary: 'vibe',
    version_arg: '--version',
    needs_env: 'MISTRAL_API_KEY',
    install_cmd: 'npm i -g @mistralai/vibe-cli',
    uninstall_spec: { pm: 'npm', args: ['uninstall', '-g', '@mistralai/vibe-cli'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: [],
    auth_hint: 'Set MISTRAL_API_KEY env var',
  },
  {
    id: 'deepseek-tui',
    binary: 'deepseek',
    version_arg: '--version',
    needs_env: 'DEEPSEEK_API_KEY',
    install_cmd: 'pip install deepseek-tui',
    uninstall_spec: { pm: 'pip', args: ['uninstall', '-y', 'deepseek-tui'] },
    stream_format: 'plain-line',
    fallback_bins: [],
    capability_flags: {
      '--model': 'model',
    },
    fallback_models: [],
    auth_hint: 'Set DEEPSEEK_API_KEY env var',
  },
  {
    // ── ClawHub（2026-06）— AutoResearchClaw 注册项（FR-07）────────────────
    // 2026-10-30 已校准：AutoResearchClaw 是 GitHub 项目（非 PyPI 包），
    // 安装 = git clone + venv 内 pip install -e .；CLI 入口 researchclaw。
    // 本机已装：C:/Users/jy/AutoResearchClaw（venv Python 3.14），
    // 全局 wrapper：~/.local/bin/researchclaw（路由到 venv python -m）。
    // 单次执行 = `researchclaw run --topic "<prompt>" --auto-approve`。
    id: 'autoresearchclaw',
    binary: 'researchclaw',
    version_arg: 'info',
    // 安装需 git clone（多步、跨目录），白名单外 → copy-only 引导。
    install_cmd:
      'git clone https://github.com/aiming-lab/AutoResearchClaw.git && cd AutoResearchClaw && python3 -m venv .venv && .venv/Scripts/pip install -e .',
    // venv 内安装，卸载 = 删除克隆目录 + PATH 里的 wrapper。目录内有用户的
    // 研究产物（artifacts/），平台不自动删 → copy-only 引导。
    uninstall_cmd:
      '删除 C:/Users/jy/AutoResearchClaw 目录（内含研究产物 artifacts/，请先备份）+ ~/.local/bin/researchclaw.exe',
    // 复用现有 plain-line 解析管道（决策 #4：不单独配置流式开关）。
    stream_format: 'plain-line',
    // 单次研究管线：prompt 作为 --topic 传入，--auto-approve 跳过人工门；
    // --config 指向本机绝对路径（ARC 按 cwd 自动探测在 run-session 目录下
    // 找不到配置，必须显式指定）。
    extra_args: [
      'run',
      '--auto-approve',
      '--config', 'C:/Users/jy/AutoResearchClaw/config.arc.yaml',
    ],
    fallback_bins: [],
    capability_flags: {},
    fallback_models: [],
    auth_hint: '需配置 config.arc.yaml（LLM base_url/api_key）',
    is_claw: true,
    description: '自动研究管线：输入课题 → 23 阶段研究 → 论文交付（GitHub: aiming-lab/AutoResearchClaw）',
    category: 'research',
    homepage: 'https://github.com/aiming-lab/AutoResearchClaw',
    // ARC 以 --topic 传 prompt（CLI 位置参数不收 stdin）。
    prompt_mode: 'argv---topic',
  },
];

/** Lookup helper — `undefined` for unknown ids. */
export function findCli(id: string): CliDescriptor | undefined {
  return KNOWN_CLIS.find((c) => c.id === id);
}
