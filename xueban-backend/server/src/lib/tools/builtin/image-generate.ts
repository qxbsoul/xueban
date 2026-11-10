/**
 * image-generate.ts — builtin `image_generate` tool (text-to-image).
 *
 * Default mode ALLOW. Provider via env, OpenAI-compatible images endpoint:
 *   `SF_IMAGE_API_BASE`  e.g. https://api.openai.com/v1  (POST {base}/images/generations)
 *   `SF_IMAGE_API_KEY`   optional Bearer token
 *   `SF_IMAGE_MODEL`     default 'gpt-image-1'
 * No base configured → structured stub, nothing generated.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const imageGenerateTool: ToolSpec = {
  name: 'image_generate',
  description:
    'Generate images from a text prompt via the configured image provider ' +
    '(SF_IMAGE_API_BASE, OpenAI-compatible /images/generations). Returns image ' +
    'urls or base64 payloads. Returns a stub when no provider is configured.',
  input_schema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'Text description of the image.' },
      size: {
        type: 'string',
        description: "Optional size, e.g. '1024x1024', '1792x1024'.",
      },
      n: { type: 'number', description: 'Number of images (1-4, default 1).' },
      model: { type: 'string', description: 'Override SF_IMAGE_MODEL for this call.' },
    },
    required: ['prompt'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ImageGenerateInput {
  prompt: string;
  size?: string;
  n?: number;
  model?: string;
}

function isImageGenerateInput(x: unknown): x is ImageGenerateInput {
  return typeof x === 'object' && x !== null && typeof (x as { prompt?: unknown }).prompt === 'string';
}

export const imageGenerateExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isImageGenerateInput(input)) {
    return { output: { error: 'image_generate: input must be { prompt: string, size?, n?, model? }' }, isError: true };
  }

  const env = providerEnv(ctx);
  const base = env.SF_IMAGE_API_BASE?.replace(/\/+$/, '');
  if (!base) {
    return providerStub(
      'image_generate',
      'No image provider configured (set SF_IMAGE_API_BASE, optionally SF_IMAGE_API_KEY / SF_IMAGE_MODEL). No image was generated.',
      { prompt: input.prompt },
    );
  }

  const n = Math.max(1, Math.min(4, input.n ?? 1));
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (env.SF_IMAGE_API_KEY) headers.authorization = `Bearer ${env.SF_IMAGE_API_KEY}`;

  try {
    const res = await providerFetchJson(
      `${base}/images/generations`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          model: input.model ?? env.SF_IMAGE_MODEL ?? 'gpt-image-1',
          prompt: input.prompt,
          n,
          ...(input.size ? { size: input.size } : {}),
        }),
      },
      ctx,
    );
    if (res.status >= 400) {
      return { output: { error: `image_generate: provider ${res.status}`, detail: res.data }, isError: true };
    }
    const payload = res.data as { data?: unknown[] } | null;
    return {
      output: {
        provider: 'http',
        prompt: input.prompt,
        images: Array.isArray(payload?.data) ? payload.data : res.data,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `image_generate failed: ${msg}` }, isError: true };
  }
};
