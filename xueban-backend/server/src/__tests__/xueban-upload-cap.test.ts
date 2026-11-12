/**
 * 附件上传总量限制（2026-11-12）。
 *
 * Multer 会并发处理同一请求里的多个文件，所以“每个文件开始时读一次已用字节”
 * 会被并发绕过。这里直接用 storage 引擎验证：多个文件同时写，累计超过上限时
 * 整个请求被拒绝，且所有文件回调都会结算（不会挂住 Multer）。
 */
import { describe, expect, it } from 'vitest';
import { PassThrough } from 'node:stream';
import type { Request } from 'express';
import { createCappedMemoryStorage } from '../routes/xueban-chat';

function handleFile(
  engine: ReturnType<typeof createCappedMemoryStorage>,
  req: Request,
  chunks: Buffer[],
) {
  const stream = new PassThrough();
  return new Promise<{ error: unknown; size: number }>((resolve) => {
    engine._handleFile(req, { stream } as never, (error, info) => {
      resolve({ error, size: info?.size ?? 0 });
    });
    for (const chunk of chunks) stream.write(chunk);
    stream.end();
  });
}

describe('并发附件总量限制', () => {
  it('三个文件各 6 字节、上限 10 字节：累计超限即中断，成功缓存不超过上限', async () => {
    const engine = createCappedMemoryStorage(10);
    const req = {} as Request;
    const results = await Promise.all([
      handleFile(engine, req, [Buffer.alloc(6, 1)]),
      handleFile(engine, req, [Buffer.alloc(6, 2)]),
      handleFile(engine, req, [Buffer.alloc(6, 3)]),
    ]);
    expect(results.some((r) => r.error)).toBe(true);
    const buffered = results.filter((r) => !r.error).reduce((sum, r) => sum + r.size, 0);
    expect(buffered).toBeLessThanOrEqual(10);
  });

  it('单个文件超过上限也会被拒绝', async () => {
    const engine = createCappedMemoryStorage(10);
    const result = await handleFile(engine, {} as Request, [Buffer.alloc(11)]);
    expect(result.error).toBeTruthy();
  });

  it('多个文件分块交错写入也不会超过上限', async () => {
    const engine = createCappedMemoryStorage(12);
    const req = {} as Request;
    const results = await Promise.all([
      handleFile(engine, req, [Buffer.alloc(4), Buffer.alloc(4)]),
      handleFile(engine, req, [Buffer.alloc(4), Buffer.alloc(4)]),
    ]);
    expect(results.some((r) => r.error)).toBe(true);
    const buffered = results.filter((r) => !r.error).reduce((sum, r) => sum + r.size, 0);
    expect(buffered).toBeLessThanOrEqual(12);
  });
});
