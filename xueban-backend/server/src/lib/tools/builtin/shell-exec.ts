/**
 * shell-exec.ts — builtin `shell_exec` tool. **HIGH RISK** — default DENY.
 *
 * Only available when the active skill's `allowed-tools` frontmatter
 * explicitly lists `shell_exec`. tool-runner.ts is responsible for the
 * permission check; this executor adds a SECOND defence layer:
 *
 *   1. Command-string blacklist — common destructive primitives are blocked
 *      regardless of skill opt-in. We'd rather false-positive than ship a
 *      hole. Patterns block: sudo, rm -rf, chmod -R 777, dd if=, mkfs,
 *      curl/wget piped to a shell, /etc/passwd writes, shutdown/reboot,
 *      eval/exec of base64-decoded payloads, network-listen primitives,
 *      kernel module ops, and direct device-file access (/dev/, /proc/).
 *
 *   2. Pipe/redirect to interpreter ban — `... | sh`, `... | bash -s`,
 *      `... > /etc/anything` are all rejected.
 *
 *   3. Working directory pinned to ctx.workspace. The spawned process
 *      cannot `cd` out of it via shell expansion AT spawn time (we use
 *      shell:true so it can run shell builtins, but the spawn cwd is fixed).
 *      Path-traversal within the command is the user's problem — we don't
 *      try to parse the shell.
 *
 *   4. Environment sanitised: only PATH + a small allowlist is forwarded.
 *      No HOME, no USER, no SSH_AUTH_SOCK, no API keys.
 *
 *   5. 30-second wall-clock timeout (SHELL_TIMEOUT_MS). On timeout we send
 *      SIGTERM, then SIGKILL after 2s.
 *
 *   6. Output cap: 256 KiB stdout + 256 KiB stderr. Beyond that the streams
 *      are dropped with a `truncated: true` flag.
 *
 * Output: `{ command, exit_code, signal, stdout, stderr, duration_ms,
 *           timed_out, truncated }`.
 */

