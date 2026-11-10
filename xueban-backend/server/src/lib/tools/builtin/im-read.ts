/**
 * im-read.ts — builtin `im_read` tool (chat/messaging read access).
 *
 * Default mode ALLOW (read-only). Talks to a generic IM bridge so any
 * messenger (WeCom / Feishu / DingTalk / Slack …) can be adapted server-side:
 *   `SF_IM_API_BASE`   bridge base URL, contract:
 *                        GET {base}/conversations?limit=N
 *                          → { conversations: [...] }
 *                        GET {base}/conversations/{id}/messages?limit=N
 *                          → { messages: [...] }
 *   `SF_IM_API_TOKEN`  optional Bearer token.
 * Without SF_IM_API_BASE → structured stub listing nothing.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const imReadTool: ToolSpec = {
  name: 'im_read',
  description:
    'Read chat/messaging data via the configured IM bridge (SF_IM_API_BASE): ' +
    'list conversations, or read the message history of one conversation. ' +
    'Read-only. Returns a stub when no bridge is configured.',
  input_schema: {
    type: 'object',
    properties: {
      action: {
        type: 'string',
        enum: ['list_conversations', 'read_messages'],
        description: 'list_conversations = chat list; read_messages = history of one chat.',
      },
      conversation_id: {
        type: 'string',
        description: 'Conversation id (required for read_messages).',
      },
      limit: { type: 'number', description: 'Max items to return (1-100, default 20).' },
    },
    required: ['action'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ImReadInput {
  action: 'list_conversations' | 'read_messages';
  conversation_id?: string;
  limit?: number;
}

function isImReadInput(x: unknown): x is ImReadInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as Record<string, unknown>;
  return o.action === 'list_conversations' || o.action === 'read_messages';
}

export const imReadExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isImReadInput(input)) {
    return {
      output: { error: "im_read: input must be { action: 'list_conversations'|'read_messages', conversation_id?, limit? }" },
      isError: true,
    };
  }
  if (input.action === 'read_messages' && !input.conversation_id) {
    return { output: { error: 'im_read: conversation_id is required for read_messages' }, isError: true };
  }

  const env = providerEnv(ctx);
  const base = env.SF_IM_API_BASE?.replace(/\/+$/, '');
  const limit = Math.max(1, Math.min(100, input.limit ?? 20));

  if (!base) {
    return providerStub(
      'im_read',
      'No IM bridge configured (set SF_IM_API_BASE + optional SF_IM_API_TOKEN; contract: GET /conversations, GET /conversations/{id}/messages). Nothing was read.',
      { action: input.action },
    );
  }

  const headers: Record<string, string> = { accept: 'application/json' };
  if (env.SF_IM_API_TOKEN) headers.authorization = `Bearer ${env.SF_IM_API_TOKEN}`;

  const url =
    input.action === 'list_conversations'
      ? `${base}/conversations?limit=${limit}`
      : `${base}/conversations/${encodeURIComponent(input.conversation_id!)}/messages?limit=${limit}`;

  try {
    const res = await providerFetchJson(url, { method: 'GET', headers }, ctx);
    if (res.status >= 400) {
      return { output: { error: `im_read: bridge ${res.status}`, detail: res.data }, isError: true };
    }
    return {
      output: {
        provider: 'http',
        action: input.action,
        conversation_id: input.conversation_id,
        data: res.data,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `im_read failed: ${msg}` }, isError: true };
  }
};
