/**
 * platform-tools.test.ts — WorkBuddy 平台能力工具层 contract tests.
 *
 * Covers the 8 provider-backed platform tools (send_email / im_read / im_send /
 * github_request / github_write / image_generate / video_generate / generate_3d)
 * and the web_search recency/wechat enhancement:
 *   - registry integrity: 17 builtins, executor/mode coverage, mode sanity
 *   - stub contract: no provider configured → structured stub, no network I/O
 *   - provider mapping: URL / headers / body asserted via a mocked global fetch
 *   - input validation errors surface as isError results
 *
 * Run: npx vitest run --project=server
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  builtinTools,
  builtinExecutors,
  builtinDefaultModes,
} from '../lib/tools/builtin';
import { webSearchExecutor } from '../lib/tools/builtin/web-search';
import { sendEmailExecutor } from '../lib/tools/builtin/send-email';
import { imReadExecutor } from '../lib/tools/builtin/im-read';
import { imSendExecutor } from '../lib/tools/builtin/im-send';
import {
  githubRequestExecutor,
  githubWriteExecutor,
} from '../lib/tools/builtin/github';
import { imageGenerateExecutor } from '../lib/tools/builtin/image-generate';
import { videoGenerateExecutor } from '../lib/tools/builtin/video-generate';
import { generate3dExecutor } from '../lib/tools/builtin/three-d-generate';
import type { BuiltinToolContext } from '../lib/tools/builtin/types';

// ─── helpers ────────────────────────────────────────────────────────────────

function makeCtx(env: Record<string, string> = {}): BuiltinToolContext {
  return {
    workspace: '/tmp/sf-platform-test',
    signal: new AbortController().signal,
    env,
  };
}

interface FetchCall {
  url: string;
  init: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
  };
}

function mockFetch(
  response: { status: number; json?: unknown; text?: string },
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init: FetchCall['init'] = {}) => {
      calls.push({ url: String(url), init });
      const bodyText =
        response.text ?? JSON.stringify(response.json ?? {});
      return {
        ok: (response.status ?? 200) < 400,
        status: response.status ?? 200,
        headers: new Headers(),
        text: async () => bodyText,
        json: async () => JSON.parse(bodyText),
      };
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ─── registry integrity ─────────────────────────────────────────────────────

describe('builtin registry (platform capability pack)', () => {
  it('registers exactly the 17 builtin tools with unique names', () => {
    expect(builtinTools).toHaveLength(17);
    const names = builtinTools.map((t) => t.name);
    expect(new Set(names).size).toBe(17);
    for (const n of [
      'send_email', 'im_read', 'im_send', 'github_request', 'github_write',
      'image_generate', 'video_generate', 'generate_3d',
    ]) {
      expect(names).toContain(n);
    }
  });

  it('every tool has an executor and a permission mode', () => {
    for (const t of builtinTools) {
      expect(typeof builtinExecutors[t.name]).toBe('function');
      expect(['allow', 'prompt', 'deny']).toContain(builtinDefaultModes[t.name]);
    }
    expect(Object.keys(builtinExecutors).sort()).toEqual(
      builtinTools.map((t) => t.name).sort(),
    );
  });

  it('side-effecting tools default to prompt; reads default to allow', () => {
    expect(builtinDefaultModes.send_email).toBe('prompt');
    expect(builtinDefaultModes.im_send).toBe('prompt');
    expect(builtinDefaultModes.github_write).toBe('prompt');
    expect(builtinDefaultModes.video_generate).toBe('prompt');
    expect(builtinDefaultModes.generate_3d).toBe('prompt');
    expect(builtinDefaultModes.im_read).toBe('allow');
    expect(builtinDefaultModes.github_request).toBe('allow');
    expect(builtinDefaultModes.image_generate).toBe('allow');
    expect(builtinDefaultModes.shell_exec).toBe('deny');
  });
});

// ─── web_search recency / wechat ────────────────────────────────────────────

describe('web_search recency + wechat source', () => {
  it('falls back to public Bing results when no API key is configured', async () => {
    const html = `
      <ol>
        <li class="b_algo"><h2><a href="https://example.com/a">示例结果 A</a></h2>
          <p>2 天前&ensp;·&ensp;这是摘要 A</p></li>
        <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9i">示例结果 B</a></h2>
          <p>摘要 B</p></li>
      </ol>`;
    const calls = mockFetch({ status: 200, text: html });
    const res = await webSearchExecutor({ query: 'xueban', limit: 5 }, makeCtx({}));
    expect(res.isError).toBeUndefined();
    const out = res.output as { provider: string; count: number; results: Array<{ title: string; url: string; snippet: string }>; note?: string };
    expect(out.provider).toBe('bing-html');
    expect(out.count).toBe(2);
    // 结果必须是真实外链，且过滤掉 bing 自己
    expect(out.results[0]).toMatchObject({ title: '示例结果 A', url: 'https://example.com/a' });
    // /ck/a 包装的跳转要还原成真实地址
    expect(out.results[1].url).toBe('https://example.com/b');
    expect(out.results[1].snippet).toBe('摘要 B');
    expect(out.note).toContain('Bing');
    expect(calls[0].url).toContain('bing.com/search');
  });

  it('maps recency to SerpAPI tbs and wechat to the wechat engine', async () => {
    const calls = mockFetch({
      status: 200,
      json: {
        wechat_results: [{ title: 't', link: 'https://mp.weixin.qq.com/x', snippet: 's' }],
      },
    });
    const res = await webSearchExecutor(
      { query: 'AI 助手', recency: 'week', source: 'wechat', limit: 3 },
      makeCtx({ SERPAPI_API_KEY: 'k' }),
    );
    expect(res.isError).toBeUndefined();
    const url = new URL(calls[0].url);
    expect(url.searchParams.get('engine')).toBe('wechat');
    expect(url.searchParams.get('q')).toBe('AI 助手');
    // wechat engine ignores recency/tbs
    expect(url.searchParams.get('tbs')).toBeNull();
    const out = res.output as { source: string; count: number; results: unknown[] };
    expect(out.source).toBe('wechat');
    expect(out.count).toBe(1);
  });

  it('maps recency to tbs=qdr:w for google engine', async () => {
    const calls = mockFetch({
      status: 200,
      json: { organic_results: [{ title: 'a', link: 'https://a.example', snippet: 'b' }] },
    });
    await webSearchExecutor(
      { query: 'q', recency: 'week' },
      makeCtx({ SERPAPI_API_KEY: 'k' }),
    );
    const url = new URL(calls[0].url);
    expect(url.searchParams.get('engine')).toBe('google');
    expect(url.searchParams.get('tbs')).toBe('qdr:w');
  });

  it('maps recency to Tavily days and notes the wechat fallback', async () => {
    const calls = mockFetch({
      status: 200,
      json: { results: [{ title: 'a', url: 'https://a.example', content: 'c' }] },
    });
    const res = await webSearchExecutor(
      { query: 'q', recency: 'month', source: 'wechat' },
      makeCtx({ TAVILY_API_KEY: 'k' }),
    );
    const body = JSON.parse(calls[0].init.body ?? '{}');
    expect(body.days).toBe(30);
    const out = res.output as { source: string; note?: string };
    expect(out.source).toBe('web');
    expect(out.note).toContain('SERPAPI_API_KEY');
  });
});

// ─── send_email ─────────────────────────────────────────────────────────────

describe('send_email', () => {
  it('stubs without a provider and sends nothing', async () => {
    const calls = mockFetch({ status: 200, json: {} });
    const res = await sendEmailExecutor(
      { to: 'a@example.com', subject: 'hi' },
      makeCtx({}),
    );
    expect(res.isError).toBeUndefined();
    expect((res.output as { provider: string }).provider).toBe('stub');
    expect(calls).toHaveLength(0);
  });

  it('errors when RESEND_API_KEY is set but SF_MAIL_FROM is missing', async () => {
    const res = await sendEmailExecutor(
      { to: ['a@example.com'], subject: 'hi', text: 'body' },
      makeCtx({ RESEND_API_KEY: 'k' }),
    );
    expect(res.isError).toBe(true);
    expect((res.output as { error: string }).error).toContain('SF_MAIL_FROM');
  });

  it('maps to the Resend API with from/to/subject and returns the id', async () => {
    const calls = mockFetch({ status: 200, json: { id: 'mail-1' } });
    const res = await sendEmailExecutor(
      { to: 'a@example.com', subject: 'hi', text: 'body', attachments: [{ filename: 'f.txt', content: 'YQ==' }] },
      makeCtx({ RESEND_API_KEY: 'k', SF_MAIL_FROM: 'bot@example.com' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://api.resend.com/emails');
    expect(calls[0].init.headers?.authorization).toBe('Bearer k');
    const body = JSON.parse(calls[0].init.body ?? '{}');
    expect(body.from).toBe('bot@example.com');
    expect(body.to).toEqual(['a@example.com']);
    expect(body.attachments).toHaveLength(1);
    expect((res.output as { id: string }).id).toBe('mail-1');
  });

  it('posts to the generic mail bridge when SF_MAIL_API_BASE is set', async () => {
    const calls = mockFetch({ status: 200, json: { ok: true } });
    const res = await sendEmailExecutor(
      { to: 'a@example.com', subject: 'hi' },
      makeCtx({ SF_MAIL_API_BASE: 'https://mail.bridge/', SF_MAIL_API_TOKEN: 't' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://mail.bridge/send');
    expect(calls[0].init.headers?.authorization).toBe('Bearer t');
    expect((res.output as { provider: string }).provider).toBe('http');
  });

  it('rejects invalid input', async () => {
    const res = await sendEmailExecutor({ subject: 'x' }, makeCtx({}));
    expect(res.isError).toBe(true);
  });
});

// ─── im_read / im_send ──────────────────────────────────────────────────────

describe('im_read / im_send', () => {
  it('im_read stubs without a bridge', async () => {
    const calls = mockFetch({ status: 200, json: {} });
    const res = await imReadExecutor({ action: 'list_conversations' }, makeCtx({}));
    expect(res.isError).toBeUndefined();
    expect((res.output as { provider: string }).provider).toBe('stub');
    expect(calls).toHaveLength(0);
  });

  it('im_read builds the conversations and messages URLs', async () => {
    const calls = mockFetch({ status: 200, json: { conversations: [], messages: [] } });
    const ctx = makeCtx({ SF_IM_API_BASE: 'https://im.bridge', SF_IM_API_TOKEN: 't' });
    await imReadExecutor({ action: 'list_conversations', limit: 5 }, ctx);
    expect(calls[0].url).toBe('https://im.bridge/conversations?limit=5');
    expect(calls[0].init.headers?.authorization).toBe('Bearer t');
    await imReadExecutor({ action: 'read_messages', conversation_id: 'c 1', limit: 2 }, ctx);
    expect(calls[1].url).toBe('https://im.bridge/conversations/c%201/messages?limit=2');
  });

  it('im_read requires conversation_id for read_messages', async () => {
    const res = await imReadExecutor({ action: 'read_messages' }, makeCtx({}));
    expect(res.isError).toBe(true);
  });

  it('im_send stubs without a bridge and posts when configured', async () => {
    const calls = mockFetch({ status: 200, json: { id: 'm1' } });
    const stub = await imSendExecutor(
      { conversation_id: 'c1', text: 'hello' },
      makeCtx({}),
    );
    expect((stub.output as { provider: string }).provider).toBe('stub');
    expect(calls).toHaveLength(0);

    const res = await imSendExecutor(
      { conversation_id: 'c1', text: 'hello' },
      makeCtx({ SF_IM_API_BASE: 'https://im.bridge' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://im.bridge/messages');
    expect(JSON.parse(calls[0].init.body ?? '{}')).toEqual({
      conversation_id: 'c1',
      text: 'hello',
    });
  });

  it('im_send rejects invalid input', async () => {
    const res = await imSendExecutor({ conversation_id: 'c1' }, makeCtx({}));
    expect(res.isError).toBe(true);
  });
});

// ─── github_request / github_write ──────────────────────────────────────────

describe('github_request / github_write', () => {
  it('github_request GETs api.github.com with optional token', async () => {
    const calls = mockFetch({ status: 200, json: { id: 1 } });
    const res = await githubRequestExecutor(
      { path: '/repos/owner/repo/issues' },
      makeCtx({ GITHUB_TOKEN: 'tok' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://api.github.com/repos/owner/repo/issues');
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].init.headers?.authorization).toBe('Bearer tok');
    expect((res.output as { status: number }).status).toBe(200);
  });

  it('github_request rejects unsafe paths', async () => {
    for (const p of ['//evil.com/x', 'https://evil.com', '/a b', 'repos/x']) {
      const res = await githubRequestExecutor({ path: p }, makeCtx({}));
      expect(res.isError).toBe(true);
    }
  });

  it('github_request flags 403 with a rate-limit note and isError', async () => {
    mockFetch({ status: 403, json: { message: 'rate limited' } });
    const res = await githubRequestExecutor({ path: '/rate_limit' }, makeCtx({}));
    expect(res.isError).toBe(true);
    expect((res.output as { note?: string }).note).toContain('GITHUB_TOKEN');
  });

  it('github_write requires a token and passes method/body through', async () => {
    const noTok = await githubWriteExecutor(
      { method: 'POST', path: '/repos/o/r/issues', body: { title: 'x' } },
      makeCtx({}),
    );
    expect(noTok.isError).toBe(true);

    const calls = mockFetch({ status: 201, json: { id: 9 } });
    const res = await githubWriteExecutor(
      { method: 'POST', path: '/repos/o/r/issues', body: { title: 'x' } },
      makeCtx({ GITHUB_TOKEN: 'tok' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].init.method).toBe('POST');
    expect(JSON.parse(calls[0].init.body ?? '{}')).toEqual({ title: 'x' });
    expect((res.output as { status: number }).status).toBe(201);
  });
});

// ─── image / video / 3d generation ──────────────────────────────────────────

describe('image_generate / video_generate / generate_3d', () => {
  it('image_generate stubs without a provider', async () => {
    const calls = mockFetch({ status: 200, json: {} });
    const res = await imageGenerateExecutor({ prompt: 'a cat' }, makeCtx({}));
    expect((res.output as { provider: string }).provider).toBe('stub');
    expect(calls).toHaveLength(0);
  });

  it('image_generate posts to OpenAI-compatible /images/generations', async () => {
    const calls = mockFetch({ status: 200, json: { data: [{ url: 'https://img' }] } });
    const res = await imageGenerateExecutor(
      { prompt: 'a cat', size: '1024x1024', n: 2 },
      makeCtx({ SF_IMAGE_API_BASE: 'https://img.api/v1/', SF_IMAGE_API_KEY: 'k' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://img.api/v1/images/generations');
    const body = JSON.parse(calls[0].init.body ?? '{}');
    expect(body).toMatchObject({ prompt: 'a cat', size: '1024x1024', n: 2, model: 'gpt-image-1' });
    expect((res.output as { images: unknown[] }).images).toHaveLength(1);
  });

  it('image_generate surfaces provider errors', async () => {
    mockFetch({ status: 500, json: { error: 'boom' } });
    const res = await imageGenerateExecutor(
      { prompt: 'x' },
      makeCtx({ SF_IMAGE_API_BASE: 'https://img.api' }),
    );
    expect(res.isError).toBe(true);
  });

  it('video_generate stubs and passes through provider results', async () => {
    const stub = await videoGenerateExecutor({ prompt: 'drone shot' }, makeCtx({}));
    expect((stub.output as { provider: string }).provider).toBe('stub');

    const calls = mockFetch({ status: 200, json: { job_id: 'v1' } });
    const res = await videoGenerateExecutor(
      { prompt: 'drone shot', duration: 5 },
      makeCtx({ SF_VIDEO_API_BASE: 'https://video.api' }),
    );
    expect(res.isError).toBeUndefined();
    expect(calls[0].url).toBe('https://video.api/generations');
    expect(JSON.parse(calls[0].init.body ?? '{}')).toMatchObject({
      prompt: 'drone shot',
      duration: 5,
    });
    expect((res.output as { result: { job_id: string } }).result.job_id).toBe('v1');
  });

  it('generate_3d stubs and passes format through', async () => {
    const stub = await generate3dExecutor({ prompt: 'a mug' }, makeCtx({}));
    expect((stub.output as { provider: string }).provider).toBe('stub');

    const calls = mockFetch({ status: 200, json: { model_url: 'https://m.glb' } });
    const res = await generate3dExecutor(
      { prompt: 'a mug', format: 'obj' },
      makeCtx({ SF_3D_API_BASE: 'https://3d.api' }),
    );
    expect(res.isError).toBeUndefined();
    expect(JSON.parse(calls[0].init.body ?? '{}')).toMatchObject({
      prompt: 'a mug',
      format: 'obj',
    });
    expect((res.output as { format: string }).format).toBe('obj');
  });

  it('rejects inputs missing prompt', async () => {
    for (const exec of [imageGenerateExecutor, videoGenerateExecutor, generate3dExecutor]) {
      const res = await exec({ not_prompt: 1 }, makeCtx({}));
      expect(res.isError).toBe(true);
    }
  });
});
