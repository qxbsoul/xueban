/**
 * routes/llm.ts — Part D — LLM protocol entrypoints (CliGate)
 *
 * Two OpenAI / Anthropic protocol-compatible endpoints that let external
 * tools (Claude Code CLI, Codex CLI, third-party wrappers, user scripts)
 * configure `OPENAI_API_BASE=http://localhost:8002` or
 * `ANTHROPIC_BASE_URL=http://localhost:8002` and use Xueban as their
 * LLM provider — transparently dispatching to the Xueban BYOK pool +
 * the 15.18 llm-providers backend.
 *
 *   POST /api/llm/messages              Anthropic Messages API shape
 *   POST /api/llm/chat/completions      OpenAI Chat Completions API shape
 *
 * Each endpoint:
 *   1. Parses the client-protocol body shape (Anthropic or OpenAI).
 *   2. Picks a backend provider from the `model` field's prefix:
 *        claude-*           → anthropic
 *        gpt-*  / o1-*      → openai
 *        deepseek-*         → deepseek
 *        glm-*  / zhipu-*   → zhipu
 *        unknown            → client-protocol default
 *   3. Extracts BYOK key from header:
 *        x-api-key  (Anthropic style)
 *        Authorization: Bearer <key>  (OpenAI style — also accepted on /messages)
 *   4. Streams `callProvider(...)` chunks and re-encodes them into the
 *      client-expected protocol (SSE frames or one-shot JSON).
 *
 * Errors are translated to the corresponding HTTP status code BEFORE any
 * SSE bytes are flushed; once we're mid-stream, errors are emitted as the
 * protocol-specific terminal event (`message_stop` for Anthropic, `[DONE]`
 * for OpenAI) followed by close.
 */

import { Router, Request, Response } from 'express';
import { randomUUID } from 'crypto';
import {
  callProvider,
  DEFAULT_MODELS,
  isProviderId,
  PROVIDER_ENV_VAR,
  type ProviderChunk,
  type ProviderId,
} from '../transport/api-clients';
import { recordUsage } from '../storage/token-usage';

const router = Router();

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Pick a backend provider id from the model string's prefix. */
export function pickProviderFromModel(
  model: unknown,
  clientDefault: ProviderId,
): ProviderId {
  if (typeof model !== 'string' || model.length === 0) return clientDefault;
  const m = model.toLowerCase();
  if (m.startsWith('claude-') || m.startsWith('claude.')) return 'anthropic';
  if (m.startsWith('gpt-') || m.startsWith('o1-') || m.startsWith('o3-') || m === 'gpt-4o') {
    return 'openai';
  }
  if (m.startsWith('deepseek-')) return 'deepseek';
  if (m.startsWith('glm-') || m.startsWith('zhipu-')) return 'zhipu';
  if (m.startsWith('mimo-') || m.startsWith('xiaomi-')) return 'mimo';
  return clientDefault;
}

/** Extract BYOK key from headers. Accepts both `x-api-key` and `Authorization: Bearer`. */
function extractApiKey(req: Request): string | undefined {
  const xKey = req.header('x-api-key');
  if (typeof xKey === 'string' && xKey.length > 0) return xKey;
  const auth = req.header('authorization');
  if (typeof auth === 'string') {
    const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
    if (m) return m[1].trim();
  }
  return undefined;
}

/**
 * Reduce an OpenAI-style or Anthropic-style messages array into a single
 * user-message string. Tool-use / multi-turn isn't expanded here — we
 * concatenate plain-text segments. System messages are stripped (they
 * belong in the `systemPrompt` arg, not `userMessage`).
 */
function extractUserContent(messages: unknown): string {
  if (!Array.isArray(messages)) return '';
  const parts: string[] = [];
  for (const msg of messages) {
    if (!msg || typeof msg !== 'object') continue;
    const role = (msg as { role?: unknown }).role;
    if (role === 'system') continue;
    const content = (msg as { content?: unknown }).content;
    if (typeof content === 'string') {
      parts.push(content);
    } else if (Array.isArray(content)) {
      for (const block of content) {
        if (block && typeof block === 'object') {
          const text = (block as { text?: unknown; type?: unknown }).text;
          if (typeof text === 'string') parts.push(text);
        } else if (typeof block === 'string') {
          parts.push(block);
        }
      }
    }
  }
  return parts.join('\n\n');
}

