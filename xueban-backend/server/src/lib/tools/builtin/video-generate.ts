/**
 * video-generate.ts — builtin `video_generate` tool (text-to-video).
 *
 * Default mode PROMPT (expensive + long-running → explicit approval unless a
 * skill whitelist opts in). Provider via env:
 *   `SF_VIDEO_API_BASE`  POST {base}/generations  { model, prompt, duration, size }
 *   `SF_VIDEO_API_KEY`   optional Bearer token
 *   `SF_VIDEO_MODEL`     provider model id
 * Providers differ wildly (sync result vs async job), so the response body is
 * passed through as `result` — an async provider simply returns its job id
 * here and the caller polls with fetch_url. No base → structured stub.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const videoGenerateTool: ToolSpec = {
  name: 'video_generate',
  description:
    'Generate a short video from a text prompt via the configured video provider ' +
    '(SF_VIDEO_API_BASE, POST /generations). Returns the provider result or job ' +
    'handle. Side-effecting (costly): prompts for approval unless allowed. ' +
    'Returns a stub when no provider is configured.',
  input_schema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'Text description of the video.' },
      duration: { type: 'number', description: 'Requested duration in seconds (provider-dependent).' },
      size: { type: 'string', description: "Requested resolution, e.g. '1280x720'." },
      model: { type: 'string', description: 'Override SF_VIDEO_MODEL for this call.' },
    },
    required: ['prompt'],
    additionalProperties: false,
  },
  source: 'base',
};

interface VideoGenerateInput {
  prompt: string;
  duration?: number;
  size?: string;
  model?: string;
}

function isVideoGenerateInput(x: unknown): x is VideoGenerateInput {
  return typeof x === 'object' && x !== null && typeof (x as { prompt?: unknown }).prompt === 'string';
}

export const videoGenerateExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isVideoGenerateInput(input)) {
    return { output: { error: 'video_generate: input must be { prompt: string, duration?, size?, model? }' }, isError: true };
  }

  const env = providerEnv(ctx);
  const base = env.SF_VIDEO_API_BASE?.replace(/\/+$/, '');
  if (!base) {
    return providerStub(
      'video_generate',
      'No video provider configured (set SF_VIDEO_API_BASE, optionally SF_VIDEO_API_KEY / SF_VIDEO_MODEL). No video was generated.',
      { prompt: input.prompt },
    );
  }

  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (env.SF_VIDEO_API_KEY) headers.authorization = `Bearer ${env.SF_VIDEO_API_KEY}`;

  try {
    const res = await providerFetchJson(
      `${base}/generations`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          model: input.model ?? env.SF_VIDEO_MODEL,
          prompt: input.prompt,
          ...(input.duration !== undefined ? { duration: input.duration } : {}),
          ...(input.size ? { size: input.size } : {}),
        }),
      },
      ctx,
    );
    if (res.status >= 400) {
      return { output: { error: `video_generate: provider ${res.status}`, detail: res.data }, isError: true };
    }
    return {
      output: {
        provider: 'http',
        prompt: input.prompt,
        result: res.data,
        note: 'Async providers return a job handle in `result`; poll the provider endpoint (e.g. with fetch_url) for completion.',
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `video_generate failed: ${msg}` }, isError: true };
  }
};
