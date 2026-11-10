export type EmbeddingProvider = 'qwen' | 'openai' | 'siliconflow' | 'custom';

export interface EmbeddingSettingsState {
  provider: EmbeddingProvider;
  baseUrl: string;
  model: string;
  hasKey: boolean;
}

export const EMBEDDING_PROVIDERS: Record<Exclude<EmbeddingProvider, 'custom'>, { label: string; baseUrl: string; models: string[] }> = {
  qwen: {
    label: '通义千问（默认）',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: ['qwen3.7-text-embedding-flash', 'text-embedding-v4', 'text-embedding-v3'],
  },
  openai: {
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    models: ['text-embedding-3-small', 'text-embedding-3-large'],
  },
  siliconflow: {
    label: 'SiliconFlow 硅基流动',
    baseUrl: 'https://api.siliconflow.cn/v1',
    models: ['BAAI/bge-m3', 'BAAI/bge-large-zh-v1.5'],
  },
};

export const DEFAULT_EMBEDDING = {
  provider: 'qwen' as const,
  baseUrl: EMBEDDING_PROVIDERS.qwen.baseUrl,
  model: EMBEDDING_PROVIDERS.qwen.models[0],
};

function read(key: string): string {
  try { return localStorage.getItem(key) ?? ''; } catch { return ''; }
}

export function loadEmbeddingSettings(): EmbeddingSettingsState {
  const provider = read('sf_embedding_provider') as EmbeddingProvider;
  const validProvider = provider === 'openai' || provider === 'siliconflow' || provider === 'custom' ? provider : 'qwen';
  return {
    provider: validProvider,
    baseUrl: read('sf_embedding_base_url') || (validProvider === 'custom' ? '' : EMBEDDING_PROVIDERS[validProvider].baseUrl),
    model: read('sf_embedding_model') || (validProvider === 'custom' ? '' : EMBEDDING_PROVIDERS[validProvider].models[0]),
    hasKey: Boolean(read('sf_embedding_key')),
  };
}

export function saveEmbeddingSettings(input: Omit<EmbeddingSettingsState, 'hasKey'> & { apiKey: string }): EmbeddingSettingsState {
  localStorage.setItem('sf_embedding_provider', input.provider);
  localStorage.setItem('sf_embedding_base_url', input.baseUrl.trim().replace(/\/$/, ''));
  localStorage.setItem('sf_embedding_model', input.model.trim());
  if (input.apiKey.trim()) localStorage.setItem('sf_embedding_key', input.apiKey.trim());
  return loadEmbeddingSettings();
}

export function clearEmbeddingKey(): EmbeddingSettingsState {
  localStorage.removeItem('sf_embedding_key');
  return loadEmbeddingSettings();
}

export function embeddingRequestHeaders(): Record<string, string> {
  const key = read('sf_embedding_key');
  if (!key) return {};
  return {
    'X-Embedding-Key': key,
    'X-Embedding-Base-Url': read('sf_embedding_base_url') || DEFAULT_EMBEDDING.baseUrl,
    'X-Embedding-Model': read('sf_embedding_model') || DEFAULT_EMBEDDING.model,
  };
}
