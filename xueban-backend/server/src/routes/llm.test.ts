/**
 * routes/llm.test.ts — Part D — protocol entrypoint tests.
 *
 * Spins up a one-off express app with `routes/llm.ts` mounted, then
 * exercises the two endpoints (Anthropic + OpenAI shape) via http.request —
 * no superagent dep. We deliberately do NOT set any provider env keys, so
 * every real-LLM path returns `NO_API_KEY` — perfect for asserting the
 * protocol-conversion logic without hitting the network.
 */

import express from 'express';
import http from 'http';
import llmRouter, { __pickProviderFromModel } from './llm';

let failed = 0;
function assert(cond: unknown, msg: string): void {
  if (!cond) {
    console.error(`  FAIL: ${msg}`);
    failed += 1;
  } else {
    console.log(`  ok: ${msg}`);
  }
}

interface HttpResp {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: unknown;
  raw: string;
}

function postJson(
  port: number,
  urlPath: string,
  body: Record<string, unknown>,
  extraHeaders: Record<string, string> = {},
): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        method: 'POST',
        path: urlPath,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data),
          ...extraHeaders,
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf-8');
          let parsed: unknown = null;
          try {
            parsed = JSON.parse(text);
          } catch {
            parsed = text;
          }
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: parsed,
            raw: text,
          });
        });
      },
    );
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

