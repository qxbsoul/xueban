import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ProviderInput } from '../transport/api-clients';
import { _resetForTests } from '../storage/sqlite';
import { createXuebanConversation } from '../storage/xueban-chat';
import { runXuebanChatTurn } from '../services/xueban-chat';
import { executeXuebanTool, xuebanToolsForEmployee } from '../services/xueban-tools';

const originalCwd = process.cwd();
let dir = '';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-tools-'));
  process.chdir(dir);
  _resetForTests();
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/api/xueban/auth/me')) return Response.json({ company: { id: 'co1', name: '测试公司' } });
    return Response.json({ items: [{ id: 'e1', name: '研究员', title: '市场研究', tools: ['网页检索', 'read_file'], status: 'idle' }] });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('XUEBAN 原生工具循环', () => {
  it('把真实工具结果回传模型，再保存最终答案与 token 用量', async () => {
    mkdirSync(join(dir, '.xueban', 'xueban-workspaces', 'co1'), { recursive: true });
    writeFileSync(join(dir, '.xueban', 'xueban-workspaces', 'co1', 'evidence.txt'), '真实资料：市场增长 18%', 'utf8');
    const inputs: ProviderInput[] = [];
    const callImpl = async function* (_provider: string, input: ProviderInput) {
      inputs.push(input);
      if (input.systemPrompt.includes('编排者')) {
        yield { type: 'text-delta' as const, text: JSON.stringify({ intent: 'task', strategy: 'single', tasks: [{ agentId: 'e1', title: '读取资料', prompt: '读取资料并报告' }] }) };
      } else if (input.systemPrompt.includes('市场研究') && !input.messages?.some(message => message.role === 'tool')) {
        expect(input.tools?.map(tool => tool.name)).toEqual(['read_file', 'web_search']);
        yield { type: 'tool-use' as const, id: 'call-1', name: 'read_file', input: { path: 'evidence.txt' } };
      } else {
        yield { type: 'text-delta' as const, text: '基于真实资料，市场增长 18%' };
        yield { type: 'usage' as const, usage: { input_tokens: 31, output_tokens: 12 } };
      }
    };
    const conversation = createXuebanConversation('co1', 'single');
    const activities: Array<{ name: string; status: string }> = [];
    const result = await runXuebanChatTurn({
      conversationId: conversation.id, companyId: 'co1', content: '读取资料并报告', authorization: 'Bearer test',
      config: { provider: 'openai', model: 'test', apiKey: 'test' }, callImpl,
      onEvent: event => { if (event.type === 'activity' && event.activity.name === 'read_file') activities.push(event.activity); },
    });
    expect(result.messages.at(-1)?.content).toBe('基于真实资料，市场增长 18%');
    expect(activities.map(item => item.status)).toEqual(['running', 'done']);
    const toolRound = [...inputs].reverse().find(item => item.messages?.some(message => message.role === 'tool'));
    expect(JSON.stringify(toolRound?.messages)).toContain('市场增长 18%');
    expect(JSON.stringify(toolRound?.messages)).toContain('"is_error":false');
  });

  it('未配置搜索 Key 时用公开网页兜底，且不伪造结果', async () => {
    // 无 Key 时走 Bing 公开结果兜底；这里用脚本化响应验证解析链路，
    // 不访问真实网络，也不返回硬编码的假结果。
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      '<ol><li class="b_algo"><h2><a href="https://example.com/x">示例</a></h2><p>摘要内容</p></li></ol>',
      { status: 200, headers: { 'content-type': 'text/html' } },
    )));
    const result = await executeXuebanTool({ companyId: 'co1', name: 'web_search', input: { query: 'test' }, env: {} });
    expect(result.isError).toBe(false);
    const output = result.output as { provider: string; count: number; results: Array<{ url: string }> };
    expect(output.provider).toBe('bing-html');
    expect(output.count).toBe(1);
    expect(output.results[0].url).toBe('https://example.com/x');
  });

  it('搜索上游不可达时如实报错，不编造结果', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));
    const result = await executeXuebanTool({ companyId: 'co1', name: 'web_search', input: { query: 'test' }, env: {} });
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result.output)).toContain('web_search failed');
  });

  it('岗位权限拒绝 shell 等高风险工具', async () => {
    expect(xuebanToolsForEmployee(['shell_exec', 'write_file', '网页检索']).map(tool => tool.name))
      .toEqual(['web_search']);
    const result = await executeXuebanTool({ companyId: 'co1', name: 'shell_exec', input: { command: 'whoami' } });
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result.output)).toContain('未执行');
  });
});
