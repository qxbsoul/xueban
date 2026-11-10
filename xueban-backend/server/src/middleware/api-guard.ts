/**
 * api-guard.ts — 写接口的最小访问控制（2026-11-05，功能线①③加固）。
 *
 * 为什么需要：带副作用的写接口（外发 / 删除 / 导出）需要最小访问控制（
 * 退款），`/api/memory/handoff/*` 有 4 个写接口（建交接包 / 接班 / 画像写回 /
 * 反馈提炼）。这些模块默认不鉴权时，任何能连上端口的机器都能退款、能改画像。
 *
 * 策略（不破坏现有本地开发）：
 *   1. 设了 `SF_API_TOKEN` → 必须带 `Authorization: Bearer <token>`，用
 *      timingSafeEqual 比较，长度不同直接拒（不做早退泄露）。
 *   2. 没设 `SF_API_TOKEN` → 只允许回环地址（127.0.0.1 / ::1 / ::ffff:127.0.0.1）。
 *      本机 curl、前端 dev server、测试都照常跑，不用配任何东西。
 *
 * 只挂在这两条新增功能线的 Router 上，不动其它既有路由（那是别人的代码）。
 */

import { timingSafeEqual } from 'node:crypto';
import type { NextFunction, Request, RequestHandler, Response } from 'express';

const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);

/** 回环判断：同时接受 IPv4 与 IPv6 映射写法，兼容 `::ffff:127.0.0.1`。 */
export function isLoopbackAddress(ip: string | undefined): boolean {
  if (!ip) return false;
  const normalized = ip.startsWith('::ffff:') ? ip.slice(7) : ip;
  return LOOPBACK.has(ip) || LOOPBACK.has(normalized);
}

/** 定长比较，避免通过响应时间猜 token。 */
function tokenEquals(got: string, expected: string): boolean {
  const a = Buffer.from(got, 'utf8');
  const b = Buffer.from(expected, 'utf8');
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

function bearer(req: Request): string {
  const header = String(req.headers.authorization ?? '');
  if (!header.toLowerCase().startsWith('bearer ')) return '';
  return header.slice(7).trim();
}

/**
 * 生成一个 guard 中间件。每次请求都读 env，方便测试与运行期改配置。
 */
export function apiGuard(): RequestHandler {
  return (req: Request, res: Response, next: NextFunction): void => {
    const expected = (process.env.SF_API_TOKEN ?? '').trim();

    if (expected) {
      const got = bearer(req);
      if (got && tokenEquals(got, expected)) {
        next();
        return;
      }
      res.status(got ? 403 : 401).json({
        error: { code: 'UNAUTHORIZED', message: got ? 'token 不匹配' : '缺少 Authorization: Bearer' },
      });
      return;
    }

    const ip = req.ip ?? req.socket?.remoteAddress;
    if (isLoopbackAddress(ip)) {
      next();
      return;
    }
    res.status(403).json({
      error: {
        code: 'LOOPBACK_ONLY',
        message: '该接口默认只允许本机访问；远程调用请设置 SF_API_TOKEN 并带 Bearer 头',
      },
    });
  };
}
