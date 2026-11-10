#!/usr/bin/env node
/**
 * Xueban 双后端统一启动器。
 *
 *   node scripts/dev.mjs --no-open
 *
 *   1. 拉起 Python FastAPI(:8000) + Node 网关(:8002)
 *   2. 两个服务都通过 HTTP 就绪探测后打印接口地址
 *   3. Ctrl+C 或任一进程退出 → 整棵进程树一起停,不留孤儿
 *
 * 端口被占用时启动前直接报错退出;Python 解释器自动探测
 * (SF_PYTHON 环境变量 > 根目录 .venv > 系统 python)。
 *
 * --no-open 仅为兼容旧命令保留；后端模式不会打开浏览器。
 * --no-python 可显式只起 Node 网关（Python 路由将返回 503）。
 */
import { spawn, execFile, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const IS_WIN = process.platform === 'win32';

const GW_PORT = 8002;
const PY_PORT = Number(process.env.XUEBAN_PYTHON_PORT ?? 8000);

const args = new Set(process.argv.slice(2));
const WITH_PYTHON = !args.has('--no-python');

const C = {
  py: '\x1b[34m', // blue
  gw: '\x1b[32m', // green
  green: '\x1b[32m',
  dim: '\x1b[2m',
  bold: '\x1b[1m',
  cyan: '\x1b[36m',
  yellow: '\x1b[33m',
  red: '\x1b[31m',
  reset: '\x1b[0m',
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── Python 解释器探测 ─────────────────────────────────────────────────────────
function resolvePython() {
  if (process.env.SF_PYTHON) return process.env.SF_PYTHON;
  const venvPy = IS_WIN
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(ROOT, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPy)) return venvPy;
  return 'python';
}

// ── 端口/HTTP 就绪探测 ────────────────────────────────────────────────────────
/**
 * 回环地址有两个协议族，服务可能只监听其中一个。
 * 同时探测 IPv4 与 IPv6，避免把已监听的地址误判为未就绪。
 */
const LOOPBACK_HOSTS = ['127.0.0.1', '::1'];

function loopbackUrl(host, port, pathname) {
  return `http://${host.includes(':') ? `[${host}]` : host}:${port}${pathname}`;
}

function portInUseOne(port, host) {
  return new Promise((resolve) => {
    const s = net.connect({ port, host });
    s.once('connect', () => { s.destroy(); resolve(true); });
    s.once('error', () => resolve(false));
  });
}

async function portInUse(port) {
  for (const host of LOOPBACK_HOSTS) if (await portInUseOne(port, host)) return true;
  return false;
}

function probe(url) {
  // 任何 HTTP 响应(含 404)都算"服务已监听"
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 2000 }, (res) => { res.resume(); resolve(true); });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitReady(port, pathname, label, timeoutMs = 120_000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    for (const host of LOOPBACK_HOSTS) {
      if (await probe(loopbackUrl(host, port, pathname))) return;
    }
    await sleep(400);
  }
  throw new Error(`${label} 在 ${timeoutMs / 1000}s 内未就绪:${pathname}（端口 ${port}）`);
}

// ── 子进程日志:按行分帧 + 彩色前缀 ──────────────────────────────────────────
function lineSplitter(onLine) {
  let buf = '';
  return (chunk) => {
    buf += chunk.toString();
    let idx;
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, '');
      buf = buf.slice(idx + 1);
      if (line.trim()) onLine(line);
    }
  };
}

function wireLogs(child, tag, color) {
  const push = (line) => process.stdout.write(`${color}[${tag}]${C.reset} ${line}\n`);
  const pushErr = (line) => process.stdout.write(`${color}[${tag}]${C.reset} ${C.yellow}${line}${C.reset}\n`);
  child.stdout?.on('data', lineSplitter(push));
  child.stderr?.on('data', lineSplitter(pushErr));
}

// ── 进程树管理 ────────────────────────────────────────────────────────────────
const children = [];
let shuttingDown = false;

