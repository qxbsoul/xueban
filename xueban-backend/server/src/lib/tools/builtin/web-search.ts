/**
 * web-search.ts — builtin `web_search` tool.
 *
 * Default mode ALLOW. Provider is picked at runtime, first available wins:
 *
 *   1. `SERPAPI_API_KEY`   — serpapi.com /search.json (Google; `source:
 *                            'wechat'` switches to the WeChat engine).
 *   2. `TAVILY_API_KEY`    — api.tavily.com /search (`days` recency filter).
 *   3. (no key)            — Bing 网页结果兜底（html.duckduckgo.com 在本机网络
 *                            不可达，Bing HTML 可用）。抓取公开搜索结果页并解析，
 *                            结果里会标注 `provider: 'bing-html'`。
 *
 * 为什么要有兜底：没有 Key 时直接返回 stub，员工只能写「未产生真实外部结果」，
 * 调研类任务等于做不了。兜底结果不完美（没有 recency / 微信公众号），但它是
 * 真实网络结果，并明确标注来源，符合「不伪造、但要能用」的原则。
 *
 * Output shape is provider-agnostic:
 *   { provider, query, results: [{ title, url, snippet }], count, note? }
 *
 * Optional filters (all backward compatible):
 *   - `recency`  day | week | month | year → SerpAPI tbs=qdr:d|w|m|y,
 *                Tavily days=1|7|30|365. Bing 兜底不支持，会在 note 里说明。
 *   - `source`   'web' (default) | 'wechat' (SerpAPI only).
 *
 * 30s timeout. Network errors surface as `isError: true` rather than throw
 * so the conversation loop can continue.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const webSearchTool: ToolSpec = {
  name: 'web_search',
  description:
    'Search the public web (or WeChat 公众号 articles via source: "wechat"). ' +
    'Returns up to 10 results with title, URL, and snippet. Optional recency ' +
    'filter: day / week / month / year. Uses SERPAPI_API_KEY or TAVILY_API_KEY ' +
    'when configured; otherwise falls back to public Bing results (provider ' +
    '"bing-html", no recency filter).',
  input_schema: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Search query string.' },
      limit: { type: 'number', description: 'Max results (1-10, default 5).' },
      recency: {
        type: 'string',
        enum: ['day', 'week', 'month', 'year'],
        description: 'Only return results from the last day/week/month/year.',
      },
      source: {
        type: 'string',
        enum: ['web', 'wechat'],
        description: "'web' = general web search (default); 'wechat' = WeChat 公众号 articles (SerpAPI only).",
      },
    },
    required: ['query'],
    additionalProperties: false,
  },
  source: 'base',
};

type Recency = 'day' | 'week' | 'month' | 'year';

interface WebSearchInput {
  query: string;
  limit?: number;
  recency?: Recency;
  source?: 'web' | 'wechat';
}

function isWebSearchInput(x: unknown): x is WebSearchInput {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as { query: unknown }).query === 'string'
  );
}

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

const SERPAPI_TBS: Record<Recency, string> = {
  day: 'qdr:d',
  week: 'qdr:w',
  month: 'qdr:m',
  year: 'qdr:y',
};

const TAVILY_DAYS: Record<Recency, number> = {
  day: 1,
  week: 7,
  month: 30,
  year: 365,
};

async function searchSerpAPI(
  apiKey: string,
  q: string,
  limit: number,
  recency: Recency | undefined,
  wechat: boolean,
  signal: AbortSignal,
): Promise<SearchResult[]> {
  const params = new URLSearchParams();
  params.set('engine', wechat ? 'wechat' : 'google');
  params.set('q', q);
  if (!wechat) params.set('num', String(limit));
  if (recency && !wechat) params.set('tbs', SERPAPI_TBS[recency]);
  params.set('api_key', apiKey);
  const res = await fetch(`https://serpapi.com/search.json?${params.toString()}`, { signal });
  if (!res.ok) throw new Error(`SerpAPI ${res.status}`);
  const json = (await res.json()) as {
    organic_results?: Array<{ title?: string; link?: string; snippet?: string }>;
    // WeChat engine returns `wechat_results` (articles) with slightly
    // different field names; normalize below.
    wechat_results?: Array<{ title?: string; link?: string; snippet?: string; url?: string }>;
  };
  const items = (wechat ? json.wechat_results ?? [] : json.organic_results ?? []);
  return items.slice(0, limit).map((r) => ({
    title: r.title ?? '',
    url: r.link ?? (r as { url?: string }).url ?? '',
    snippet: r.snippet ?? '',
  }));
}

async function searchTavily(
  apiKey: string,
  q: string,
  limit: number,
  recency: Recency | undefined,
  signal: AbortSignal,
): Promise<SearchResult[]> {
  const res = await fetch('https://api.tavily.com/search', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      api_key: apiKey,
      query: q,
      max_results: limit,
      ...(recency ? { days: TAVILY_DAYS[recency] } : {}),
    }),
    signal,
  });
  if (!res.ok) throw new Error(`Tavily ${res.status}`);
  const json = (await res.json()) as { results?: Array<{ title?: string; url?: string; content?: string }> };
  const items = json.results ?? [];
  return items.slice(0, limit).map((r) => ({
    title: r.title ?? '',
    url: r.url ?? '',
    snippet: r.content ?? '',
  }));
}

// ─── 无 Key 兜底：解析 Bing 结果页 ────────────────────────────────────────────

const ENTITIES: Record<string, string> = {
  '&amp;': '&', '&quot;': '"', '&#39;': "'", '&apos;': "'", '&lt;': '<', '&gt;': '>',
  '&nbsp;': ' ', '&ensp;': ' ', '&emsp;': ' ', '&middot;': '·', '&hellip;': '…',
  '&mdash;': '—', '&ndash;': '–', '&times;': '×',
};

function decodeEntities(value: string): string {
  return value.replace(/&(?:[a-z]+|#\d+|#x[0-9a-f]+);/gi, (match) => {
    const lower = match.toLowerCase();
    if (ENTITIES[lower]) return ENTITIES[lower];
    if (lower.startsWith('&#x')) return String.fromCharCode(Number.parseInt(lower.slice(3, -1), 16));
    if (lower.startsWith('&#')) return String.fromCharCode(Number.parseInt(lower.slice(2, -1), 10));
    return match;
  });
}

function stripTags(html: string): string {
  const withoutBlocks = html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ');
  return decodeEntities(withoutBlocks.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
}

/** Bing 有一部分结果包成 /ck/a?...&u=a1<base64url>，还原成真实地址。 */
function decodeBingRedirect(href: string): string {
  if (!href.startsWith('/ck/a') && !href.startsWith('https://www.bing.com/ck/a')) return href;
  const match = href.match(/[?&]u=a1([^&]+)/);
  if (!match) return href;
  try {
    const base64 = match[1].replace(/-/g, '+').replace(/_/g, '/');
    const decoded = Buffer.from(base64, 'base64').toString('utf8');
    return /^https?:\/\//i.test(decoded) ? decoded : href;
  } catch {
    return href;
  }
}

