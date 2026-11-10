/**
 * skill-runners/anthropic.ts — Default executor runner (Story 15.19 v2)
 *
 * Story 15.18 refactor — this file no longer talks to the Anthropic SDK
 * directly. It is now a thin adapter between the dispatcher's RunnerInput
 * shape and the new llm-providers abstraction. Behavior remains identical
 * for the default executor path:
 *
 *   1. Resolve provider — `input.provider` (forwarded by route handler) wins
 *      over the historical hard-coded 'anthropic' default. The runner's name
 *      stays "anthropic" because the dispatcher key (`undefined` /
 *      `'anthropic-direct'`) selects this runner; the actual LLM provider
 *      fanned out at the provider layer is whatever the user picked.
 *   2. Build ProviderInput from RunnerInput.
 *   3. Pipe ProviderChunk → SseEvent through `parseAndExtract`.
 *   4. Persist <artifact> tag bodies under `input.cwd`.
 *
 * The dispatcher (`skill-runners/index.ts`) calls into here whenever the
 * skill's `executor` field is undefined or `'anthropic-direct'`.
 */

import fs from 'fs';
import path from 'path';
import { parseAndExtract, type SseEvent } from '../../parser';
import {
  callProvider,
  isProviderId,
  PROVIDER_LABEL,
  type ProviderId,
} from '../api-clients';
import type { RunnerInput } from './types';

export async function* runAnthropicDirect(input: RunnerInput): AsyncGenerator<SseEvent> {
  const {
    prompt: goal,
    system_prompt,
    session_id,
    anthropic_key,
    signal,
  } = input;

  if (!system_prompt) {
    yield {
      event: 'error',
      data: {
        message: 'Skill 未配置 system_prompt。',
        code: 'SKILL_NOT_CONFIGURED',
      },
    };
    return;
  }

  // Story 15.18 — provider resolution priority:
  //   process.env.XUEBAN_DEFAULT_PROVIDER
  //     > input.provider (forwarded by route handler / sessionStore)
  //     > 'anthropic'
  // Env wins so a server admin can pin a provider for debugging without
  // touching the front-end.
  const envProvider = process.env.XUEBAN_DEFAULT_PROVIDER;
  const candidate =
    (envProvider && isProviderId(envProvider) ? envProvider : undefined) ??
    input.provider ??
    'anthropic';
  const providerId: ProviderId = isProviderId(candidate) ? candidate : 'anthropic';

  // Each provider has its own BYOK header; the route handler picks the right
  // one and forwards it via `input.api_key`. Back-compat: when called via the
  // older anthropic_key field (still present in 15.19 dispatcher seam) we use
  // it as the fallback for the anthropic provider only.
  const apiKey =
    input.api_key ??
    (providerId === 'anthropic' ? anthropic_key : undefined);

  // Project dir (under input.cwd which is the per-session artifact root).
  try {
    fs.mkdirSync(input.cwd, { recursive: true });
  } catch (err) {
    yield {
      event: 'error',
      data: {
        message: `无法创建产物目录: ${(err as Error).message}`,
        code: 'PROJECT_DIR_FAILED',
      },
    };
    return;
  }

  const artifactCallback = (filename: string, content: string, _type: string) => {
    const safeName = path.basename(filename);
    const filePath = path.join(input.cwd, safeName);
    fs.writeFileSync(filePath, content, 'utf-8');
    console.log(
      `[skill-runner:${providerId}] artifact written: ${filePath} (${content.length} bytes)`,
    );
  };

  console.log(
    `[skill-runner:${providerId}] session=${session_id}` +
      ` model=${input.model ?? '(default)'}` +
      ` max_tokens=${input.max_tokens ?? '(default)'}` +
      ` temperature=${input.temperature ?? '(default)'}`,
  );

  let buffer = '';

  for await (const chunk of callProvider(providerId, {
    systemPrompt: system_prompt,
    userMessage: goal,
    model: input.model,
    max_tokens: input.max_tokens,
    temperature: input.temperature,
    api_key: apiKey,
    signal,
  })) {
    if (signal?.aborted) return;
    if (chunk.type === 'text-delta') {
      buffer += chunk.text;
      const { buffer: remaining, events } = parseAndExtract(
        buffer,
        session_id,
        artifactCallback,
      );
      buffer = remaining;
      for (const e of events) yield e;
    } else if (chunk.type === 'error') {
      const label = chunk.provider
        ? PROVIDER_LABEL[chunk.provider]
        : PROVIDER_LABEL[providerId];
      yield {
        event: 'error',
        data: {
          message: `[${label}] ${chunk.message}`,
          code: chunk.code ?? 'PROVIDER_ERROR',
          provider: chunk.provider ?? providerId,
        },
      };
      return;
    } else if (chunk.type === 'end') {
      break;
    }
  }

  if (buffer.trim()) {
    const { events } = parseAndExtract(buffer, session_id, artifactCallback);
    for (const e of events) yield e;
  }
}
