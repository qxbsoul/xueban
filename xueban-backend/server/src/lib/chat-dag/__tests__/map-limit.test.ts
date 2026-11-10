import { describe, it, expect } from 'vitest';
import { mapLimit } from '../map-limit';

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('mapLimit', () => {
  it('保序返回结果', async () => {
    const r = await mapLimit([1, 2, 3, 4], 2, async (n) => n * 10);
    expect(r).toEqual([10, 20, 30, 40]);
  });

  it('并发不超过 limit', async () => {
    let active = 0;
    let peak = 0;
    await mapLimit([1, 2, 3, 4, 5, 6], 2, async () => {
      active++; peak = Math.max(peak, active);
      await tick(15);
      active--;
    });
    expect(peak).toBeLessThanOrEqual(2);
  });

  it('limit ≥ 长度 → 全并发(峰值=长度)', async () => {
    let active = 0; let peak = 0;
    await mapLimit([1, 2, 3], 10, async () => { active++; peak = Math.max(peak, active); await tick(10); active--; });
    expect(peak).toBe(3);
  });

  it('limit ≤ 0 视为 1(串行兜底)', async () => {
    let active = 0; let peak = 0;
    await mapLimit([1, 2, 3], 0, async () => { active++; peak = Math.max(peak, active); await tick(10); active--; });
    expect(peak).toBe(1);
  });

  it('空数组 → 空结果,不调用 fn', async () => {
    let called = 0;
    const r = await mapLimit([], 3, async () => { called++; return 1; });
    expect(r).toEqual([]);
    expect(called).toBe(0);
  });

  it('fn 抛错 → 整体 reject(快速失败)', async () => {
    await expect(mapLimit([1, 2, 3], 2, async (n) => { if (n === 2) throw new Error('boom'); return n; })).rejects.toThrow('boom');
  });

  it('fn 收到正确 index', async () => {
    const seen: Array<[number, number]> = [];
    await mapLimit(['a', 'b', 'c'], 2, async (item, i) => { seen.push([i, item.charCodeAt(0)]); return 0; });
    expect(seen.map((s) => s[0]).sort((a, b) => a - b)).toEqual([0, 1, 2]);
  });

  it('fast-fail 后不再领新 item(失败后 fn 调用数受限)', async () => {
    let calls = 0;
    await expect(
      mapLimit([1, 2, 3, 4], 2, async (n) => {
        calls++;
        await tick(5);
        if (n === 1) throw new Error('boom');
        return n;
      }),
    ).rejects.toThrow('boom');
    expect(calls).toBeLessThanOrEqual(2); // 修复前会变成 3~4
  });

  it('limit 为 NaN → 兜底为 1(不返回稀疏空结果)', async () => {
    const r = await mapLimit([1, 2, 3], NaN, async (n) => n * 2);
    expect(r).toEqual([2, 4, 6]);
  });
});
