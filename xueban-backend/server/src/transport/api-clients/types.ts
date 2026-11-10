/**
 * llm-providers/types.ts — Shared Provider abstraction (Story 15.18)
 *
 * 2026-05-16: Expanded ProviderId union from 4 → 12 to match the BYOK UI's
 * supported set. New providers added: google (Gemini), qwen (Bailian),
 * moonshot, mistral, groq, openrouter, ollama, lmstudio, azure. Every new
 * provider uses the OpenAI-compatible streamCompletion contract except for
 * google (Gemini's own /v1beta protocol) and azure (deployment-name URL).
 *
 * IMPORTANT — error contract:
 *   Providers NEVER throw inside `streamCompletion`. All errors must be yielded
 *   as `{ type: 'error', message, code }` chunks; the generator then `return`s.
 *   This keeps the dispatcher / runner code symmetric across all providers.
 */

/** All supported provider identifiers. */
export type ProviderId =
  | 'anthropic'
  | 'openai'
  | 'deepseek'
  | 'zhipu'
  | 'mimo'
  | 'google'
  | 'qwen'
  | 'moonshot'
  | 'mistral'
  | 'groq'
  | 'openrouter'
  | 'ollama'
  | 'lmstudio'
  | 'azure';

/** Inputs every provider accepts. */
export interface ProviderInput {
  /** Final composed system prompt. */
  systemPrompt: string;
  /** User goal (single-turn user message). */
  userMessage: string;
  /** Optional base64 data URLs for MiMo/OpenAI-compatible multimodal input. */
  attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
  /** Provider-specific model id. Falls back to provider's defaultModel. */
  model?: string;
  /** Generation cap; provider may interpret slightly differently. */
  max_tokens?: number;
  /** 0..1 sampling temperature; omitted when undefined. */
  temperature?: number;
  /** BYOK key passed in by route handler. */
  api_key?: string;
  /** DeepSeek 思考强度（low / high / max） */
  reasoning_effort?: 'low' | 'high' | 'max';
  /**
   * Optional override of the provider's default chat endpoint. Lets a user
   * point ollama / lmstudio at a non-default host, or use a corporate proxy
   * for openai / anthropic. The factory respects this verbatim (no path
   * inference) — caller is expected to have already run formatApiHost.
   */
  base_url?: string;
  /** Optional rolling history and native tools for agentic callers. */
  messages?: import('../../lib/conversation-types').ConversationMessage[];
  tools?: import('../../lib/tool-spec').ToolSpec[];
  /**
   * 是否让模型输出思考过程（目前仅 MiMo 支持）。执行型任务（例如长文档整理）
   * 关掉思考可以显著缩短耗时，避免把预算耗在推理上。
   */
  thinking?: 'enabled' | 'disabled';
  /** AbortSignal — provider must wire to its underlying SDK / fetch. */
  signal?: AbortSignal;
}

/**
 * Cross-provider error taxonomy. Front-end can map these to localized user
 * messages without caring which provider emitted them.
 *   NO_API_KEY        — input.api_key + env both empty
 *   AUTH_FAILED       — provider returned 401 / invalid_api_key
 *   RATE_LIMITED      — provider returned 429
 *   MODEL_NOT_FOUND   — provider returned 404 / model_not_found
 *   NETWORK_ERROR     — fetch failed / DNS / timeout
 *   NOT_IMPLEMENTED   — provider stub exists but real impl is pending
 *   PROVIDER_ERROR    — generic catch-all
 */
export type ProviderErrorCode =
  | 'NO_API_KEY'
  | 'AUTH_FAILED'
  | 'RATE_LIMITED'
  | 'MODEL_NOT_FOUND'
  | 'NETWORK_ERROR'
  | 'NOT_IMPLEMENTED'
  | 'PROVIDER_ERROR';

/** Streaming chunk yielded by every provider. */
export type ProviderChunk =
  | { type: 'text-delta'; text: string }
  | { type: 'usage'; usage: import('../../lib/conversation-types').TokenUsage }
  | { type: 'tool-use'; id: string; name: string; input: unknown }
  | { type: 'end' }
  | {
      type: 'error';
      message: string;
      code?: ProviderErrorCode;
      /** Provider id that emitted the error (used for prefix). */
      provider?: ProviderId;
    };

