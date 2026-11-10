/**
 * send-email.ts — builtin `send_email` tool.
 *
 * Default mode PROMPT (external side effect — every send is user-approved
 * unless a skill whitelist explicitly opts in via listed→allow semantics).
 *
 * Provider selection (priority order), mirroring web-search.ts:
 *   1. `RESEND_API_KEY`    — Resend HTTP API (https://api.resend.com/emails).
 *                            Requires `SF_MAIL_FROM` as the from address.
 *   2. `SF_MAIL_API_BASE`  — generic mail bridge: POST {base}/send with JSON
 *                            { to, subject, text, html, attachments }.
 *                            Optional `SF_MAIL_API_TOKEN` → Bearer auth.
 *   3. (none)              — structured stub, no mail is sent.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson, providerStub } from './provider-http';

export const sendEmailTool: ToolSpec = {
  name: 'send_email',
  description:
    'Send an email. Provider is configured via RESEND_API_KEY (+SF_MAIL_FROM) or ' +
    'SF_MAIL_API_BASE (generic bridge); without those configured returns a stub ' +
    'and sends nothing. Attachments are base64-encoded.',
  input_schema: {
    type: 'object',
    properties: {
      to: {
        oneOf: [
          { type: 'string' },
          { type: 'array', items: { type: 'string' } },
        ],
        description: 'Recipient address, or list of addresses.',
      },
      subject: { type: 'string', description: 'Email subject.' },
      text: { type: 'string', description: 'Plain-text body.' },
      html: { type: 'string', description: 'HTML body (optional).' },
      attachments: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            filename: { type: 'string' },
            content: { type: 'string', description: 'Base64-encoded file content.' },
          },
          required: ['filename', 'content'],
          additionalProperties: false,
        },
        description: 'Optional base64 attachments.',
      },
    },
    required: ['to', 'subject'],
    additionalProperties: false,
  },
  source: 'base',
};

interface MailAttachment {
  filename: string;
  content: string;
}

interface SendEmailInput {
  to: string | string[];
  subject: string;
  text?: string;
  html?: string;
  attachments?: MailAttachment[];
}

function isSendEmailInput(x: unknown): x is SendEmailInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as Record<string, unknown>;
  const toOk =
    typeof o.to === 'string' ||
    (Array.isArray(o.to) && o.to.every((t) => typeof t === 'string' && t.length > 0));
  return toOk && typeof o.subject === 'string' && o.subject.length > 0;
}

export const sendEmailExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isSendEmailInput(input)) {
    return {
      output: { error: 'send_email: input must be { to: string|string[], subject: string, text?, html?, attachments? }' },
      isError: true,
    };
  }

  const to = Array.isArray(input.to) ? input.to : [input.to];
  const env = providerEnv(ctx);
  const resendKey = env.RESEND_API_KEY;
  const mailBase = env.SF_MAIL_API_BASE;

  if (!resendKey && !mailBase) {
    return providerStub(
      'send_email',
      'No mail provider configured (set RESEND_API_KEY + SF_MAIL_FROM, or SF_MAIL_API_BASE). No email was sent.',
      { to, subject: input.subject },
    );
  }

  try {
    if (resendKey) {
      const from = env.SF_MAIL_FROM;
      if (!from) {
        return {
          output: {
            error: 'send_email: RESEND_API_KEY is set but SF_MAIL_FROM (from address) is missing',
          },
          isError: true,
        };
      }
      const res = await providerFetchJson(
        'https://api.resend.com/emails',
        {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            authorization: `Bearer ${resendKey}`,
          },
          body: JSON.stringify({
            from,
            to,
            subject: input.subject,
            text: input.text,
            html: input.html,
            attachments: input.attachments,
          }),
        },
        ctx,
      );
      if (res.status >= 400) {
        return { output: { error: `send_email: Resend ${res.status}`, detail: res.data }, isError: true };
      }
      const id = (res.data as { id?: string } | null)?.id;
      return { output: { provider: 'resend', ok: true, id, to, subject: input.subject } };
    }

    // Generic mail bridge.
    const base = mailBase!.replace(/\/+$/, '');
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    if (env.SF_MAIL_API_TOKEN) headers.authorization = `Bearer ${env.SF_MAIL_API_TOKEN}`;
    const res = await providerFetchJson(
      `${base}/send`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          to,
          subject: input.subject,
          text: input.text,
          html: input.html,
          attachments: input.attachments,
        }),
      },
      ctx,
    );
    if (res.status >= 400) {
      return { output: { error: `send_email: bridge ${res.status}`, detail: res.data }, isError: true };
    }
    return { output: { provider: 'http', ok: true, to, subject: input.subject, data: res.data } };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `send_email failed: ${msg}` }, isError: true };
  }
};
