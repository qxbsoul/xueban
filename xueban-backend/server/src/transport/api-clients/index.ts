/**
 * transport/api-clients/index.ts — Compat shim (Phase 2 consolidation).
 *
 * Phase 2 collapsed the duplicate provider abstractions:
 *   - Track A (formerly here): `LLMProvider` + `callProvider()` simple
 *     AsyncGenerator<ProviderChunk> (single-turn, no tool_use)
 *   - Track B (formerly under `lib/api-clients/`): `ApiClient` + `stream()`
 *     AssistantEvent (multi-turn, tool_use, usage accounting)
 * into a single home at `transport/api-clients/`. ApiClient is the canonical
 * contract; `callProvider()` is preserved here as a thin adapter over
 * `ApiClient.stream({ tools: [] })` so legacy single-turn callers
 * (routes/llm.ts, transport/spawners/anthropic.ts) keep working unchanged.
 *
 * Long-term: those callers should migrate to `new XxxApiClient(...).stream(...)`
 * directly, or move through `ApiClientCallable` → `LlmCallable.turn()`. This
 * file's `callProvider` is a transition shim, not a forever API.
 *
 * Re-exports the data tables (DEFAULT_MODELS, PROVIDER_ENV_VAR, etc.) from
 * `./types` verbatim — those constants are still the single source of truth
 * for the 13 provider catalog.
 */

import { AnthropicApiClient } from './anthropic-api-client';
import { GoogleApiClient } from './google-api-client';
import { OpenAiCompatApiClient } from './openai-compat-api-client';
import {
  isProviderId,
  PROVIDER_ENV_VAR,
  PROVIDERS_NO_KEY,
  type ProviderChunk,
  type ProviderId,
  type ProviderInput,
} from './types';

export type {
  LLMProvider,
  ProviderChunk,
  ProviderErrorCode,
  ProviderId,
  ProviderInput,
} from './types';
export {
  DEFAULT_MODELS,
  PROVIDER_ENV_VAR,
  PROVIDER_IDS,
  PROVIDER_LABEL,
  PROVIDERS_NO_KEY,
  isProviderId,
} from './types';

/** OpenAI-compatible providers (all routed through OpenAiCompatApiClient). */
const OPENAI_COMPAT: ReadonlySet<ProviderId> = new Set<ProviderId>([
  'openai',
  'deepseek',
  'zhipu',
  'mimo',
  'qwen',
  'moonshot',
  'mistral',
  'groq',
  'openrouter',
  'ollama',
  'lmstudio',
  'azure',
]);

/**
 * Single-turn streaming entry — adapts ApiClient.stream() into the simple
 * AsyncGenerator<ProviderChunk> shape that older callers expect. Used by:
 *   - routes/llm.ts (proxy/back-compat endpoint)
 *   - transport/spawners/anthropic.ts (default-executor SSE runner)
 *
 * Behavior contract preserved from the original Track A `callProvider`:
 *   - Unknown provider id              → yield single 'error' chunk, return
 *   - text deltas                      → yield { type: 'text-delta', text }
 *   - token usage                      → yield { type: 'usage', usage }
 *   - normal end                       → yield { type: 'end' }
 *   - any throw from ApiClient.stream  → yield { type: 'error', ... }, return
 *
 * Tool-use / message_stop AssistantEvents are intentionally dropped
 * — single-turn callers don't consume them.
 */
export async function* callProvider(
  providerId: string,
  input: ProviderInput,
): AsyncGenerator<ProviderChunk> {
  if (!isProviderId(providerId)) {
    yield {
      type: 'error',
      message: `Unknown provider: "${providerId}"`,
      code: 'PROVIDER_ERROR',
    };
    return;
  }

  const envVar = PROVIDER_ENV_VAR[providerId];
  const apiKey = input.api_key || (envVar ? process.env[envVar] : undefined);
  const model = input.model;
  const max_tokens = input.max_tokens;
  const temperature = input.temperature;
  const signal = input.signal ?? new AbortController().signal;

  // ApiClient implementations intentionally throw for a missing key. Restore
  // the public callProvider error contract before constructing a client so
  // callers receive NO_API_KEY (and HTTP 401 in routes/llm.ts), not a generic
  // PROVIDER_ERROR / HTTP 500. Local providers remain keyless by design.
  if (!apiKey && !PROVIDERS_NO_KEY.has(providerId)) {
    yield {
      type: 'error',
      message: `${providerId} API key not configured (BYOK)`,
      code: 'NO_API_KEY',
      provider: providerId,
    };
    return;
  }

  let client;
  try {
    if (providerId === 'anthropic') {
      client = new AnthropicApiClient({ apiKey, model, max_tokens, temperature });
    } else if (providerId === 'google') {
      client = new GoogleApiClient({ apiKey, model, max_tokens, temperature });
    } else if (OPENAI_COMPAT.has(providerId)) {
      client = new OpenAiCompatApiClient({
        providerId,
        apiKey,
        model,
        max_tokens,
        temperature,
        thinking: input.thinking,
        reasoning_effort: input.reasoning_effort,
        baseURL: input.base_url,
      });
    } else {
      yield {
        type: 'error',
        message: `Unsupported provider: "${providerId}"`,
        code: 'PROVIDER_ERROR',
        provider: providerId,
      };
      return;
    }
  } catch (err) {
    yield {
      type: 'error',
      message: (err as Error).message ?? String(err),
      code: 'PROVIDER_ERROR',
      provider: providerId,
    };
    return;
  }

  try {
    const stream = client.stream({
      system_prompt: input.systemPrompt,
      messages: input.messages ?? [
        { role: 'user', blocks: [
          { kind: 'text', text: input.userMessage },
          ...(input.attachments ?? []).map(item => item.kind === 'image'
            ? { kind: 'image' as const, data_url: item.data_url }
            : { kind: 'video' as const, data_url: item.data_url }),
        ] },
      ],
      tools: input.tools ?? [],
      signal,
    });
    for await (const ev of stream) {
      if (signal.aborted) return;
      if (ev.kind === 'text_delta') {
        yield { type: 'text-delta', text: ev.text };
      } else if (ev.kind === 'usage') {
        yield { type: 'usage', usage: ev.usage };
      } else if (ev.kind === 'tool_use') {
        yield { type: 'tool-use', id: ev.id, name: ev.name, input: ev.input };
      }
      // message_stop is normalized to the single end event below.
    }
    yield { type: 'end' };
  } catch (err) {
    if (signal.aborted) return;
    yield {
      type: 'error',
      message: (err as Error).message ?? String(err),
      code: 'PROVIDER_ERROR',
      provider: providerId,
    };
  }
}
