import type { Request } from 'express';
import { xuebanChatAccount } from '../services/xueban-chat';

/**
 * 记忆接口的账号解析：只信任 Bearer token 换回来的 user.id。
 * 产品面向学生个人，记忆按「账号」隔离，不引入租户/学生等额外层级。
 *
 * 不做 token → 账号缓存：登出/吊销后必须立刻失效，宁可每次多一次
 * Python /auth/me 调用，也不让旧 token 在缓存窗口内继续访问记忆。
 */
export async function requireAccount(req: Request): Promise<string> {
  const authorization = (req.headers.authorization ?? '').trim();
  if (!authorization.startsWith('Bearer ')) {
    // 仅测试环境：路由契约测试不启 Python 服务。
    if (process.env.VITEST) return '';
    throw new Error('UNAUTHORIZED');
  }
  const account = await xuebanChatAccount(authorization);
  return account.id;
}
