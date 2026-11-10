/**
 * acp-detector.test.ts — Real PATH scan + TCP ping (Story 15.23)
 *
 * Run from server/:  npx tsx src/acp-detector.test.ts
 *
 * On a clean dev machine, hermes/shadowsoul almost certainly are NOT on PATH,
 * and ports 8003/8004 are NOT bound — so we expect installed=false. That
 * itself is a successful detection per AC4 ("不抛错，不阻塞 server 启动").
 *
 * We additionally exercise:
 *   - resolveAcpCommand('custom?cmd=node&arg=--version') round-trip
 *   - resolveAcpCommand('does-not-exist') → throws ACP_UNREACHABLE-class
 *   - port ping shape: spin up a temporary TCP server on a random port,
 *     write a custom registry into a tmpdir cwd, detect, expect installed.
 */

import { detectAcpAgents, resolveAcpCommand, __resetAcpDetectCacheForTest } from './acp-detector';
import * as net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); if (detail !== undefined) console.log('        detail:', detail); }
}

async function main() {
  console.log('\n── Real default-registry detection (hermes / shadowsoul) ──');
  __resetAcpDetectCacheForTest();
  const snap = await detectAcpAgents(true);
  check('snapshot has scanned_at ISO string', typeof snap.scanned_at === 'string' && snap.scanned_at.includes('T'), snap.scanned_at);
  check('snapshot includes hermes', snap.items.some((i) => i.id === 'hermes'), snap.items.map((i) => i.id));
  check('snapshot includes shadowsoul', snap.items.some((i) => i.id === 'shadowsoul'), snap.items.map((i) => i.id));
  for (const a of snap.items) {
    console.log(`        agent=${a.id}  installed=${a.installed}  transport=${a.transport}  path=${a.path ?? '-'}${a.error ? `  err="${a.error}"` : ''}`);
    check(`${a.id}: last_checked is ISO`, typeof a.last_checked === 'string' && a.last_checked.includes('T'));
  }

  console.log('\n── resolveAcpCommand custom? form ──');
  {
    const r = await resolveAcpCommand('custom?cmd=node&arg=--version');
    check('custom resolves cmd', r.command === 'node', r);
    check('custom resolves args', r.args.length === 1 && r.args[0] === '--version', r.args);
  }

  console.log('\n── resolveAcpCommand unknown id throws ──');
  {
    let threw = false;
    try { await resolveAcpCommand('does-not-exist-12345'); }
    catch (err) { threw = true; check('error message mentions registry', /not in registry/.test((err as Error).message), (err as Error).message); }
    check('unknown id rejected', threw);
  }

  console.log('\n── TCP ping detection via temp listener ──');
  {
    // Spin up a TCP listener so the http_endpoint port-ping fires positive.
    const srv = net.createServer();
    await new Promise<void>((r) => srv.listen(0, '127.0.0.1', r));
    const addr = srv.address();
    if (!addr || typeof addr === 'string') {
      check('temp listener bound', false);
    } else {
      const port = addr.port;
      const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-acp-test-'));
      fs.mkdirSync(path.join(tmpDir, '.xueban'), { recursive: true });
      const cfg = {
        agents: [
          {
            id: 'test-tcp-agent',
            type: 'acp',
            binary: 'this-binary-definitely-does-not-exist-xyz',
            args: [],
            http_endpoint: `http://127.0.0.1:${port}`,
            install_cmd: 'fake',
            capabilities: ['tools'],
          },
        ],
      };
      fs.writeFileSync(path.join(tmpDir, '.xueban', 'acp-agents.json'), JSON.stringify(cfg));
      const oldCwd = process.cwd();
      process.chdir(tmpDir);
      try {
        __resetAcpDetectCacheForTest();
        const s = await detectAcpAgents(true);
        const tcpAgent = s.items.find((a) => a.id === 'test-tcp-agent');
        check('custom registry loaded', !!tcpAgent, s.items.map((a) => a.id));
        check('TCP ping marked installed=true', tcpAgent?.installed === true, tcpAgent);
        check('transport=http', tcpAgent?.transport === 'http', tcpAgent?.transport);
      } finally {
        process.chdir(oldCwd);
        srv.close();
      }
    }
  }

  console.log(`\nDone: ${pass} passed, ${fail} failed.`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => { console.error(err); process.exit(1); });