/** Pull a system prompt from the body. OpenAI = role:system messages joined. */
function extractSystemPrompt(body: {
  system?: unknown;
  messages?: unknown;
}, protocol: 'anthropic' | 'openai'): string {
  if (protocol === 'anthropic') {
    const sys = body.system;
    if (typeof sys === 'string') return sys;
    if (Array.isArray(sys)) {
      return sys
        .map((b) => (b && typeof b === 'object' ? (b as { text?: unknown }).text : b))
        .filter((t): t is string => typeof t === 'string')
        .join('\n\n');
    }
    return '';
  }
  // OpenAI: gather role:system messages
  if (!Array.isArray(body.messages)) return '';
  const sys: string[] = [];
  for (const msg of body.messages) {
    if (!msg || typeof msg !== 'object') continue;
    if ((msg as { role?: unknown }).role !== 'system') continue;
    const c = (msg as { content?: unknown }).content;
    if (typeof c === 'string') sys.push(c);
  }
  return sys.join('\n\n');
}

/** Map a ProviderChunk error code to an HTTP status. */
function errorCodeToStatus(code: string | undefined): number {
  switch (code) {
    case 'NO_API_KEY':
    case 'AUTH_FAILED':
      return 401;
    case 'RATE_LIMITED':
      return 429;
    case 'MODEL_NOT_FOUND':
      return 404;
    case 'NETWORK_ERROR':
      return 502;
    default:
      return 500;
  }
}

/**
 * BYOK key resolution: header key first, then the server-side env fallback
 * (PROVIDER_ENV_VAR — e.g. ZHIPU_API_KEY). Lets the persona generator and
 * other BYOK-optional frontends work without a locally stored key when the
 * server has a key configured.
 */
function resolveApiKey(req: Request, providerId: ProviderId): string | undefined {
  const headerKey = extractApiKey(req);
  if (headerKey && headerKey.length > 0) return headerKey;
  const envVar = PROVIDER_ENV_VAR[providerId];
  const envKey = envVar ? process.env[envVar] : undefined;
  return typeof envKey === 'string' && envKey.length > 0 ? envKey : undefined;
}

