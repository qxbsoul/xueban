/**
 * acp-detector.ts — Discover ACP / MCP remote agents (Story 15.23)
 *
 * Two probes per spec:
 *   1. PATH scan via `which`/`where` — does the binary exist?
 *   2. Optional TCP ping (5 s timeout) of well-known ACP HTTP endpoints
 *      (`localhost:8003` Hermes, `localhost:8004` ShadowSoul) — used purely
 *      as supplementary signal; the canonical entrypoint is still stdio.
 *
 * Cache shape mirrors `cli-detector.ts`. The same module also resolves
 * spawn commands for the ACP runner (`resolveAcpCommand(target)`).
 *
 * No long-lived connections are kept here — each detection spawns the
 * subprocess only long enough to verify it can be invoked, then kills.
 */

import { spawn } from 'node:child_process';
import { platform } from 'node:os';
import * as net from 'node:net';
import fs from 'node:fs/promises';
import path from 'node:path';

export interface AcpAgentSpec {
  /** Stable id used in `executor: acp:<id>`. */
  id: string;
  type: 'acp' | 'mcp';
  binary: string;
  args: string[];
  /** Optional HTTP port to ping in addition to PATH scan. */
  http_endpoint?: string;
  install_cmd?: string;
  /** Free-form capability hints surfaced in the UI. */
  capabilities?: string[];
}

export interface DetectedAgent extends AcpAgentSpec {
  installed: boolean;
  /** stdio (PATH) or http (port ping). */
  transport: 'stdio' | 'http' | 'unreachable';
  endpoint?: string;
  path: string | null;
  last_checked: string;
  error?: string;
}

export interface DetectAcpSnapshot {
  scanned_at: string;
  items: DetectedAgent[];
}

/**
 * Default registry — used when `.xueban/acp-agents.json` is absent.
 * These entries reflect Epic 2 deliverables (Hermes / ShadowSoul). They will
 * almost always be `installed=false` on a clean dev machine; that's expected
 * and not an error per AC4.
 */
const DEFAULT_REGISTRY: AcpAgentSpec[] = [
  {
    id: 'hermes',
    type: 'acp',
    binary: 'hermes',
    args: ['acp'],
    http_endpoint: 'http://localhost:8003',
    install_cmd: 'pip install -e xueban[hermes]',
    capabilities: ['tools', 'prompts'],
  },
  {
    id: 'shadowsoul',
    type: 'acp',
    binary: 'shadowsoul',
    args: ['acp'],
    http_endpoint: 'http://localhost:8004',
    install_cmd: 'pip install -e xueban',
    capabilities: ['tools'],
  },
];

let cache: DetectAcpSnapshot | null = null;

const PATH_TIMEOUT_MS = 3000;
const HTTP_PING_TIMEOUT_MS = 5000;

async function which(bin: string): Promise<string | null> {
  // Absolute / explicitly relative executable paths do not need a PATH scan.
  // Windows `where` also rejects some absolute paths, which previously made
  // valid custom ACP agents show up as EXECUTOR_NOT_INSTALLED.
  if (path.isAbsolute(bin) || bin.includes('/') || bin.includes('\\')) {
    const candidate = path.resolve(bin);
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      return null;
    }
  }

  return new Promise((resolve) => {
    const isWin = platform() === 'win32';
    const cmd = isWin ? 'where' : 'which';
    let child: ReturnType<typeof spawn>;
    try { child = spawn(cmd, [bin], { shell: false }); }
    catch { resolve(null); return; }
    let out = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return; settled = true;
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
      resolve(null);
    }, PATH_TIMEOUT_MS);
    if (typeof timer.unref === 'function') timer.unref();
    child.stdout?.on('data', (b) => { out += b.toString(); });
    child.on('close', (code) => {
      if (settled) return; settled = true;
      clearTimeout(timer);
      if (code === 0) {
        const first = out.split(/\r?\n/).map((s) => s.trim()).find((s) => s.length > 0);
        resolve(first ?? null);
      } else { resolve(null); }
    });
    child.on('error', () => { if (settled) return; settled = true; clearTimeout(timer); resolve(null); });
  });
}

/**
 * TCP-ping a host:port with a 5 s timeout. Returns true if connection
 * succeeded (port open). Used purely as supplementary "is it running?" hint.
 */
function pingTcp(host: string, port: number, timeoutMs = HTTP_PING_TIMEOUT_MS): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return; settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => finish(true));
    sock.once('timeout', () => finish(false));
    sock.once('error', () => finish(false));
    try { sock.connect(port, host); } catch { finish(false); }
  });
}