// Windows 上 uvicorn --reload 的 worker / tsx 的 node 子进程会脱离 taskkill /T
// 的 PPID 快照(中间进程退出后链路断掉),所以退场分三段:
//   1. 对每个直接子进程 taskkill /T /F(大多数情况足够)
//   2. PowerShell 沿 PPID 递归清点所有后代,逐个补杀(抓断链孤儿)
//   3. netstat 按端口反查监听者兜底
function killDescendantsOnWin(rootPids) {
  const roots = rootPids.filter((p) => Number.isFinite(p)).join(',');
  if (!roots) return Promise.resolve();
  const script = [
    '$roots = @(' + roots + ')',
    '$procs = Get-CimInstance Win32_Process',
    '$byParent = @{}',
    'foreach ($p in $procs) { $byParent[[int]$p.ParentProcessId] += ,([int]$p.ProcessId) }',
    '$seen = @{}',
    '$stack = New-Object System.Collections.Stack',
    'foreach ($r in $roots) { $stack.Push($r) }',
    'while ($stack.Count -gt 0) {',
    '  $cur = $stack.Pop()',
    '  if ($seen.ContainsKey($cur)) { continue }',
    '  $seen[$cur] = $true',
    '  if ($byParent.ContainsKey($cur)) { foreach ($c in $byParent[$cur]) { $stack.Push($c) } }',
    '}',
    'foreach ($k in $seen.Keys) { if ($k -ne $PID) { & taskkill /PID $k /F 2>$null | Out-Null } }',
  ].join('\n');
  return new Promise((resolve) => {
    const ps = spawn('powershell', ['-NoProfile', '-Command', script], { stdio: 'ignore' });
    ps.once('close', resolve);
    ps.once('error', resolve);
  });
}

function killPortOwnersOnWin(ports) {
  return new Promise((resolve) => {
    execFile('netstat', ['-ano'], (err, stdout) => {
      if (!err && stdout) {
        for (const line of stdout.split(/\r?\n/)) {
          const cols = line.trim().split(/\s+/);
          if (cols.length === 5 && cols[0] === 'TCP' && cols[3] === 'LISTENING') {
            const port = Number(cols[1].split(':').pop());
            if (ports.includes(port)) {
              spawn('taskkill', ['/pid', cols[4], '/T', '/F'], { stdio: 'ignore' });
            }
          }
        }
      }
      resolve();
    });
  });
}

function teardown(exitCode) {
  if (shuttingDown) return;
  shuttingDown = true;
  const rootPids = children.map((c) => c.pid);
  for (const child of children) {
    if (child.exitCode !== null) continue;
    if (IS_WIN) {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
    }
  }
  const finish = () => process.exit(exitCode);
  if (IS_WIN) {
    const allPorts = WITH_PYTHON ? [GW_PORT, PY_PORT] : [GW_PORT];
    setTimeout(() => {
      killDescendantsOnWin(rootPids)
        .then(() => killPortOwnersOnWin(allPorts))
        .then(() => setTimeout(finish, 200));
    }, 250);
  } else {
    setTimeout(finish, 50);
  }
}

function launch(name, color, file, argv, opts = {}) {
  // POSIX 用独立进程组,组杀时能带走整棵树;Windows 直接 spawn exe/node 入口
  const child = spawn(file, argv, {
    cwd: opts.cwd ?? ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...opts.env },
    detached: !IS_WIN,
    shell: false,
  });
  children.push(child);
  wireLogs(child, name, color);
  child.on('exit', (code, signal) => {
    if (shuttingDown) return;
    process.stdout.write(`\n${C.red}✖ [${name}] 已退出 (code=${code} signal=${signal}) — 停止其余服务${C.reset}\n`);
    teardown(code ?? 1);
  });
  child.on('error', (err) => {
    process.stdout.write(`${C.red}✖ [${name}] 启动失败:${err.message}${C.reset}\n`);
    teardown(1);
  });
  return child;
}