/** Write one SSE event in the Anthropic format (`event:` + `data:` lines). */
function writeAnthropicSse(res: Response, event: string, data: unknown): void {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

/** Write one SSE event in the OpenAI format (`data: <json>`). */
function writeOpenAiSse(res: Response, data: unknown): void {
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

// ── Anthropic protocol — POST /api/llm/messages ──────────────────────────────

router.post('/messages', async (req: Request, res: Response) => {
  const body = (req.body ?? {}) as {
    model?: unknown;
    system?: unknown;
    messages?: unknown;
    max_tokens?: unknown;
    temperature?: unknown;
    stream?: unknown;
  };

  if (typeof body.model !== 'string' || body.model.length === 0) {
    res.status(400).json({
      type: 'error',
      error: { type: 'invalid_request_error', message: 'model is required' },
    });
    return;
  }
  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    res.status(400).json({
      type: 'error',
      error: { type: 'invalid_request_error', message: 'messages must be a non-empty array' },
    });
    return;
  }

  const providerId = pickProviderFromModel(body.model, 'anthropic');
  const apiKey = resolveApiKey(req, providerId);
  const wantStream = body.stream === true;
  const max_tokens = typeof body.max_tokens === 'number' ? body.max_tokens : 1024;
  const temperature = typeof body.temperature === 'number' ? body.temperature : undefined;
  const systemPrompt = extractSystemPrompt(body, 'anthropic');
  const userMessage = extractUserContent(body.messages);

  const ac = new AbortController();
  // Wire client-disconnect to the response object, NOT the request. Node 16+
  // fires `req` 'close' as soon as the request body is fully consumed (i.e.
  // immediately after express.json() parses it) — aborting on that signal
  // silently kills every provider call before it starts (0 chunks, 200 + empty
  // content). `res` 'close' fires only when the underlying connection drops
  // before the response completes, which is the semantics we actually want.
  res.on('close', () => {
    if (!res.writableEnded) ac.abort();
  });

  const stream = callProvider(providerId, {
    systemPrompt,
    userMessage,
    model: body.model,
    max_tokens,
    temperature,
    api_key: apiKey,
    signal: ac.signal,
  });

  const msgId = `msg_${randomUUID().replace(/-/g, '').slice(0, 24)}`;

  if (!wantStream) {
    // Non-streaming: accumulate all text-deltas, then return one JSON shape.
    let text = '';
    let firstChunk: ProviderChunk | null = null;
    try {
      for await (const chunk of stream) {
        if (firstChunk === null) firstChunk = chunk;
        if (chunk.type === 'text-delta') {
          text += chunk.text;
        } else if (chunk.type === 'error') {
          res.status(errorCodeToStatus(chunk.code)).json({
            type: 'error',
            error: {
              type: chunk.code ?? 'api_error',
              message: chunk.message,
              provider: chunk.provider ?? providerId,
            },
          });
          return;
        }
      }
    } catch (err) {
      res.status(500).json({
        type: 'error',
        error: { type: 'api_error', message: (err as Error).message },
      });
      return;
    }
    res.status(200).json({
      id: msgId,
      type: 'message',
      role: 'assistant',
      model: body.model,
      content: [{ type: 'text', text }],
      stop_reason: 'end_turn',
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: text.length },
    });
    // Token 用量流水（best-effort，失败静默）— 数据看板数据源
    recordUsage({
      session_id: req.header('x-session-id') ?? '',
      model: body.model,
      input_tokens: 0,
      output_tokens: text.length,
    });
    return;
  }

  // Streaming: Anthropic Messages SSE protocol
  res.status(200);
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders?.();

  writeAnthropicSse(res, 'message_start', {
    type: 'message_start',
    message: {
      id: msgId,
      type: 'message',
      role: 'assistant',
      model: body.model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    },
  });
  writeAnthropicSse(res, 'content_block_start', {
    type: 'content_block_start',
    index: 0,
    content_block: { type: 'text', text: '' },
  });

  let outputChars = 0;
  let errored = false;
  try {
    for await (const chunk of stream) {
      if (chunk.type === 'text-delta') {
        outputChars += chunk.text.length;
        writeAnthropicSse(res, 'content_block_delta', {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: chunk.text },
        });
      } else if (chunk.type === 'error') {
        errored = true;
        writeAnthropicSse(res, 'error', {
          type: 'error',
          error: {
            type: chunk.code ?? 'api_error',
            message: chunk.message,
            provider: chunk.provider ?? providerId,
          },
        });
        break;
      }
    }
  } catch (err) {
    errored = true;
    writeAnthropicSse(res, 'error', {
      type: 'error',
      error: { type: 'api_error', message: (err as Error).message },
    });
  }

  writeAnthropicSse(res, 'content_block_stop', { type: 'content_block_stop', index: 0 });
  writeAnthropicSse(res, 'message_delta', {
    type: 'message_delta',
    delta: { stop_reason: errored ? 'error' : 'end_turn', stop_sequence: null },
    usage: { output_tokens: outputChars },
  });
  writeAnthropicSse(res, 'message_stop', { type: 'message_stop' });
  // Token 用量流水（best-effort，失败静默）— 流式 Anthropic 路径
  recordUsage({
    session_id: req.header('x-session-id') ?? '',
    model: body.model,
    input_tokens: 0,
    output_tokens: outputChars,
  });
  res.end();
});

// ── OpenAI protocol — POST /api/llm/chat/completions ─────────────────────────