function parseHttpEndpoint(url: string): { host: string; port: number } | null {
  try {
    const u = new URL(url);
    const port = u.port ? parseInt(u.port, 10) : (u.protocol === 'https:' ? 443 : 80);
    return { host: u.hostname, port };
  } catch { return null; }
}

async function loadRegistry(): Promise<AcpAgentSpec[]> {
  const cfgPath = path.join(process.cwd(), '.xueban', 'acp-agents.json');
  try {
    const raw = await fs.readFile(cfgPath, 'utf8');
    const parsed = JSON.parse(raw) as { agents?: AcpAgentSpec[] };
    if (Array.isArray(parsed.agents) && parsed.agents.length > 0) return parsed.agents;
  } catch { /* fall through */ }
  return DEFAULT_REGISTRY;
}

async function detectOne(spec: AcpAgentSpec): Promise<DetectedAgent> {
  const now = new Date().toISOString();
  const found = await which(spec.binary);
  let httpOk = false;
  if (spec.http_endpoint) {
    const parsed = parseHttpEndpoint(spec.http_endpoint);
    if (parsed) httpOk = await pingTcp(parsed.host, parsed.port);
  }
  const installed = !!found || httpOk;
  let transport: DetectedAgent['transport'] = 'unreachable';
  if (found) transport = 'stdio';
  else if (httpOk) transport = 'http';
  return {
    ...spec,
    installed,
    transport,
    endpoint: httpOk ? spec.http_endpoint : undefined,
    path: found,
    last_checked: now,
    error: installed ? undefined : `binary "${spec.binary}" not on PATH${spec.http_endpoint ? ` and ${spec.http_endpoint} not reachable` : ''}`,
  };
}

export async function detectAcpAgents(force = true): Promise<DetectAcpSnapshot> {
  if (!force && cache) return cache;
  const specs = await loadRegistry();
  const items = await Promise.all(specs.map(detectOne));
  cache = { scanned_at: new Date().toISOString(), items };
  return cache;
}

export function getCachedAcpSnapshot(): DetectAcpSnapshot | null {
  return cache;
}

export function __resetAcpDetectCacheForTest(): void {
  cache = null;
}

/**
 * Resolve an `executor: acp:<target>` value to a spawn command.
 *
 * Supported forms:
 *   - `<id>`                 → look up registry entry by id
 *   - `custom?cmd=...&arg=...` → user-defined command (multi-arg supported)
 */
export async function resolveAcpCommand(target: string): Promise<{ command: string; args: string[]; id: string }> {
  if (target.startsWith('custom?')) {
    const params = new URLSearchParams(target.slice('custom?'.length));
    const command = params.get('cmd');
    const args = params.getAll('arg');
    if (!command) throw new Error('custom ACP target missing ?cmd= parameter');
    return { command, args, id: `custom:${command}` };
  }
  if (!cache) await detectAcpAgents(true);
  const found = cache?.items.find((a) => a.id === target && a.type === 'acp');
  if (!found) throw new Error(`ACP agent "${target}" not in registry`);
  if (!found.installed) {
    const err = new Error(
      `ACP agent "${target}" not installed. ` +
        (found.install_cmd ? `Install via: ${found.install_cmd}` : 'No install_cmd known.'),
    );
    (err as Error & { code?: string; install_cmd?: string }).code = 'EXECUTOR_NOT_INSTALLED';
    (err as Error & { code?: string; install_cmd?: string }).install_cmd = found.install_cmd;
    throw err;
  }
  return { command: found.binary, args: found.args, id: target };
}

/**
 * Resolve an `executor: mcp:<server>/<tool>` server portion to a spawn command.
 * Reads `.xueban/mcp.json` (if present) for user-defined servers, falling
 * back to a small built-in list keyed off Epic 11 names.
 */
export async function resolveMcpServer(name: string): Promise<{ command: string; args: string[] }> {
  const cfgPath = path.join(process.cwd(), '.xueban', 'mcp.json');
  try {
    const raw = await fs.readFile(cfgPath, 'utf8');
    const parsed = JSON.parse(raw) as { servers?: Array<{ name: string; command: string; args?: string[] }> };
    const found = (parsed.servers ?? []).find((s) => s.name === name);
    if (found) return { command: found.command, args: found.args ?? [] };
  } catch { /* fall through */ }
  // Final fallback: assume `name` itself is a binary on PATH.
  const onPath = await which(name);
  if (onPath) return { command: name, args: [] };
  const err = new Error(`MCP server "${name}" not in .xueban/mcp.json and "${name}" binary not on PATH`);
  (err as Error & { code?: string }).code = 'MCP_SERVER_NOT_FOUND';
  throw err;
}
