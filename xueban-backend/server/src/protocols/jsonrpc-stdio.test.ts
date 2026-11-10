/**
 * jsonrpc-stdio.test.ts — Real round-trip tests (Story 15.23)
 *
 * Run from server/:  npx tsx src/protocols/jsonrpc-stdio.test.ts
 *
 * Three scenarios:
 *   1. Pure framing: encode/decode a multi-frame buffer, including a frame
 *      split across a chunk boundary.
 *   2. In-process round-trip: a PassThrough-pair simulates the wire — we
 *      manually echo a response after observing the request frame.
 *   3. **Real subprocess round-trip**: spawn `node __mock_acp_server.js`,
 *      send `initialize`, assert protocolVersion=1; send `session/new` then
 *      `session/prompt`, observe ≥3 notifications + response; close.
 */

import { spawn } from 'node:child_process';
import { PassThrough } from 'node:stream';
import path from 'node:path';
import {
  FrameDecoder,
  JsonRpcStdioTransport,
  encodeFrame,
} from './jsonrpc-stdio';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); if (detail !== undefined) console.log('        detail:', detail); }
}

const MOCK_ACP = path.resolve(__dirname, '..', 'transport', 'spawners', '__fixtures__', '__mock_acp_server.js');

async function main() {
  // ─── 1. FrameDecoder unit (pure framing) ─────────────────────────────────
  console.log('\n── FrameDecoder framing ──');
  {
    const dec = new FrameDecoder();
    const f1 = encodeFrame({ jsonrpc: '2.0', id: 1, method: 'a' });
    const f2 = encodeFrame({ jsonrpc: '2.0', id: 2, method: 'b' });
    // Push split: header of f2 split mid-byte
    const combined = Buffer.concat([f1, f2]);
    const splitAt = f1.length + 6;
    dec.push(combined.slice(0, splitAt));
    const partial = dec.drain();
    check('decoder yields 1 frame after partial second push', partial.length === 1, partial);
    dec.push(combined.slice(splitAt));
    const rest = dec.drain();
    check('decoder yields 2nd frame after rest', rest.length === 1, rest);
    check('1st frame parsed correctly', (partial[0] as any)?.method === 'a', partial[0]);
    check('2nd frame parsed correctly', (rest[0] as any)?.method === 'b', rest[0]);
  }

  // ─── 2. In-process round-trip via PassThrough ────────────────────────────
  console.log('\n── In-process PassThrough round-trip ──');
  {
    const clientToServer = new PassThrough();
    const serverToClient = new PassThrough();
    const transport = JsonRpcStdioTransport.fromStreams(clientToServer, serverToClient);

    // Server side: read request frame, write response.
    const serverDecoder = new FrameDecoder();
    clientToServer.on('data', (chunk: Buffer) => {
      serverDecoder.push(chunk);
      for (const m of serverDecoder.drain()) {
        const msg = m as { id?: string | number; method?: string };
        if (msg.method === 'ping' && msg.id !== undefined) {
          serverToClient.write(encodeFrame({ jsonrpc: '2.0', id: msg.id, result: { pong: true, echo: 'hello' } }));
        }
      }
    });

    const result = await transport.request<{ pong: boolean; echo: string }>('ping', {}, 2000);
    check('round-trip returned pong=true', result.pong === true, result);
    check('round-trip returned echo=hello', result.echo === 'hello', result);

    // Notification path
    let gotNote = false;
    transport.onNotification((n) => { if (n.method === 'event/x') gotNote = true; });
    serverToClient.write(encodeFrame({ jsonrpc: '2.0', method: 'event/x', params: { v: 1 } }));
    await new Promise((r) => setTimeout(r, 50));
    check('notification handler fired', gotNote);

    await transport.close(false);
  }

  // ─── 3. Real subprocess round-trip (spawn node mock-acp) ─────────────────
  console.log('\n── Real subprocess round-trip (node __mock_acp_server.js) ──');
  {
    const transport = JsonRpcStdioTransport.fromCommand(process.execPath, [MOCK_ACP]);
    try {
      const init = await transport.request<{ protocolVersion: number; agentCapabilities?: any }>(
        'initialize',
        { protocolVersion: 1, clientInfo: { name: 'sf-test', version: '0' } },
        3000,
      );
      check('initialize returned protocolVersion=1', init.protocolVersion === 1, init);
      check('initialize advertised agentCapabilities.tools', init.agentCapabilities?.tools === true, init);

      const sess = await transport.request<{ sessionId: string }>('session/new', { cwd: process.cwd() }, 3000);
      check('session/new returned sessionId', typeof sess.sessionId === 'string' && sess.sessionId.length > 0, sess);

      const updates: any[] = [];
      transport.onNotification((n) => {
        if (n.method === 'session/update') updates.push(n.params);
      });

      const promptRes = await transport.request<{ stopReason: string }>(
        'session/prompt',
        { sessionId: sess.sessionId, prompt: [{ type: 'text', text: 'hi' }] },
        5000,
      );
      check('session/prompt resolved with stopReason', typeof promptRes.stopReason === 'string', promptRes);
      check('received ≥3 session/update chunks', updates.length >= 3, updates.length);
      const sample = JSON.stringify(updates[0]);
      console.log(`        sample-frame: ${sample}`);
    } finally {
      await transport.close(true);
    }
  }

  console.log(`\nDone: ${pass} passed, ${fail} failed.`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => { console.error(err); process.exit(1); });
