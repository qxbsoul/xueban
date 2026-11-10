import type { IncomingHttpHeaders } from 'node:http';

export interface EmbeddingRequestConfig {
  key: string;
  baseUrl: string;
  model: string;
}

export function embeddingConfig(headers: IncomingHttpHeaders): EmbeddingRequestConfig | null {
  const key = String(headers['x-embedding-key'] || '').trim();
  if (!key) return null;
  const baseUrl = String(headers['x-embedding-base-url'] || '').trim().replace(/\/$/, '');
  const model = String(headers['x-embedding-model'] || '').trim();
  if (!/^https?:\/\//.test(baseUrl) || !model) return null;
  return { key, baseUrl, model };
}

export function embeddingHeaders(headers: IncomingHttpHeaders): Record<string, string> {
  const config = embeddingConfig(headers);
  if (!config) return {};
  return {
    'X-Embedding-Key': config.key,
    'X-Embedding-Base-Url': config.baseUrl,
    'X-Embedding-Model': config.model,
  };
}

export async function verifyEmbedding(config: EmbeddingRequestConfig): Promise<{ model: string; dimensions: number }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60_000);
  try {
    const response = await fetch(`${config.baseUrl}/embeddings`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${config.key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: config.model, input: ['Xueban Embedding 连接测试'], encoding_format: 'float' }),
      signal: controller.signal,
    });
    const body = await response.json() as {
      data?: Array<{ embedding?: number[] }>;
      error?: { message?: string };
      message?: string;
    };
    if (!response.ok) throw new Error(body.error?.message || body.message || `Embedding 服务返回 HTTP ${response.status}`);
    const dimensions = body.data?.[0]?.embedding?.length ?? 0;
    if (!dimensions) throw new Error('Embedding 服务没有返回有效向量');
    return { model: config.model, dimensions };
  } finally {
    clearTimeout(timer);
  }
}