router.post('/chat/completions', async (req: Request, res: Response) => {
  const body = (req.body ?? {}) as {
    model?: unknown;
    messages?: unknown;
    max_tokens?: unknown;
    temperature?: unknown;
    stream?: unknown;
  };

  if (typeof body.model !== 'string' || body.model.length === 0) {
    res.status(400).json({
      error: { type: 'invalid_request_error', code: 'invalid_request', message: 'model is required' },
    });
    return;
  }
  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    res.status(400).json({
      error: {
        type: 'invalid_request_error',
        code: 'invalid_request',
        message: 'messages must be a non-empty array',
      },
    });
    return;
  }

  const providerId = pickProviderFromModel(body.model, 'openai');
  const apiKey = resolveApiKey(req, providerId);
  const wantStream = body.stream === true;
  const max_tokens = typeof body.max_tokens === 'number' ? body.max_tokens : 1024;
  const temperature = typeof body.temperature === 'number' ? body.temperature : undefined;
  const systemPrompt = extractSystemPrompt(body, 'openai');
  const userMessage = extractUserContent(body.messages);

  const ac = new AbortController();
  // See the Anthropic-handler comment above: `req` 'close' fires the moment
  // the request body is consumed in Node 16+, aborting the provider call
  // before it starts. `res` 'close' is the real client-disconnect signal.
  res.on('close', () => {
    if (!res.writableEnded) ac.abort();
  });

  const stream = callProvider(providerId, {
    systemPrompt,
    userMessage,
    model: body.model,
    max_tokens,
    temperature,
    api_key: apiKey,
    signal: ac.signal,
  });

  const completionId = `chatcmpl-${randomUUID().replace(/-/g, '').slice(0, 24)}`;
  const created = Math.floor(Date.now() / 1000);

  if (!wantStream) {
    let text = '';
    try {
      for await (const chunk of stream) {
        if (chunk.type === 'text-delta') {
          text += chunk.text;
        } else if (chunk.type === 'error') {
          res.status(errorCodeToStatus(chunk.code)).json({
            error: {
              type: chunk.code ?? 'api_error',
              code: chunk.code ?? 'api_error',
              message: chunk.message,
              provider: chunk.provider ?? providerId,
            },
          });
          return;
        }
      }
    } catch (err) {
      res.status(500).json({
        error: { type: 'api_error', code: 'api_error', message: (err as Error).message },
      });
      return;
    }
    res.status(200).json({
      id: completionId,
      object: 'chat.completion',
      created,
      model: body.model,
      choices: [
        {
          index: 0,
          message: { role: 'assistant', content: text },
          finish_reason: 'stop',
        },
      ],
      usage: { prompt_tokens: 0, completion_tokens: text.length, total_tokens: text.length },
    });
    // Token 用量流水（best-effort，失败静默）— 非流式 OpenAI 路径
    recordUsage({
      session_id: req.header('x-session-id') ?? '',
      model: body.model,
      input_tokens: 0,
      output_tokens: text.length,
    });
    return;
  }

  // Streaming: OpenAI chat.completion.chunk SSE protocol
  res.status(200);
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders?.();

  // First chunk: role marker
  writeOpenAiSse(res, {
    id: completionId,
    object: 'chat.completion.chunk',
    created,
    model: body.model,
    choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }],
  });

  let errored = false;
  let outputCharsOpenAi = 0;
  try {
    for await (const chunk of stream) {
      if (chunk.type === 'text-delta') {
        outputCharsOpenAi += chunk.text.length;
        writeOpenAiSse(res, {
          id: completionId,
          object: 'chat.completion.chunk',
          created,
          model: body.model,
          choices: [{ index: 0, delta: { content: chunk.text }, finish_reason: null }],
        });
      } else if (chunk.type === 'error') {
        errored = true;
        writeOpenAiSse(res, {
          error: {
            type: chunk.code ?? 'api_error',
            code: chunk.code ?? 'api_error',
            message: chunk.message,
            provider: chunk.provider ?? providerId,
          },
        });
        break;
      }
    }
  } catch (err) {
    errored = true;
    writeOpenAiSse(res, {
      error: { type: 'api_error', code: 'api_error', message: (err as Error).message },
    });
  }

  // Terminal chunk: finish_reason + [DONE]
  writeOpenAiSse(res, {
    id: completionId,
    object: 'chat.completion.chunk',
    created,
    model: body.model,
    choices: [{ index: 0, delta: {}, finish_reason: errored ? 'error' : 'stop' }],
  });
  res.write('data: [DONE]\n\n');
  // Token 用量流水（best-effort，失败静默）— 流式 OpenAI 路径
  recordUsage({
    session_id: req.header('x-session-id') ?? '',
    model: body.model,
    input_tokens: 0,
    output_tokens: outputCharsOpenAi,
  });
  res.end();
});

// ── Method guards: non-POST verbs → 405 ──────────────────────────────────────

router.all('/messages', (req: Request, res: Response) => {
  if (req.method === 'POST') return;
  res.status(405).json({
    type: 'error',
    error: { type: 'invalid_request_error', message: 'method not allowed' },
  });
});

router.all('/chat/completions', (req: Request, res: Response) => {
  if (req.method === 'POST') return;
  res.status(405).json({
    error: { type: 'invalid_request_error', code: 'method_not_allowed', message: 'method not allowed' },
  });
});

export default router;
// Re-exports for tests
export { pickProviderFromModel as __pickProviderFromModel };
// Suppress unused-export warning — these are intentionally referenced by tests only.
export const _typeRefs: { ProviderId?: ProviderId; isProviderId?: typeof isProviderId; DEFAULT_MODELS?: typeof DEFAULT_MODELS } = {};