// ── 主流程 ────────────────────────────────────────────────────────────────────
/** Python 是必需业务后端；缺依赖时明确失败，不静默退成半个产品。 */
function pythonUsable(pyBin) {
  try {
    execFileSync(pyBin, ['-c', 'import uvicorn, yaml, fastapi'], {
      stdio: 'ignore',
      timeout: 20_000,
    });
    return true;
  } catch {
    return false;
  }
}

async function main() {
  const pyBin = resolvePython();
  const withPython = WITH_PYTHON;
  if (withPython && !pythonUsable(pyBin)) {
    throw new Error(
      `Python 依赖不全，无法启动完整后端。解释器: ${pyBin}\n` +
        `  请先执行: python -m pip install -e ".[server]"\n` +
        `  仅需调试 Node 网关时，可显式执行: node scripts/dev.mjs --no-python`,
    );
  }

  console.log(`
${C.bold}  Xueban Backend Dev${C.reset}
  ────────────────────────────────────────────
  ${C.cyan}Node 网关${C.reset}  http://localhost:${GW_PORT}/health
  ${withPython ? `${C.cyan}Python API${C.reset}  http://localhost:${PY_PORT}/docs` : `${C.dim}Python API  已显式跳过${C.reset}`}
  ${C.dim}Ctrl+C 停止全部服务${C.reset}
`);

  // 1) 端口预检：被占用直接报错，避免和已有实例冲突。
  const need = [
    { port: GW_PORT, name: 'Node 网关' },
    ...(withPython ? [{ port: PY_PORT, name: 'Python FastAPI' }] : []),
  ];
  const busy = [];
  for (const n of need) if (await portInUse(n.port)) busy.push(n);
  if (busy.length) {
    console.error(`${C.red}✖ 端口被占用，请先停掉已有实例：${C.reset}`);
    for (const n of busy) console.error(`    ${C.yellow}:${n.port}  ${n.name}${C.reset}`);
    process.exit(1);
  }

  // 2) 直连两个后端入口，不经过 npm 壳，便于完整清理进程树。
  if (withPython) {
    launch('py', C.py, pyBin, [
      '-m', 'uvicorn', 'xueban.server:app',
      '--host', '127.0.0.1', '--port', String(PY_PORT), '--reload',
    ]);
    console.log(`${C.dim}  [py] ${pyBin}${C.reset}`);
  }
  launch('gw', C.gw, process.execPath, [
    path.join('node_modules', 'tsx', 'dist', 'cli.mjs'),
    'watch', 'src/index.ts',
  ], { cwd: path.join(ROOT, 'server') });
  console.log(`${C.dim}  [gw] tsx watch server/src/index.ts${C.reset}\n`);

  // 3) 两个后端都通过 HTTP 探测后才报告就绪。
  const waiting = [
    waitReady(GW_PORT, '/health', 'Node 网关').then(() => console.log(`${C.green}✓${C.reset} 网关就绪 :${GW_PORT}`)),
    ...(withPython
      ? [waitReady(PY_PORT, '/docs', 'Python FastAPI').then(() => console.log(`${C.green}✓${C.reset} Python 就绪 :${PY_PORT}`))]
      : []),
  ];
  try {
    await Promise.all(waiting);
  } catch (err) {
    console.error(`\n${C.red}✖ ${err.message}${C.reset}`);
    teardown(1);
    return;
  }

  console.log(
    `\n${C.green}✓${C.reset} 后端全部就绪\n` +
      `  新前端统一入口: ${C.bold}http://localhost:${GW_PORT}${C.reset}\n` +
      `  Python OpenAPI:  ${C.bold}http://localhost:${PY_PORT}/docs${C.reset}`,
  );
}

process.on('SIGINT', () => { console.log('\n正在停止全部服务…'); teardown(0); });
process.on('SIGTERM', () => teardown(0));

main().catch((err) => {
  console.error(`${C.red}✖ ${err?.stack || err}${C.reset}`);
  teardown(1);
});