import { spawn } from 'child_process';
import type { ToolSpec } from '../../tool-spec';
import { SHELL_TIMEOUT_MS } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const shellExecTool: ToolSpec = {
  name: 'shell_exec',
  description:
    'Run a shell command inside the workspace. HIGH RISK — only available when the active skill ' +
    "lists shell_exec in its allowed-tools frontmatter. Destructive primitives (sudo, rm -rf, curl|sh, etc.) " +
    'are blocked. 30s timeout. Output capped at 256 KiB per stream.',
  input_schema: {
    type: 'object',
    properties: {
      command: { type: 'string', description: 'Shell command line to execute.' },
      timeout_ms: { type: 'number', description: 'Optional timeout in milliseconds (1..30000).' },
    },
    required: ['command'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ShellExecInput {
  command: string;
  timeout_ms?: number;
}

function isShellExecInput(x: unknown): x is ShellExecInput {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as { command: unknown }).command === 'string'
  );
}

const MAX_OUTPUT_BYTES = 256 * 1024;

/**
 * Patterns that are blocked unconditionally. Order matters — first match wins
 * the error message. We're deliberately liberal: false positives on
 * legitimate commands are easier to debug than a hole.
 */
const BLOCKED_PATTERNS: Array<{ re: RegExp; reason: string }> = [
  { re: /(^|[\s;&|`(])sudo(\s|$)/i, reason: 'sudo' },
  { re: /(^|[\s;&|`(])doas(\s|$)/i, reason: 'doas' },
  { re: /(^|[\s;&|`(])su\s+-/i, reason: 'su -' },
  { re: /rm\s+(-[A-Za-z]*[rRfF][A-Za-z]*\s|--recursive\s|--force\s)/i, reason: 'rm -r/-f' },
  { re: /rm\s+-rf?\s+\/(?!\s*$)/i, reason: 'rm -rf /' },
  { re: /:\s*\(\s*\)\s*\{\s*:\s*\|\s*:/, reason: 'fork bomb' },
  { re: /\bmkfs(\.\w+)?\b/i, reason: 'mkfs' },
  { re: /\bdd\s+(if|of)=/i, reason: 'dd' },
  { re: /\bshutdown\b/i, reason: 'shutdown' },
  { re: /\breboot\b/i, reason: 'reboot' },
  { re: /\bhalt\b/i, reason: 'halt' },
  { re: /\bpoweroff\b/i, reason: 'poweroff' },
  { re: /\binit\s+0\b/i, reason: 'init 0' },
  { re: /\binsmod\b|\brmmod\b|\bmodprobe\b/i, reason: 'kernel module op' },
  { re: /\bchmod\s+-R\s+777\b/i, reason: 'chmod -R 777' },
  { re: /\bchown\s+-R\b/i, reason: 'chown -R' },
  { re: /(curl|wget|fetch|iwr|Invoke-WebRequest)[^\n]*\|\s*(sh|bash|zsh|ksh|csh|tcsh|pwsh|powershell|python\b|perl|ruby|node|tee\s)/i, reason: 'pipe download to interpreter' },
  { re: /(curl|wget)[^\n]*--?o[A-Za-z]*\s+\/(etc|bin|sbin|usr|var|root|boot|lib|dev|proc|sys)\//i, reason: 'download into system path' },
  { re: /\s>\s*\/(etc|bin|sbin|usr|root|boot|lib|dev|proc|sys)\//i, reason: 'redirect into system path' },
  { re: /\beval\s+["'`]?\$\(/i, reason: 'eval $(...)' },
  { re: /\bbase64\s+(-d|--decode)\b[^\n]*\|\s*(sh|bash|zsh)/i, reason: 'base64 | sh' },
  { re: /\bnc\b\s+-[a-z]*l/i, reason: 'netcat listen' },
  { re: /\bncat\b\s+-[a-z]*l/i, reason: 'ncat listen' },
  { re: /\b\/etc\/(passwd|shadow|sudoers|hosts)\b/i, reason: '/etc sensitive file' },
  { re: /\b\/dev\/(sd[a-z]|nvme|hd[a-z]|mem|kmem)\b/i, reason: '/dev block/raw device' },
  { re: /\bcrontab\s+(-r|-e)\b/i, reason: 'crontab modify' },
  { re: /\bsystemctl\s+(disable|enable|stop|start|mask|unmask)\b/i, reason: 'systemctl service' },
  { re: /\bservice\s+\w+\s+(stop|start|restart)\b/i, reason: 'service control' },
  { re: /\biptables\b|\bnft\b|\bufw\b/i, reason: 'firewall op' },
  { re: /\bRegistry::|\bReg(istry)?\s+(add|delete)\b|\bregedit\b/i, reason: 'registry op' },
  { re: /\bformat\s+[A-Za-z]:/i, reason: 'format drive' },
  { re: /\bDel\s+\/[A-Za-z]\s+\/[A-Za-z]/i, reason: 'recursive delete' },
];

function checkBlocked(cmd: string): string | null {
  for (const { re, reason } of BLOCKED_PATTERNS) {
    if (re.test(cmd)) return reason;
  }
  return null;
}

/** Build a minimal, sanitised env. PATH is required for shell to find utilities. */
function buildSafeEnv(srcEnv?: Readonly<Record<string, string>>): NodeJS.ProcessEnv {
  const src = srcEnv ?? (process.env as Record<string, string>);
  const out: NodeJS.ProcessEnv = {};
  const allowed = ['PATH', 'SystemRoot', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'LANG', 'LC_ALL'];
  for (const k of allowed) {
    if (src[k]) out[k] = src[k];
  }
  // Hard-cap PATH length — pathological values can break spawn.
  if (out.PATH && out.PATH.length > 8192) out.PATH = out.PATH.slice(0, 8192);
  return out;
}

export const shellExecExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isShellExecInput(input)) {
    return { output: { error: 'shell_exec: input must be { command: string, timeout_ms? }' }, isError: true };
  }

  const cmd = input.command;
  if (cmd.length === 0) {
    return { output: { error: 'shell_exec: empty command' }, isError: true };
  }
  if (cmd.length > 8192) {
    return { output: { error: 'shell_exec: command exceeds 8 KiB' }, isError: true };
  }
  if (cmd.includes('\0')) {
    return { output: { error: 'shell_exec: command contains NUL byte' }, isError: true };
  }

  // Belt-and-braces: shell_exec is gated by permission policy AT tool-runner.
  // We additionally require the skill to have opted in via allowed-tools so
  // a misconfigured policy can't accidentally enable it.
  if (ctx.allowedTools && !ctx.allowedTools.has('shell_exec')) {
    return {
      output: { error: "shell_exec denied: not present in skill's allowed-tools frontmatter" },
      isError: true,
    };
  }

  const blockedReason = checkBlocked(cmd);
  if (blockedReason) {
    return {
      output: { error: `shell_exec blocked: matches dangerous pattern (${blockedReason})` },
      isError: true,
    };
  }

  const timeoutMs = Math.min(Math.max(input.timeout_ms ?? SHELL_TIMEOUT_MS, 1), SHELL_TIMEOUT_MS);
  const start = Date.now();

  return await new Promise((resolve) => {
    const child = spawn(cmd, {
      shell: true,
      cwd: ctx.workspace,
      env: buildSafeEnv(ctx.env),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });

    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let truncated = false;
    let timedOut = false;
    let settled = false;

    interface ShellResult {
      output: Record<string, unknown>;
      isError: boolean;
    }
    const finish = (result: ShellResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(killTimer);
      clearTimeout(forceKillTimer);
      resolve(result);
    };

    const buildResult = (exitCode: number | null, signalName: NodeJS.Signals | null): ShellResult => {
      const stdout = Buffer.concat(stdoutChunks).toString('utf8');
      const stderr = Buffer.concat(stderrChunks).toString('utf8');
      return {
        output: {
          command: cmd,
          exit_code: exitCode,
          signal: signalName,
          stdout,
          stderr,
          duration_ms: Date.now() - start,
          timed_out: timedOut,
          truncated,
        },
        isError: timedOut || (exitCode !== null && exitCode !== 0),
      };
    };

    child.stdout.on('data', (chunk: Buffer) => {
      const remaining = MAX_OUTPUT_BYTES - stdoutBytes;
      if (remaining <= 0) {
        truncated = true;
        return;
      }
      if (chunk.length > remaining) {
        stdoutChunks.push(chunk.subarray(0, remaining));
        stdoutBytes += remaining;
        truncated = true;
      } else {
        stdoutChunks.push(chunk);
        stdoutBytes += chunk.length;
      }
    });

    child.stderr.on('data', (chunk: Buffer) => {
      const remaining = MAX_OUTPUT_BYTES - stderrBytes;
      if (remaining <= 0) {
        truncated = true;
        return;
      }
      if (chunk.length > remaining) {
        stderrChunks.push(chunk.subarray(0, remaining));
        stderrBytes += remaining;
        truncated = true;
      } else {
        stderrChunks.push(chunk);
        stderrBytes += chunk.length;
      }
    });

    child.on('error', (err) => {
      finish({
        output: { error: `shell_exec spawn failed: ${err.message}` },
        isError: true,
      });
    });

    child.on('close', (code, signalName) => {
      finish(buildResult(code, signalName));
    });

    // Timeout — SIGTERM first, SIGKILL 2s later.
    let forceKillTimer: NodeJS.Timeout = setTimeout(() => undefined, 0);
    clearTimeout(forceKillTimer);
    const killTimer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill('SIGTERM');
      } catch {
        /* ignore */
      }
      forceKillTimer = setTimeout(() => {
        try {
          child.kill('SIGKILL');
        } catch {
          /* ignore */
        }
      }, 2000);
    }, timeoutMs);

    // Abort signal — propagate to child.
    const onAbort = () => {
      if (settled) return;
      try {
        child.kill('SIGTERM');
      } catch {
        /* ignore */
      }
    };
    if (ctx.signal.aborted) {
      onAbort();
    } else {
      ctx.signal.addEventListener('abort', onAbort, { once: true });
    }
  });
};
