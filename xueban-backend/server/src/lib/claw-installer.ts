/**
 * claw-installer.ts — ClawHub 安装执行器（2026-06）
 *
 * 职责：
 *  - parseInstallCommand(install_cmd)：把自由文本安装命令解析为白名单
 *    {pm, args}（仅当首 token 命中包管理器白名单时；其余返回 null → copy-only）
 *  - startInstall(cliId)：建 InstallSession（内存 Map）+ spawn(pm, args,
 *    {shell:false})，stdout/stderr 逐 chunk 进事件队列
 *  - streamEvents(session)：安装 SSE 事件生成器（install-start → install-log*
 *    → install-done | install-error），供 routes/clawhub.ts 写入 res
 *  - cancelInstall(id)：SIGTERM → 5s → SIGKILL（复用 spawner 终止惯例）
 *
 * 安全边界（架构文档决策 D5 / 共享知识 #6）：
 *  - 全部 shell:false + argv 数组，无注入面
 *  - pm 必须命中白名单（npm/pnpm/yarn/pip/pip3/pipx/uv/brew/cargo/go/gh）
 *  - Windows 下 npm/yarn/pnpm 等补 .cmd 后缀（node spawn 无法直接执行
 *    npm.bat/.cmd —— cli-detector 的 which 同款先例）
 */

import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { platform } from 'node:os';
import { findCli, type InstallSpec, type UninstallSpec } from '../cli-registry';

/** 包管理器白名单 —— install_spec.pm 与 parseInstallCommand 首 token 的唯一准入集。
 *  （2026-10-30 增补：gh——gh extension install/remove；hermes——自带 uninstall 子命令） */
export const PM_WHITELIST: ReadonlySet<string> = new Set([
  'npm', 'pnpm', 'yarn', 'pip', 'pip3', 'pipx', 'uv', 'brew', 'cargo', 'go', 'gh', 'hermes',
]);

/** 测试辅助：临时向白名单注入一个 pm（返回恢复函数）。 */
export function __testInjectPm(pm: string): () => void {
  (PM_WHITELIST as Set<string>).add(pm);
  return () => {
    (PM_WHITELIST as Set<string>).delete(pm);
  };
}

/** Windows 下这些 pm 需要补 .cmd 后缀才能被 spawn（shell:false）直接执行。 */
const WIN_CMD_SUFFIX_PMS: ReadonlySet<string> = new Set([
  'npm', 'pnpm', 'yarn', 'npx',
]);

export interface ParsedCommand {
  pm: string;
  args: string[];
}

/**
 * 解析自由文本安装命令为 {pm, args}。
 *  - 首个 token 必须命中白名单，且必须至少带 1 个参数（裸 `npm` 不是安装命令）
 *  - shell 元字符（; | & > < ` $( )）直接拒绝 —— 防御 copy-paste 注入
 * 返回 null = copy-only（不可自动执行）。
 */
