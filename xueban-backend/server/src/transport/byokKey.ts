/**
 * byokKey.ts — resolve a provider's BYOK API key, server-side.
 *
 * Extracted from run-sessions.ts so the Node chat gateway (groups-chat.ts) and
 * run-sessions share ONE key-resolution policy. Priority order:
 *   1. Per-provider HTTP header (e.g. `x-zhipu-key`)       — most specific, wins
 *   2. Generic `X-LLM-Provider` + `X-LLM-Key` headers      — browser localStorage,
 *      forwarded by src/api/chat.ts buildByokHeaders (DEBT-1, 2026-06-04)
 *   3. ByokSection's saved key in settings/byok            — server-side UI path
 *   4. Per-provider env var (e.g. ZHIPU_API_KEY)           — last-resort default
 *
 * (2) is the DEBT-1 unification: the browser stores the user's key in
 * localStorage (KEY_STORAGE) and forwards it on EVERY request as the generic
 * `X-LLM-*` headers. Before this, the Node gateway only honored per-provider
 * headers + the server `byok` setting, so a user who configured their key in
 * the browser (but never populated server `byok`) got 401 on chat replies.
 * Honoring the generic header bridges 「browser localStorage → Node gateway」,
 * matching what the Python /api/chat/completions path already reads.
 *
 * (3) is the older "前端做了后端没接上" fix: ByokSection writes apiKey to
 * byok-config.json via PUT /api/settings/byok.
 */
import { getSetting } from '../storage/settings';
import { PROVIDER_ENV_VAR, type ProviderId } from './api-clients';
import { BUILTIN_MIMO_API_KEY } from './builtin-keys';

/** Lower-cased request header carrying each provider's key (per-request override). */
export const HEADER_BY_PROVIDER: Record<ProviderId, string> = {
  anthropic: 'x-anthropic-key',
  openai: 'x-openai-key',
  deepseek: 'x-deepseek-key',
  zhipu: 'x-zhipu-key',
  mimo: 'x-mimo-key',
  google: 'x-google-key',
  qwen: 'x-qwen-key',
  moonshot: 'x-moonshot-key',
  mistral: 'x-mistral-key',
  groq: 'x-groq-key',
  openrouter: 'x-openrouter-key',
  ollama: 'x-ollama-key',
  lmstudio: 'x-lmstudio-key',
  azure: 'x-azure-key',
};

/** 取 header 值(Express 可能给 string[]),trim 后返回非空字符串,否则 undefined。 */
function pickHeader(
  headers: Record<string, string | string[] | undefined>,
  name: string,
): string | undefined {
  const v = headers[name];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === 'string' && s.trim() ? s.trim() : undefined;
}

/**
 * 前端 chat-provider 名 → 后端 ProviderId。浏览器 buildByokHeaders 的 `X-LLM-Provider`
 * 用 chat 名('claude' 等),归一到 ProviderId(claude→anthropic;其余同名)。
 */
function normalizeChatProvider(name: string): string {
  return name === 'claude' ? 'anthropic' : name;
}

/**
 * Resolve the API key for `provider`. `headers` is the incoming request's
 * headers map (lower-cased keys, as Express provides). Returns undefined when
 * no key is found in any source (CLI executors don't need one).
 */
export function resolveProviderKey(
  provider: ProviderId,
  headers: Record<string, string | string[] | undefined> = {},
): string | undefined {
  // 1) per-provider 头(最具体,最高优先)。
  const specific = pickHeader(headers, HEADER_BY_PROVIDER[provider]);
  if (specific) return specific;

  // 2) 通用 X-LLM-* 头(DEBT-1 统一,2026-06-04):浏览器把 localStorage 里配的 key 经
  //    X-LLM-Provider / X-LLM-Key 转发(src/api/chat.ts buildByokHeaders)。仅当通用头声明
  //    的 provider 归一后与请求 provider 一致才采用,防止张冠李戴(如声明 openai 却被当
  //    anthropic 用)。这桥接「浏览器 localStorage → Node 网关」,不再依赖服务端 byok 设置另填。
  const llmKey = pickHeader(headers, 'x-llm-key');
  const llmProvider = pickHeader(headers, 'x-llm-provider');
  if (llmKey && llmProvider && normalizeChatProvider(llmProvider) === provider) {
    return llmKey;
  }

  // 3) 服务端 byok 设置(ByokSection 经 PUT /api/settings/byok 写入)。
  try {
    const cfg = getSetting('byok') as
      | { providers?: Record<string, { apiKey?: string }> }
      | undefined;
    const k = cfg?.providers?.[provider]?.apiKey;
    if (typeof k === 'string' && k.trim()) return k.trim();
  } catch {
    /* settings unavailable — fall through to env */
  }

  // 4) 环境变量(最后兜底)。
  const envVal = process.env[PROVIDER_ENV_VAR[provider]];
  if (typeof envVal === 'string' && envVal.trim()) return envVal.trim();

  // 5) 内置 Key：用户什么都没配时也能直接用（只在服务端使用，不下发浏览器）。
  if (provider === 'mimo' && BUILTIN_MIMO_API_KEY) return BUILTIN_MIMO_API_KEY;
  return undefined;
}

/**
 * 解析 provider 的 BYOK 自定义 baseUrl（2026-11-07 Day1 打通修复）。
 *
 * 与 `resolveProviderKey` 同一 store（settings/byok）：BYOK 设置页里用户为
 * provider 配置的自定义 API 地址（本地网关 / 私有部署 / OpenAI 兼容中转）。
 * 返回 undefined 表示未配置——调用方回退到各 client 的内置默认地址。
 *
 * 背景：此前 `resolveCallable('byok:<provider>')` 只传 key 不传 baseUrl，
 * OpenAiCompatApiClient 回退到 `https://api.openai.com/v1` 等默认地址，
 * 自定义网关/本地端点场景全部打偏（表现为 SDK "Request timed out."）。
 */
export function resolveProviderBaseUrl(
  provider: ProviderId | string,
): string | undefined {
  try {
    const cfg = getSetting('byok') as
      | { providers?: Record<string, { baseUrl?: string }> }
      | undefined;
    const u = cfg?.providers?.[provider]?.baseUrl;
    if (typeof u === 'string' && u.trim()) return u.trim();
  } catch {
    /* settings unavailable — fall through */
  }
  return undefined;
}