function getReq(port: number, urlPath: string): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, method: 'GET', path: urlPath },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf-8');
          let parsed: unknown = null;
          try {
            parsed = JSON.parse(text);
          } catch {
            parsed = text;
          }
          resolve({ status: res.statusCode ?? 0, headers: res.headers, body: parsed, raw: text });
        });
      },
    );
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  // CRITICAL: clear API keys so the NO_API_KEY path runs deterministically
  // even on a dev machine that has them set globally.
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.OPENAI_API_KEY;
  delete process.env.DEEPSEEK_API_KEY;
  delete process.env.ZHIPU_API_KEY;

  const app = express();
  app.use(express.json({ limit: '1mb' }));
  app.use('/api/llm', llmRouter);
  const server = app.listen(0);
  await new Promise<void>((r) => server.on('listening', () => r()));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;

  try {
    // ── Test 1 — pickProviderFromModel mapping (pure unit) ──────────────────
    console.log('--- pickProviderFromModel: model→provider mapping ---');
    {
      assert(__pickProviderFromModel('claude-sonnet-4-6', 'openai') === 'anthropic',
        'claude-* → anthropic');
      assert(__pickProviderFromModel('gpt-4o', 'anthropic') === 'openai',
        'gpt-4o → openai');
      assert(__pickProviderFromModel('o1-preview', 'anthropic') === 'openai',
        'o1-* → openai');
      assert(__pickProviderFromModel('deepseek-chat', 'openai') === 'deepseek',
        'deepseek-* → deepseek');
      assert(__pickProviderFromModel('glm-4-plus', 'openai') === 'zhipu',
        'glm-* → zhipu');
      assert(__pickProviderFromModel('zhipu-x', 'openai') === 'zhipu',
        'zhipu-* → zhipu');
      assert(__pickProviderFromModel('unknown-model', 'anthropic') === 'anthropic',
        'unknown → anthropic client default');
      assert(__pickProviderFromModel('unknown-model', 'openai') === 'openai',
        'unknown → openai client default');
      assert(__pickProviderFromModel(undefined, 'anthropic') === 'anthropic',
        'undefined model → client default');
    }

    // ── Test 2 — Anthropic /messages NO_API_KEY (non-stream, JSON shape) ────
    console.log('--- POST /api/llm/messages (no key, non-stream) → 401 ---');
    {
      const r = await postJson(port, '/api/llm/messages', {
        model: 'claude-sonnet-4-6',
        max_tokens: 100,
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 401, `status=401 (got ${r.status})`);
      const body = r.body as { type?: string; error?: { type?: string; provider?: string } };
      assert(body.type === 'error', 'top-level type="error"');
      assert(body.error?.type === 'NO_API_KEY', `error.type=NO_API_KEY (got ${body.error?.type})`);
      assert(body.error?.provider === 'anthropic', `error.provider=anthropic`);
    }

    // ── Test 3 — Anthropic /messages NO_API_KEY (stream, SSE) ──────────────
    console.log('--- POST /api/llm/messages (no key, stream:true) → SSE 200 ---');
    {
      const r = await postJson(port, '/api/llm/messages', {
        model: 'claude-sonnet-4-6',
        max_tokens: 100,
        stream: true,
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 200, `status=200 (SSE; got ${r.status})`);
      const ct = String(r.headers['content-type'] ?? '');
      assert(ct.includes('text/event-stream'), `content-type SSE (got ${ct})`);
      assert(r.raw.includes('event: message_start'), 'has message_start event');
      assert(r.raw.includes('event: content_block_start'), 'has content_block_start');
      assert(r.raw.includes('event: error'), 'has error event for NO_API_KEY');
      assert(r.raw.includes('NO_API_KEY'), 'raw contains NO_API_KEY');
      assert(r.raw.includes('event: message_stop'), 'has terminal message_stop');
    }

    // ── Test 4 — OpenAI /chat/completions NO_API_KEY (non-stream) ──────────
    console.log('--- POST /api/llm/chat/completions (no key, non-stream) → 401 ---');
    {
      const r = await postJson(port, '/api/llm/chat/completions', {
        model: 'gpt-4o',
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 401, `status=401 (got ${r.status})`);
      const body = r.body as { error?: { code?: string; provider?: string } };
      assert(body.error?.code === 'NO_API_KEY', `error.code=NO_API_KEY (got ${body.error?.code})`);
      assert(body.error?.provider === 'openai', `error.provider=openai`);
    }

    // ── Test 5 — OpenAI /chat/completions NO_API_KEY (stream, SSE) ──────────
    console.log('--- POST /api/llm/chat/completions (no key, stream:true) → SSE 200 ---');
    {
      const r = await postJson(port, '/api/llm/chat/completions', {
        model: 'gpt-4o',
        stream: true,
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 200, `status=200 (SSE; got ${r.status})`);
      const ct = String(r.headers['content-type'] ?? '');
      assert(ct.includes('text/event-stream'), `content-type SSE (got ${ct})`);
      assert(r.raw.includes('"object":"chat.completion.chunk"'), 'has chunk objects');
      assert(r.raw.includes('NO_API_KEY'), 'raw contains NO_API_KEY in error chunk');
      assert(r.raw.includes('data: [DONE]'), 'has terminal [DONE]');
    }

    // ── Test 6 — model prefix → provider routing via real endpoint ─────────
    console.log('--- POST /chat/completions with deepseek model → deepseek provider error ---');
    {
      const r = await postJson(port, '/api/llm/chat/completions', {
        model: 'deepseek-chat',
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 401, `status=401 (got ${r.status})`);
      const body = r.body as { error?: { code?: string; provider?: string } };
      assert(body.error?.code === 'NO_API_KEY', 'NO_API_KEY');
      assert(body.error?.provider === 'deepseek',
        `provider routed to deepseek (got ${body.error?.provider})`);
    }

    // ── Test 7 — claude-* model on OpenAI endpoint routes to anthropic ─────
    console.log('--- POST /chat/completions with claude model → anthropic provider ---');
    {
      const r = await postJson(port, '/api/llm/chat/completions', {
        model: 'claude-sonnet-4-6',
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 401, `status=401`);
      const body = r.body as { error?: { provider?: string } };
      assert(body.error?.provider === 'anthropic',
        `provider routed to anthropic (got ${body.error?.provider})`);
    }

    // ── Test 8 — 400 missing model ──────────────────────────────────────────
    console.log('--- POST /messages missing model → 400 ---');
    {
      const r = await postJson(port, '/api/llm/messages', {
        max_tokens: 100,
        messages: [{ role: 'user', content: 'hi' }],
      });
      assert(r.status === 400, `status=400 (got ${r.status})`);
      const body = r.body as { error?: { message?: string } };
      assert(body.error?.message?.includes('model'), 'error mentions model');
    }

    // ── Test 9 — 400 missing messages on /chat/completions ─────────────────
    console.log('--- POST /chat/completions missing messages → 400 ---');
    {
      const r = await postJson(port, '/api/llm/chat/completions', {
        model: 'gpt-4o',
      });
      assert(r.status === 400, `status=400 (got ${r.status})`);
      const body = r.body as { error?: { code?: string } };
      assert(body.error?.code === 'invalid_request', 'code=invalid_request');
    }

    // ── Test 10 — 400 empty messages array on /messages ────────────────────
    console.log('--- POST /messages empty messages array → 400 ---');
    {
      const r = await postJson(port, '/api/llm/messages', {
        model: 'claude-sonnet-4-6',
        messages: [],
      });
      assert(r.status === 400, `status=400 (got ${r.status})`);
    }

    // ── Test 11 — GET /messages → 405 ───────────────────────────────────────
    console.log('--- GET /api/llm/messages → 405 ---');
    {
      const r = await getReq(port, '/api/llm/messages');
      assert(r.status === 405, `status=405 (got ${r.status})`);
    }

    // ── Test 12 — Authorization: Bearer header accepted on /messages ───────
    console.log('--- POST /messages with Bearer header → still NO_API_KEY path picks key correctly ---');
    {
      // We use a fake key — provider will hit AUTH_FAILED or NETWORK_ERROR
      // (network attempt happens). To keep the test offline we just verify
      // the request was accepted and didn't return NO_API_KEY (i.e. the
      // header parser found the key). Status will be 401/502/500.
      const r = await postJson(
        port,
        '/api/llm/messages',
        {
          model: 'claude-sonnet-4-6',
          max_tokens: 50,
          messages: [{ role: 'user', content: 'ping' }],
        },
        { Authorization: 'Bearer sk-ant-fake-key-not-real' },
      );
      const body = r.body as { error?: { type?: string } };
      // Whatever status — must NOT be NO_API_KEY (we *did* supply a key).
      assert(body.error?.type !== 'NO_API_KEY',
        `key from Authorization header was extracted (got error.type=${body.error?.type})`);
    }
  } finally {
    server.close();
  }

  console.log('---');
  if (failed > 0) {
    console.error(`FAILED: ${failed} assertion(s)`);
    process.exit(1);
  } else {
    console.log('PASS');
  }
}

main().catch((err) => {
  console.error('Test runner crashed:', err);
  process.exit(1);
});