export function parseInstallCommand(cmd: string): ParsedCommand | null {
  if (!cmd || typeof cmd !== 'string') return null;
  const trimmed = cmd.trim();
  if (!trimmed) return null;
  // 拒绝任何 shell 元字符（我们不用 shell，但句子本身混入这些字符说明它
  // 不是一条单纯命令，宁降级不执行）。
  if (/[;&|<>`$]/.test(trimmed)) return null;
  const tokens = trimmed.split(/\s+/);
  const pm = tokens[0] ?? '';
  if (!PM_WHITELIST.has(pm)) return null;
  const args = tokens.slice(1);
  if (args.length === 0) return null;
  return { pm, args };
}

export type InstallStatus =
  | 'pending'      // 已建会话，子进程尚未 spawn（SSE 尚未附加）
  | 'running'      // 子进程运行中
  | 'done'         // exit 0
  | 'error'        // exit ≠0 / spawn 失败 / PM 不存在
  | 'canceled';    // 用户取消

/** install-error 帧的错误码（与架构文档 §3.6 对齐）。 */
export type InstallErrorCode = 'INSTALL_FAILED' | 'INSTALL_CANCELED' | 'PM_NOT_FOUND';

export interface InstallLogLine {
  stream: 'stdout' | 'stderr';
  line: string;
}

export interface InstallErrorPayload {
  code: InstallErrorCode;
  message: string;
  exit_code?: number;
  stderr_tail: string;
}

export interface InstallSession {
  install_id: string;
  cli_id: string;
  /** 展示用完整命令文本（install_cmd 原文或 install_spec 拼接）。 */
  command: string;
  /** 实际执行的 argv（shell:false）。 */
  argv: string[];
  status: InstallStatus;
  started_at: string;
  /** 心跳 + 事件队列：SSE 订阅端 drain；未订阅时事件仍累积（断线重连重放）。 */
  events: Array<{ event: string; data: Record<string, unknown> }>;
  /** resolve 当子进程退出（routes 层 await 用）；reject 仅在异常路径。 */
  finished: Promise<void>;
  /** 内部：取消信号。 */
  _cancelRequested: boolean;
  _child: ChildProcess | null;
  _stderrTail: string;
}

/** stderr 尾巴截断长度（架构文档 §3.6：2000 字符）。 */
const STDERR_TAIL_MAX = 2000;

/** 完成的会话在内存里保留多久（前端可重放终态帧后清理）。 */
const SESSION_TTL_MS = 10 * 60 * 1000;

const sessions = new Map<string, InstallSession>();

/** 定时清理终态会话（setInterval 惰性创建，测试环境下 unref 不阻退出）。 */
let sweeper: NodeJS.Timeout | null = null;
function ensureSweeper(): void {
  if (sweeper) return;
  sweeper = setInterval(() => {
    const now = Date.now();
    for (const [id, s] of sessions) {
      const started = Date.parse(s.started_at);
      const terminal = s.status === 'done' || s.status === 'error' || s.status === 'canceled';
      if (terminal && Number.isFinite(started) && now - started > SESSION_TTL_MS) {
        sessions.delete(id);
      }
    }
    if (sessions.size === 0 && sweeper) {
      clearInterval(sweeper);
      sweeper = null;
    }
  }, 60_000);
  if (typeof sweeper.unref === 'function') sweeper.unref();
}

/** Windows pm 名 → 可执行名补全（npm → npm.cmd）。 */
function resolvePmBinary(pm: string): string {
  if (platform() === 'win32' && WIN_CMD_SUFFIX_PMS.has(pm)) {
    return `${pm}.cmd`;
  }
  return pm;
}

/**
 * Windows spawn 适配（2026-10-30 修复）：
 * node ≥20.12.2 安全限制 —— shell:false 直接执行 .cmd/.bat 抛 EINVAL
 * （CVE-2024-27980 缓解）。对 .cmd 类 pm 改走 cmd.exe /c 中转：
 *   spawn('cmd.exe', ['/c', 'npm.cmd', ...args], { shell:false })
 * 安全性：pm 必须先命中白名单（PM_WHITELIST），args 来自注册表
 * install_spec/uninstall_spec（非用户自由输入），且 parseInstallCommand
 * 已拒绝全部 shell 元字符 —— 无注入面。
 */
function buildSpawnArgv(pmBinary: string, args: string[]): { file: string; argv: string[] } {
  if (platform() === 'win32' && /\.cmd$/i.test(pmBinary)) {
    return { file: 'cmd.exe', argv: ['/c', pmBinary, ...args] };
  }
  return { file: pmBinary, argv: args };
}

export class InstallError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'InstallError';
  }
}

/**
 * 启动一次安装会话。可能抛 InstallError：
 *  - 404 CLI_NOT_FOUND       未知 cliId
 *  - 422 INSTALL_NOT_EXECUTABLE  无 install_spec 且 install_cmd 不在白名单（copy-only）
 *  - 409 INSTALL_IN_PROGRESS    该 CLI 已有进行中的安装
 */
export function startInstall(cliId: string): InstallSession {
  const desc = findCli(cliId);
  if (!desc) {
    throw new InstallError(404, 'CLI_NOT_FOUND', `unknown cli id: ${cliId}`);
  }

  // 已在进行中的安装 → 409（同一 CLI 不并发装两次）。
  for (const s of sessions.values()) {
    if (s.cli_id === cliId && (s.status === 'pending' || s.status === 'running')) {
      throw new InstallError(409, 'INSTALL_IN_PROGRESS', `install already in progress for ${cliId}`);
    }
  }

  // 优先 install_spec；否则按 install_cmd 白名单解析（detect 层同源判定）。
  let spec: ParsedCommand | null = null;
  if (desc.install_spec) {
    const s = desc.install_spec as InstallSpec;
    if (PM_WHITELIST.has(s.pm)) spec = { pm: s.pm, args: [...s.args] };
  }
  if (!spec) spec = parseInstallCommand(desc.install_cmd);
  if (!spec) {
    throw new InstallError(
      422,
      'INSTALL_NOT_EXECUTABLE',
      `install_cmd for ${cliId} is not executable (copy-only): ${desc.install_cmd}`,
    );
  }

  const installId = `inst-${randomUUID().slice(0, 12)}`;
  const command = [spec.pm, ...spec.args].join(' ');
  const session: InstallSession = {
    install_id: installId,
    cli_id: cliId,
    command,
    argv: [resolvePmBinary(spec.pm), ...spec.args],
    status: 'pending',
    started_at: new Date().toISOString(),
    events: [],
    _cancelRequested: false,
    _child: null,
    _stderrTail: '',
    // finished 在 spawnInternal 里被赋真值；构造期先给一个已 resolve 的占位，
    // 防 race（理论上 startInstall 同步返回后 SSE 端立刻 await）。
    finished: Promise.resolve(),
  };

  const doSpawn = spawnInternal(session);
  session.finished = doSpawn;

  sessions.set(installId, session);
  ensureSweeper();
  return session;
}

/** spawn 子进程并接管整个生命周期（日志队列 + 终态帧）。 */
function spawnInternal(session: InstallSession): Promise<void> {
  return new Promise((resolveFinished) => {
    session.events.push({
      event: 'install-start',
      data: {
        install_id: session.install_id,
        cli_id: session.cli_id,
        command: session.command,
        started_at: session.started_at,
      },
    });

    let child: ChildProcess;
    try {
      // Windows .cmd 类 pm 走 cmd.exe /c 中转（见 buildSpawnArgv 注释）。
      const target = buildSpawnArgv(session.argv[0], session.argv.slice(1));
      child = spawn(target.file, target.argv, {
        shell: false,
        windowsHide: true,
        env: { ...process.env },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      session.status = 'error';
      session.events.push({
        event: 'install-error',
        data: {
          code: 'PM_NOT_FOUND',
          message: `无法启动包管理器: ${(err as Error).message}`,
          stderr_tail: '',
        } satisfies InstallErrorPayload as unknown as Record<string, unknown>,
      });
      resolveFinished();
      return;
    }
    session._child = child;
    session.status = 'running';

    child.on('error', (err: NodeJS.ErrnoException) => {
      // ENOENT = pm 不存在（未装 npm/pip 等）。
      if (session.status !== 'running') return;
      session.status = 'error';
      session.events.push({
        event: 'install-error',
        data: {
          code: 'PM_NOT_FOUND',
          message:
            err.code === 'ENOENT'
              ? `未找到包管理器 ${session.argv[0]}，请先安装它`
              : `启动安装进程失败: ${err.message}`,
          stderr_tail: session._stderrTail.slice(-STDERR_TAIL_MAX),
        } satisfies InstallErrorPayload as unknown as Record<string, unknown>,
      });
      resolveFinished();
    });

    child.stdout?.on('data', (b: Buffer) => {
      for (const line of b.toString().split(/\r?\n/)) {
        if (line.length === 0) continue;
        session.events.push({
          event: 'install-log',
          data: { stream: 'stdout', line } satisfies InstallLogLine as unknown as Record<string, unknown>,
        });
      }
    });
    child.stderr?.on('data', (b: Buffer) => {
      const text = b.toString();
      session._stderrTail += text;
      if (session._stderrTail.length > STDERR_TAIL_MAX * 4) {
        session._stderrTail = session._stderrTail.slice(-STDERR_TAIL_MAX * 2);
      }
      for (const line of text.split(/\r?\n/)) {
        if (line.length === 0) continue;
        session.events.push({
          event: 'install-log',
          data: { stream: 'stderr', line } satisfies InstallLogLine as unknown as Record<string, unknown>,
        });
      }
    });

    child.on('close', (code) => {
      if (session.status !== 'running') {
        // 已被 cancel 分支收尾。
        resolveFinished();
        return;
      }
      const finishedAt = new Date().toISOString();
      const durationMs = Date.now() - Date.parse(session.started_at);
      if (code === 0) {
        session.status = 'done';
        session.events.push({
          event: 'install-done',
          data: { exit_code: 0, finished_at: finishedAt, duration_ms: durationMs },
        });
      } else {
        session.status = 'error';
        session.events.push({
          event: 'install-error',
          data: {
            code: 'INSTALL_FAILED',
            exit_code: code ?? -1,
            message: `安装命令退出码 ${code ?? '(unknown)'}`,
            stderr_tail: session._stderrTail.slice(-STDERR_TAIL_MAX),
          } satisfies InstallErrorPayload as unknown as Record<string, unknown>,
        });
      }
      resolveFinished();
    });
  });
}

/**
 * 启动一次卸载会话（2026-10-30 生命周期闭环）。复用安装的会话/事件/SSE 基础
 * 设施（事件名同为 install-*，前端同一个 LogPanel 复用）。可能抛 InstallError：
 *  - 404 CLI_NOT_FOUND          未知 cliId
 *  - 422 UNINSTALL_NOT_EXECUTABLE  无 uninstall_spec（copy-only，提示 uninstall_cmd）
 *  - 409 INSTALL_IN_PROGRESS    该 CLI 已有进行中的安装/卸载
 */
export function startUninstall(cliId: string): InstallSession {
  const desc = findCli(cliId);
  if (!desc) {
    throw new InstallError(404, 'CLI_NOT_FOUND', `unknown cli id: ${cliId}`);
  }

  // 安装/卸载共用会话池 → 同一 CLI 不并发。
  for (const s of sessions.values()) {
    if (s.cli_id === cliId && (s.status === 'pending' || s.status === 'running')) {
      throw new InstallError(409, 'INSTALL_IN_PROGRESS', `operation already in progress for ${cliId}`);
    }
  }

  const spec: UninstallSpec | undefined = desc.uninstall_spec;
  if (!spec || !PM_WHITELIST.has(spec.pm)) {
    throw new InstallError(
      422,
      'UNINSTALL_NOT_EXECUTABLE',
      desc.uninstall_cmd
        ? `${cliId} 需手动卸载：${desc.uninstall_cmd}`
        : `${cliId} 无自动卸载方式（copy-only）`,
    );
  }

  const installId = `uninst-${randomUUID().slice(0, 12)}`;
  const command = [spec.pm, ...spec.args].join(' ');
  const session: InstallSession = {
    install_id: installId,
    cli_id: cliId,
    command,
    argv: [resolvePmBinary(spec.pm), ...spec.args],
    status: 'pending',
    started_at: new Date().toISOString(),
    events: [],
    _cancelRequested: false,
    _child: null,
    _stderrTail: '',
    finished: Promise.resolve(),
  };

  session.finished = spawnInternal(session);
  sessions.set(installId, session);
  ensureSweeper();
  return session;
}

export function getInstall(installId: string): InstallSession | undefined {
  return sessions.get(installId);
}

/**
 * 取消安装：SIGTERM → 5s → SIGKILL。返回 true = 会话存在且已发出终止。
 * 终态帧（install-error INSTALL_CANCELED）由 kill 链路的 close 回调派发。
 */
export function cancelInstall(installId: string): boolean {
  const s = sessions.get(installId);
  if (!s) return false;
  if (s.status !== 'pending' && s.status !== 'running') return true; // 已终态，幂等成功
  s._cancelRequested = true;
  s.status = 'canceled';
  const child = s._child;
  if (child) {
    try {
      child.kill('SIGTERM');
    } catch {
      /* ignore — child may already be dead */
    }
    const timer = setTimeout(() => {
      try {
        child.kill('SIGKILL');
      } catch {
        /* ignore */
      }
    }, 5000);
    if (typeof timer.unref === 'function') timer.unref();
  }
  s.events.push({
    event: 'install-error',
    data: {
      code: 'INSTALL_CANCELED',
      message: '安装已取消',
      stderr_tail: s._stderrTail.slice(-STDERR_TAIL_MAX),
    } satisfies InstallErrorPayload as unknown as Record<string, unknown>,
  });
  return true;
}

/** 测试辅助：清空全部会话。 */
export function __resetInstallerForTest(): void {
  for (const s of sessions.values()) {
    if (s.status === 'pending' || s.status === 'running') cancelInstall(s.install_id);
  }
  sessions.clear();
}
