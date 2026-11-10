import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { recordUsage, sumUsageBetween } from './token-usage';
import { _resetForTests } from './sqlite';

const original = process.cwd();
let directory = '';
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'sf-company-usage-'));
  process.chdir(directory);
  _resetForTests();
});
afterEach(() => {
  _resetForTests();
  process.chdir(original);
  rmSync(directory, { recursive: true, force: true });
});

describe('公司用量与人民币刊例价', () => {
  it('按公司隔离，分别计算缓存输入、普通输入和输出成本', () => {
    recordUsage({ company_id: 'a', model: 'mimo-v2.6-pro', input_tokens: 1_000_000, cached_input_tokens: 100_000, output_tokens: 200_000 });
    recordUsage({ company_id: 'b', model: 'mimo-v2.6-pro', input_tokens: 50, output_tokens: 20 });
    const usage = sumUsageBetween(0, Date.now() + 1000, 'a');
    expect(usage.calls).toBe(1);
    expect(usage.cached_input_tokens).toBe(100_000);
    expect(usage.cost_est_cny).toBeCloseTo(3.9025, 8);
    expect(sumUsageBetween(0, Date.now() + 1000, 'b').cost_est_cny).toBeCloseTo(0.00027, 8);
  });
});
