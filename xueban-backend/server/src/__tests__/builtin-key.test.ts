import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveProviderKey } from '../transport/byokKey';
import { _resetForTests } from '../storage/sqlite';

const originalCwd = process.cwd();
let dir = '';
let savedKey: string | undefined;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-env-key-'));
  process.chdir(dir);
  _resetForTests();
  savedKey = process.env.DEEPSEEK_API_KEY;
  delete process.env.DEEPSEEK_API_KEY;
});

afterEach(() => {
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
  if (savedKey === undefined) delete process.env.DEEPSEEK_API_KEY;
  else process.env.DEEPSEEK_API_KEY = savedKey;
});

describe('DeepSeek Key 解析', () => {
  it('没有配置时返回 undefined，不内置硬编码 Key', () => {
    expect(resolveProviderKey('deepseek', {})).toBeUndefined();
  });

  it('环境变量提供 Key', () => {
    process.env.DEEPSEEK_API_KEY = 'sk-env-key';
    expect(resolveProviderKey('deepseek', {})).toBe('sk-env-key');
  });

  it('请求头 Key 优先于环境变量', () => {
    process.env.DEEPSEEK_API_KEY = 'sk-env-key';
    expect(resolveProviderKey('deepseek', { 'x-deepseek-key': 'sk-request-key' })).toBe('sk-request-key');
  });
});
