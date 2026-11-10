/**
 * token-usage.test.ts — Token 用量流水与看板聚合单测。
 *
 * 覆盖：recordUsage 落库与成本估算 / 非法输入归零 / 单价表缺失成本为 0 /
 * sumUsageBetween / usageDailyBetween 补零由路由层做、这里测窗口聚合 /
 * usageBreakdown TopN。
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-tokenusage-'));
process.chdir(tmpRoot);

import {
  recordUsage,
  sumUsageBetween,
  usageDailyBetween,
  usageBreakdown,
  startOfDay,
} from './storage/token-usage';
import { _resetForTests } from './storage/sqlite';

let failures = 0;
function check(name: string, cond: boolean): void {
  if (cond) {
    console.log(`  ok - ${name}`);
  } else {
    failures += 1;
    console.error(`  FAIL - ${name}`);
  }
}

const DAY = 24 * 60 * 60 * 1000;
const now = Date.now();

// ── recordUsage ──────────────────────────────────────────────────────────────
{
  recordUsage(
    { session_id: 'sess-1', model: 'claude-sonnet', input_tokens: 1000, output_tokens: 500, ts: now },
    { 'claude-sonnet': { input: 3, output: 15 } },
  );
  const sum = sumUsageBetween(now - 1000, now + 1000);
  check('落库一条且 calls=1', sum.calls === 1);
  check('input/output 求和正确', sum.input_tokens === 1000 && sum.output_tokens === 500);
  const expectCost = (1000 * 3 + 500 * 15) / 1_000_000;
  check('成本 = (in×pin + out×pout)/1M', Math.abs(sum.cost_est_usd - expectCost) < 1e-9);

  // 非法输入归零不抛
  recordUsage({ session_id: '', model: '', input_tokens: -5, output_tokens: NaN });
  const sum2 = sumUsageBetween(now - 1000, now + 1000);
  check('非法 tokens 归零且不抛错', sum2.calls === 2 && sum2.input_tokens === 1000);

  // 单价表缺失 → 成本 0（宁可少算不虚报）
  recordUsage({ session_id: 'sess-2', model: 'unknown-model', input_tokens: 10, output_tokens: 10 });
  const rows = usageBreakdown(now - DAY, now + DAY, 'model');
  const unknown = rows.find((r) => r.key === 'unknown-model');
  check('无单价模型成本按 0 计', unknown?.cost_est_usd === 0);
}

// ── 按天聚合 ─────────────────────────────────────────────────────────────────
{
  const yesterday = now - DAY;
  recordUsage({ session_id: 's3', model: 'm', input_tokens: 100, output_tokens: 1, ts: yesterday });
  const daily = usageDailyBetween(now - 3 * DAY, now + DAY);
  check('按天分组（昨天与今天各一组）', daily.length === 2);
  const todayBucket = daily.find((d) => d.day === startOfDay(now));
  // 到此处今天已落库：1(claude) + 1(非法归零) + 1(unknown-model) + 1(s3 属昨天不算) = 3 条
  check('今天的桶按天累计所有当日流水', todayBucket?.calls === 3);
  const yesterdayBucket = daily.find((d) => d.day === startOfDay(yesterday));
  check('昨天的桶独立累计', yesterdayBucket?.calls === 1 && yesterdayBucket?.input_tokens === 100);
}

// ── Breakdown TopN ───────────────────────────────────────────────────────────
{
  for (let i = 0; i < 5; i++) {
    recordUsage({ session_id: 'bd-a', model: 'big-model', input_tokens: 5000, output_tokens: 100, ts: now });
  }
  recordUsage({ session_id: 'bd-b', model: 'small-model', input_tokens: 10, output_tokens: 10, ts: now });
  const byModel = usageBreakdown(now - DAY, now + DAY, 'model', 10);
  check('TopN 排序按 tokens 降序', byModel[0]?.key === 'big-model');
  const bySession = usageBreakdown(now - DAY, now + DAY, 'session_id', 10);
  check('按 session 分组正确', bySession.some((r) => r.key === 'bd-a'));
}

_resetForTests();
// Windows 下 WAL 句柄释放有延迟，rmSync 可能 EBUSY——清理失败不影响测试结论
try {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
} catch {
  /* ignore */
}

if (failures > 0) {
  console.error(`\n${failures} 个断言失败`);
  process.exit(1);
}
console.log('\ntoken-usage.test: all passed');
