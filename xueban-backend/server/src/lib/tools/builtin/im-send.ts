/**
 * im-send.ts — builtin `im_send` tool (chat/messaging send).
 *
 * Default mode PROMPT (external side effect — every send is user-approved
 * unless a skill whitelist explicitly opts in via listed→allow semantics).
 * Same SF_IM_API_BASE bridge as im_read.ts:
 *   POST {base}/messages  { conversation_id, text }
 * Optional `SF_IM_API_TOKEN` → Bearer auth. No bridge → stub, nothing sent.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const imSendTool: ToolSpec = {
  name: 'im_send',
  description:
    'Send a chat message via the configured IM bridge (SF_IM_API_BASE, POST /messages). ' +
    'Sends to one conversation id. Returns a stub when no bridge is configured.',
  input_schema: {
    type: 'object',
    properties: {
      conversation_id: { type: 'string', description: 'Target conversation id.' },
      text: { type: 'string', description: 'Message text to send.' },
    },
    required: ['conversation_id', 'text'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ImSendInput {
  conversation_id: string;
  text: string;
}

function isImSendInput(x: unknown): x is ImSendInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as Record<string, unknown>;
  return (
    typeof o.conversation_id === 'string' &&
    o.conversation_id.length > 0 &&
    typeof o.text === 'string' &&
    o.text.length > 0
  );
}

export const imSendExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isImSendInput(input)) {
    return {
      output: { error: 'im_send: input must be { conversation_id: string, text: string }' },
      isError: true,
    };
  }

  const env = providerEnv(ctx);
  const base = env.SF_IM_API_BASE?.replace(/\/+$/, '');

  if (!base) {
    return providerStub(
      'im_send',
      'No IM bridge configured (set SF_IM_API_BASE + optional SF_IM_API_TOKEN). Nothing was sent.',
      { conversation_id: input.conversation_id },
    );
  }

  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (env.SF_IM_API_TOKEN) headers.authorization = `Bearer ${env.SF_IM_API_TOKEN}`;

  try {
    const res = await providerFetchJson(
      `${base}/messages`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({ conversation_id: input.conversation_id, text: input.text }),
      },
      ctx,
    );
    if (res.status >= 400) {
      return { output: { error: `im_send: bridge ${res.status}`, detail: res.data }, isError: true };
    }
    return {
      output: {
        provider: 'http',
        ok: true,
        conversation_id: input.conversation_id,
        data: res.data,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `im_send failed: ${msg}` }, isError: true };
  }
};
