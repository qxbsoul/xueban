/**
 * byokKey.test.ts — resolveProviderKey 单测(DEBT-1:BYOK 密钥两套存储统一)。
 *
 * 重点验证 2026-06-04 新增的「通用 X-LLM-* 头」来源:浏览器把 localStorage 里配的 key
 * 经 X-LLM-Provider / X-LLM-Key 转发(src/api/chat.ts buildByokHeaders),Node 网关此前
 * 只认 per-provider 头 + 服务端 byok 设置 → 浏览器配的 key 收不到 → 401。
 *
 * 用 mock 隔离服务端 byok 设置(不读真实 settings.json)。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const getSettingMock = vi.fn();
vi.mock('../../storage/settings', () => ({ getSetting: (k: string) => getSettingMock(k) }));

import { resolveProviderKey } from '../byokKey';
import { PROVIDER_ENV_VAR } from '../api-clients';

describe('resolveProviderKey', () => {
  beforeEach(() => {
    getSettingMock.mockReset();
    getSettingMock.mockReturnValue(undefined); // 默认服务端无 byok 设置
  });

  it('per-provider 头(x-anthropic-key)最高优先', () => {
    expect(resolveProviderKey('anthropic', { 'x-anthropic-key': 'sk-a' })).toBe('sk-a');
  });

  it('[新] 通用 X-LLM-Key + X-LLM-Provider=claude → 归一到 anthropic 命中', () => {
    expect(
      resolveProviderKey('anthropic', { 'x-llm-provider': 'claude', 'x-llm-key': 'sk-llm' }),
    ).toBe('sk-llm');
  });

  it('[新] 通用头 provider 名直配(openai)→ 命中', () => {
    expect(
      resolveProviderKey('openai', { 'x-llm-provider': 'openai', 'x-llm-key': 'sk-o' }),
    ).toBe('sk-o');
  });

  it('[新] 通用头 provider 不匹配 → 不误用该 key(回落服务端设置)', () => {
    getSettingMock.mockReturnValue({ providers: { anthropic: { apiKey: 'sk-setting' } } });
    // 请求 anthropic,但通用头声明的是 openai → 不能拿 openai 的 key 顶替
    expect(
      resolveProviderKey('anthropic', { 'x-llm-provider': 'openai', 'x-llm-key': 'sk-o' }),
    ).toBe('sk-setting');
  });

  it('per-provider 头优先于通用头', () => {
    expect(
      resolveProviderKey('anthropic', {
        'x-anthropic-key': 'sk-specific',
        'x-llm-provider': 'claude',
        'x-llm-key': 'sk-generic',
      }),
    ).toBe('sk-specific');
  });

  it('无头 → 读服务端 byok 设置', () => {
    getSettingMock.mockReturnValue({ providers: { anthropic: { apiKey: 'sk-server' } } });
    expect(resolveProviderKey('anthropic', {})).toBe('sk-server');
  });

  it('通用头优先于服务端设置(浏览器配的 key 应盖过陈旧服务端设置)', () => {
    getSettingMock.mockReturnValue({ providers: { anthropic: { apiKey: 'sk-stale' } } });
    expect(
      resolveProviderKey('anthropic', { 'x-llm-provider': 'claude', 'x-llm-key': 'sk-fresh' }),
    ).toBe('sk-fresh');
  });

  it('数组型 header 值(Express 可能给 string[])取第一个', () => {
    expect(
      resolveProviderKey('openai', { 'x-llm-provider': ['openai'], 'x-llm-key': ['sk-arr'] }),
    ).toBe('sk-arr');
  });

  it('全空 → 回落环境变量', () => {
    const envName = PROVIDER_ENV_VAR.anthropic;
    const prev = process.env[envName];
    process.env[envName] = 'sk-env';
    try {
      expect(resolveProviderKey('anthropic', {})).toBe('sk-env');
    } finally {
      if (prev === undefined) delete process.env[envName];
      else process.env[envName] = prev;
    }
  });

  it('啥都没有 → undefined', () => {
    const envName = PROVIDER_ENV_VAR.anthropic;
    const prev = process.env[envName];
    delete process.env[envName];
    try {
      expect(resolveProviderKey('anthropic', {})).toBeUndefined();
    } finally {
      if (prev !== undefined) process.env[envName] = prev;
    }
  });
});