/** Bing HTML 结果兜底：无需 API Key。只抓公开搜索结果，不登录、不带凭据。 */
async function searchBingHtml(
  q: string,
  limit: number,
  signal: AbortSignal,
): Promise<SearchResult[]> {
  const url = new URL('https://www.bing.com/search');
  url.searchParams.set('q', q);
  url.searchParams.set('setlang', 'zh-cn');
  url.searchParams.set('count', String(Math.max(limit, 10)));
  const res = await fetch(url.toString(), {
    headers: {
      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
      'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
      accept: 'text/html,application/xhtml+xml',
    },
    signal,
  });
  if (!res.ok) throw new Error(`Bing ${res.status}`);
  const html = await res.text();
  const blocks = html.match(/<li class="b_algo"[\s\S]*?<\/li>/g) ?? [];
  const results: SearchResult[] = [];
  const seen = new Set<string>();
  for (const block of blocks) {
    const heading = block.match(/<h2[^>]*>([\s\S]*?)<\/h2>/);
    if (!heading) continue;
    const anchor = heading[1].match(/<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/);
    if (!anchor) continue;
    const href = decodeBingRedirect(decodeEntities(anchor[1]));
    if (!/^https?:\/\//i.test(href)) continue;
    if (/^https?:\/\/(www\.)?bing\.com\//i.test(href)) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    const paragraph = block.match(/<p[^>]*>([\s\S]*?)<\/p>/);
    const snippet = (paragraph ? stripTags(paragraph[1]) : '')
      .replace(/^\d+\s*(?:秒|分钟|小时|天|周|个月|年)前\s*[·・]?\s*/, '');
    results.push({ title: stripTags(anchor[2]) || href, url: href, snippet });
    if (results.length >= limit) break;
  }
  return results;
}

export const webSearchExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isWebSearchInput(input)) {
    return { output: { error: 'web_search: input must be { query: string, limit?, recency?, source? }' }, isError: true };
  }

  const limit = Math.max(1, Math.min(10, input.limit ?? 5));
  const env = ctx.env ?? (process.env as Record<string, string>);

  const serp = env.SERPAPI_API_KEY;
  const tavily = env.TAVILY_API_KEY;

  // WeChat 公众号 search is a SerpAPI-only engine. With Tavily / Bing we fall
  // back to general web results and say so, rather than failing the turn.
  const wantWechat = input.source === 'wechat';
  const wechat = wantWechat && Boolean(serp);
  const notes: string[] = [];
  if (wantWechat && !serp) {
    notes.push('WeChat 公众号 search requires SERPAPI_API_KEY; fell back to general web results.');
  }
  if (!serp && !tavily) {
    notes.push('未配置 SERPAPI_API_KEY / TAVILY_API_KEY，使用 Bing 公开网页结果兜底（无 recency 过滤）。');
  } else if (input.recency && !serp && tavily) {
    notes.push('recency 由 Tavily 的 days 参数实现。');
  }

  const timeout = AbortSignal.timeout(30_000);
  const combined = AbortSignal.any([ctx.signal, timeout]);

  try {
    const provider = serp ? 'serpapi' : tavily ? 'tavily' : 'bing-html';
    const results = serp
      ? await searchSerpAPI(serp, input.query, limit, input.recency, wechat, combined)
      : tavily
        ? await searchTavily(tavily, input.query, limit, input.recency, combined)
        : await searchBingHtml(input.query, limit, combined);
    if (provider === 'bing-html' && results.length === 0) {
      notes.push('Bing 兜底没有解析到结果；可配置 SERPAPI_API_KEY 或 TAVILY_API_KEY 获得更稳定的搜索。');
    }
    return {
      output: {
        provider,
        query: input.query,
        source: wechat ? 'wechat' : 'web',
        ...(input.recency ? { recency: input.recency } : {}),
        count: results.length,
        results,
        ...(notes.length ? { note: notes.join(' ') } : {}),
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `web_search failed: ${msg}`, provider: serp ? 'serpapi' : tavily ? 'tavily' : 'bing-html' }, isError: true };
  }
};
