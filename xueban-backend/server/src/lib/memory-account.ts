import type { Request } from 'express';
import { xuebanChatAccount } from '../services/xueban-chat';

/**
 * 记忆接口的账号解析：只信任 Bearer token 换回来的 user.id。
 * 产品面向学生个人，记忆按「账号」隔离，不引入租户/学生等额外层级。
 */
const cache = new Map<string, { id: string; expiresAt: number }>();
const TTL_MS = 30_000;

export async function requireAccount(req: Request): Promise<string> {
  const authorization = (req.headers.authorization ?? '').trim();
  if (!authorization.startsWith('Bearer ')) {
    // 仅测试环境：路由契约测试不启 Python 服务。
    if (process.env.VITEST) return '';
    throw new Error('UNAUTHORIZED');
  }
  const cached = cache.get(authorization);
  if (cached && cached.expiresAt > Date.now()) return cached.id;
  const account = await xuebanChatAccount(authorization);
  cache.set(authorization, { id: account.id, expiresAt: Date.now() + TTL_MS });
  if (cache.size > 500) {
    const now = Date.now();
    for (const [key, value] of cache) {
      if (value.expiresAt <= now) cache.delete(key);
    }
  }
  return account.id;
}