/** Per-provider implementation. */
export interface LLMProvider {
  id: ProviderId;
  defaultModel: string;
  /**
   * Stream a chat completion from this provider. Implementation contract:
   *   - For each text delta: `yield { type: 'text-delta', text }`
   *   - On normal end: `yield { type: 'end' }` then return
   *   - On error (incl. NO_API_KEY): `yield { type: 'error', ... }` then return
   *   - On signal.aborted: silently return without yielding error
   */
  streamCompletion(input: ProviderInput): AsyncGenerator<ProviderChunk>;
}

/** Per-provider default model fallback table. */
export const DEFAULT_MODELS: Record<ProviderId, string> = {
  anthropic:  'claude-sonnet-4-6',
  openai:     'gpt-4o',
  deepseek:   'deepseek-chat',
  // 2026-11-04 对话完善：glm-4.5-flash 在 Coding Plan 端点返回 1309（套餐不含
  // 该模型）；glm-4.6 实测 coding 端点可用（思考模型，reasoning_content 走
  // <sf:thinking> 通道）。作为 zhipu 无显式 model 时的兜底。
  zhipu:      'glm-4.6',
  // 小米 MiMo（JUNCTION 2026 赛道用）。OpenAI 兼容端点 api.xiaomimimo.com/v1。
  mimo:       'mimo-v2.6-pro',
  google:     'gemini-2.5-flash',
  qwen:       'qwen3-max',
  moonshot:   'kimi-k2-turbo-preview',
  mistral:    'mistral-large-latest',
  groq:       'llama-3.3-70b-versatile',
  openrouter: 'anthropic/claude-sonnet-4',
  ollama:     'llama3.1:8b',
  lmstudio:   'local-model',
  azure:      'gpt-4o',
};

/** localStorage / env var name for each provider's BYOK key. */
export const PROVIDER_ENV_VAR: Record<ProviderId, string> = {
  anthropic:  'ANTHROPIC_API_KEY',
  openai:     'OPENAI_API_KEY',
  deepseek:   'DEEPSEEK_API_KEY',
  zhipu:      'ZHIPU_API_KEY',
  mimo:       'MIMO_API_KEY',
  google:     'GOOGLE_API_KEY',
  qwen:       'DASHSCOPE_API_KEY',
  moonshot:   'MOONSHOT_API_KEY',
  mistral:    'MISTRAL_API_KEY',
  groq:       'GROQ_API_KEY',
  openrouter: 'OPENROUTER_API_KEY',
  ollama:     'OLLAMA_API_KEY',
  lmstudio:   'LMSTUDIO_API_KEY',
  azure:      'AZURE_OPENAI_API_KEY',
};

/** Display name for error messages. */
export const PROVIDER_LABEL: Record<ProviderId, string> = {
  anthropic:  'Anthropic',
  openai:     'OpenAI',
  deepseek:   'DeepSeek',
  zhipu:      'Zhipu',
  mimo:       'Xiaomi MiMo',
  google:     'Google Gemini',
  qwen:       'Qwen',
  moonshot:   'Moonshot',
  mistral:    'Mistral',
  groq:       'Groq',
  openrouter: 'OpenRouter',
  ollama:     'Ollama',
  lmstudio:   'LM Studio',
  azure:      'Azure OpenAI',
};

/** All provider ids — useful for validation. */
export const PROVIDER_IDS: ReadonlyArray<ProviderId> = [
  'anthropic',
  'openai',
  'deepseek',
  'zhipu',
  'mimo',
  'google',
  'qwen',
  'moonshot',
  'mistral',
  'groq',
  'openrouter',
  'ollama',
  'lmstudio',
  'azure',
];

export function isProviderId(s: unknown): s is ProviderId {
  return typeof s === 'string' && (PROVIDER_IDS as ReadonlyArray<string>).includes(s);
}

/** Providers that allow an empty api_key (local runtimes). */
export const PROVIDERS_NO_KEY: ReadonlySet<ProviderId> = new Set<ProviderId>([
  'ollama',
  'lmstudio',
]);
