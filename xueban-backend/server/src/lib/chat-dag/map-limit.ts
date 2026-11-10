/**
 * map-limit.ts — 有界并发池(批 2 Phase 2b · B5)。
 *
 * 把 items 过 fn,最多 limit 个并发在跑,结果按输入顺序返回。任一 fn 抛错 → 整体 reject
 * (快速失败,与 Promise.all 一致语义)。limit ≤ 0 兜底为 1(串行)。
 * 用于 chat DAG 同层调度:防大团队同层 Promise.all 无限并发(= Phase 2a 的 MAX_FANOUT 缺口)。
 */
export async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const n = items.length;
  const results: R[] = new Array(n);
  if (n === 0) return results;
  const cap = Number.isFinite(limit) && limit >= 1 ? Math.floor(limit) : 1;
  let next = 0;
  let failed = false;

  async function worker(): Promise<void> {
    // 领取下一个未处理下标直到取尽;任一 worker 抛错 → failed=true,其余 worker 下一轮即退出
    // (不再领新 item;在飞的调用无法取消,但不再消耗新的 LLM 调用)。
    while (!failed && next < n) {
      const i = next++;
      try {
        results[i] = await fn(items[i], i);
      } catch (e) {
        failed = true;
        throw e;
      }
    }
  }

  const workers = Array.from({ length: Math.min(cap, n) }, () => worker());
  await Promise.all(workers); // 任一 worker reject → 整体 reject
  return results;
}
