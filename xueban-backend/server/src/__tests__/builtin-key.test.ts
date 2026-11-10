import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveProviderKey } from '../transport/byokKey';
import { BUILTIN_MIMO_API_KEY } from '../transport/builtin-keys';
import { _resetForTests } from '../storage/sqlite';

const originalCwd = process.cwd();
let dir = '';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-builtin-key-'));
  process.chdir(dir);
  _resetForTests();
  delete process.env.MIMO_API_KEY;
});

afterEach(() => {
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('内置 MiMo 密钥', () => {
  it('什么都没有配置时用内置 Key，未配置 Key 也能直接对话', () => {
    expect(BUILTIN_MIMO_API_KEY.startsWith('sk-')).toBe(true);
    expect(resolveProviderKey('mimo', {})).toBe(BUILTIN_MIMO_API_KEY);
  });

  it('用户自己的 Key 优先于内置 Key', () => {
    expect(resolveProviderKey('mimo', { 'x-mimo-key': 'sk-user-key' })).toBe('sk-user-key');
    process.env.MIMO_API_KEY = 'sk-env-key';
    expect(resolveProviderKey('mimo', {})).toBe('sk-env-key');
  });
});
