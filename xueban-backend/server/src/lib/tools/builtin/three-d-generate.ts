/**
 * three-d-generate.ts — builtin `generate_3d` tool (text/image-to-3D model).
 *
 * Default mode PROMPT (expensive → explicit approval unless a skill whitelist
 * opts in). Provider via env:
 *   `SF_3D_API_BASE`  POST {base}/generations  { prompt, format }
 *   `SF_3D_API_KEY`   optional Bearer token
 *   `SF_3D_MODEL`     optional model id
 * Response body passed through as `result` (usually a model file url or
 * job handle). No base → structured stub.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const generate3dTool: ToolSpec = {
  name: 'generate_3d',
  description:
    'Generate a 3D model from a text prompt via the configured 3D provider ' +
    '(SF_3D_API_BASE, POST /generations). Returns the provider result (model ' +
    'file url / job handle). Side-effecting (costly): prompts for approval ' +
    'unless allowed. Returns a stub when no provider is configured.',
  input_schema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'Text description of the 3D asset.' },
      format: {
        type: 'string',
        enum: ['glb', 'obj', 'usdz', 'fbx'],
        description: 'Requested model format (default glb).',
      },
      model: { type: 'string', description: 'Override SF_3D_MODEL for this call.' },
    },
    required: ['prompt'],
    additionalProperties: false,
  },
  source: 'base',
};

interface Generate3dInput {
  prompt: string;
  format?: 'glb' | 'obj' | 'usdz' | 'fbx';
  model?: string;
}

function isGenerate3dInput(x: unknown): x is Generate3dInput {
  return typeof x === 'object' && x !== null && typeof (x as { prompt?: unknown }).prompt === 'string';
}

export const generate3dExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isGenerate3dInput(input)) {
    return { output: { error: 'generate_3d: input must be { prompt: string, format?, model? }' }, isError: true };
  }

  const env = providerEnv(ctx);
  const base = env.SF_3D_API_BASE?.replace(/\/+$/, '');
  if (!base) {
    return providerStub(
      'generate_3d',
      'No 3D provider configured (set SF_3D_API_BASE, optionally SF_3D_API_KEY / SF_3D_MODEL). No model was generated.',
      { prompt: input.prompt },
    );
  }

  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (env.SF_3D_API_KEY) headers.authorization = `Bearer ${env.SF_3D_API_KEY}`;

  try {
    const res = await providerFetchJson(
      `${base}/generations`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          model: input.model ?? env.SF_3D_MODEL,
          prompt: input.prompt,
          format: input.format ?? 'glb',
        }),
      },
      ctx,
    );
    if (res.status >= 400) {
      return { output: { error: `generate_3d: provider ${res.status}`, detail: res.data }, isError: true };
    }
    return {
      output: {
        provider: 'http',
        prompt: input.prompt,
        format: input.format ?? 'glb',
        result: res.data,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `generate_3d failed: ${msg}` }, isError: true };
  }
};
