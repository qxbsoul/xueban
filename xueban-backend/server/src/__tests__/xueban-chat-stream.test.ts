import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ThinkingDeltaParser, type XuebanChatStreamEvent } from '../services/xueban-chat-stream';
import { runXuebanChatTurn } from '../services/xueban-chat';
import { createXuebanConversation, listXuebanMessages } from '../storage/xueban-chat';
import { _resetForTests } from '../storage/sqlite';

const originalCwd = process.cwd();
let dir = '';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-stream-'));
  process.chdir(dir);
  _resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('工作台流式思考拆分', () => {
  it('标签跨 chunk 时仍分别输出思考与答案', () => {
    const events: XuebanChatStreamEvent[] = [];
    const parser = new ThinkingDeltaParser('client-1', event => events.push(event));
    for (const chunk of ['<sf:th', 'inking>先分析', '目标</sf:think', 'ing>正式', '答案']) parser.push(chunk);
    expect(parser.finish()).toBe('正式答案');
    expect(events.filter(event => event.type === 'thinking-delta').map(event => event.text).join('')).toBe('先分析目标');
    expect(events.filter(event => event.type === 'content-delta').map(event => event.text)).toEqual(['正式', '答案']);
  });

  it('单聊逐条发送思考和答案事件，数据库只保存正式答案', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ company: { id: 'co1', name: '测试公司' } })));
    const events: XuebanChatStreamEvent[] = [];
    const conversation = createXuebanConversation('co1', 'single');
    const callImpl = async function* () {
      yield { type: 'text-delta' as const, text: '<sf:thinking>内部推理</sf:thinking>' };
      yield { type: 'text-delta' as const, text: '可见答案' };
    };
    await runXuebanChatTurn({
      conversationId: conversation.id,
      companyId: 'co1',
      content: '你好',
      config: { provider: 'mimo', model: 'mimo-v2.6-pro', apiKey: 'test' },
      authorization: 'Bearer test',
      callImpl,
      onEvent: event => events.push(event),
    });
    expect(events.map(event => event.type)).toEqual(['message-start', 'thinking-delta', 'content-delta', 'message-end']);
    expect(events.filter(event => event.type === 'thinking-delta').map(event => event.text)).toEqual(['内部推理']);
    expect(listXuebanMessages(conversation.id).map(message => message.content)).toEqual(['你好', '可见答案']);
    expect(listXuebanMessages(conversation.id)[1]).toMatchObject({ thinking: '内部推理', status: 'done' });
    expect(listXuebanMessages(conversation.id)[0].run_id).toBe(listXuebanMessages(conversation.id)[1].run_id);
  });
});
