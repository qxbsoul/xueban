/**
 * 学生长期记忆测试（2026-11-12）。
 * 覆盖：事件/正式记忆/证据/版本、账号隔离、召回、候选自动接收、提取校验。
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { ProviderChunk } from '../transport/api-clients';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let store: typeof import('../storage/student-memory');
let chat: typeof import('../storage/xueban-chat');
let service: typeof import('../services/student-memory');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-student-memory-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  chat = await import('../storage/xueban-chat');
  store = await import('../storage/student-memory');
  service = await import('../services/student-memory');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

function seedTurn(content: string, answer = '好的，我们先讲思路。') {
  const conversation = chat.createXuebanConversation('co-1', 'single', '记忆测试');
  const user = chat.appendXuebanMessage(conversation.id, {
    sender_kind: 'user', sender_name: '学生', content, status: 'done',
  });
  const assistant = chat.appendXuebanMessage(conversation.id, {
    sender_kind: 'ceo', sender_name: '教学主管', content: answer, status: 'done',
  });
  return { conversation, user, assistant };
}

describe('存储与版本', () => {
  it('候选创建 / 自动接收 / 证据 / 版本 / 归档', () => {
    const { conversation, user } = seedTurn('我希望先讲思路再给答案。');
    const eventId = store.recordLearningEvent({
      owner_id: 'acc-a', conversation_id: conversation.id, message_id: user.id, content: user.content,
    });
    expect(eventId).toBeTruthy();

    const candidate = store.createCandidate({
      owner_id: 'acc-a', category: 'preference', memory_key: 'teaching.explanation_style',
      title: '讲题偏好', content: '学生希望先讲思路，再给答案。', confidence: 0.95,
      explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const accepted = store.acceptCandidate('acc-a', candidate.id, { changedBy: 'system' });
    expect(accepted.action).toBe('created');
    expect(accepted.memory?.status).toBe('active');
    expect(store.memoryEvidence('acc-a', accepted.memory!.id)).toHaveLength(1);

    const corrected = store.correctMemory('acc-a', accepted.memory!.id, {
      content: '学生希望先讲思路，再给提示，最后才给完整答案。', reason: '学生纠正',
    });
    expect(corrected?.version).toBe(2);
    expect(store.memoryVersions('acc-a', accepted.memory!.id)).toHaveLength(1);

    expect(store.archiveMemory('acc-a', accepted.memory!.id)).toBe(true);
    expect(store.recallMemories({ owner_id: 'acc-a', query: '讲题' })).toHaveLength(0);
    expect(store.memoryEvidence('acc-a', accepted.memory!.id)).toHaveLength(1);
  });

  it('同 key 同内容合并证据，不新增记忆', () => {
    const { conversation, user } = seedTurn('讲题先讲思路。');
    const base = {
      owner_id: 'acc-b' as const, category: 'preference' as const, memory_key: 'teaching.explanation_style',
      content: '学生希望先讲思路再给答案。', confidence: 0.9, explicitness: 'explicit' as const,
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    };
    const first = store.createCandidate(base);
    store.acceptCandidate('acc-b', first.id, { auto: true });
    const second = store.createCandidate({ ...base, confidence: 0.92 });
    const merged = store.acceptCandidate('acc-b', second.id, { auto: true });
    expect(merged.action).toBe('merged');
    expect(store.listMemories({ owner_id: 'acc-b' })).toHaveLength(1);
  });

  it('账号隔离：A 的记忆 B 读不到', () => {
    const memory = store.listMemories({ owner_id: 'acc-a', status: 'archived' })[0];
    expect(store.getMemory('acc-b', memory.id)).toBeUndefined();
    expect(store.listMemories({ owner_id: 'acc-b' }).some((m) => m.id === memory.id)).toBe(false);
  });
});

describe('召回', () => {
  it('按学科与关键词召回，并排除不相关学科', () => {
    const { conversation, user } = seedTurn('数学函数定义域总是搞错。');
    const math = store.createCandidate({
      owner_id: 'acc-c', category: 'weakness', subject: '数学', topic: '函数定义域',
      memory_key: 'subject.math.weakness.function_domain',
      content: '函数定义域判断容易出错。', confidence: 0.8, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    store.acceptCandidate('acc-c', math.id, { auto: true });

    const hit = store.recallMemories({ owner_id: 'acc-c', subject: '数学', query: '函数定义域怎么求' });
    expect(hit.map((x) => x.memory.memory_key)).toContain('subject.math.weakness.function_domain');

    const miss = store.recallMemories({ owner_id: 'acc-c', subject: '英语', query: '作文怎么写' });
    expect(miss.map((x) => x.memory.memory_key)).not.toContain('subject.math.weakness.function_domain');

    expect(store.buildMemoryBlock({ owner_id: 'acc-c', subject: '数学', query: '函数' })).toContain('函数定义域');
    expect(store.buildMemoryBlock({ owner_id: 'acc-other', subject: '数学' })).toBe('');
  });
});

describe('提取校验与自动接收', () => {
  it('拒绝伪造证据 / 助手消息证据 / 非法类别，接受真实学生原话', () => {
    const { user, assistant } = seedTurn('我希望先讲思路，不要直接给答案。');
    const messages = [
      { id: user.id, content: user.content },
      { id: assistant.id, content: assistant.content },
    ];
    const candidates = service.validateCandidates({
      candidates: [
        {
          category: 'preference', memory_key: 'teaching.explanation_style', content: '先讲思路再给答案',
          confidence: 0.95, explicitness: 'explicit',
          evidence: [{ message_id: user.id, quote: '先讲思路' }],
        },
        {
          category: 'preference', memory_key: 'teaching.bad_quote', content: '伪造引用',
          confidence: 0.95, explicitness: 'explicit',
          evidence: [{ message_id: user.id, quote: '这句话不在原消息里' }],
        },
        {
          category: 'preference', memory_key: 'teaching.assistant_quote', content: '助手内容',
          confidence: 0.95, explicitness: 'explicit',
          evidence: [{ message_id: assistant.id, quote: '好的，我们先讲思路' }],
        },
        {
          category: 'not_allowed', memory_key: 'x.bad_category', content: '非法类别',
          confidence: 0.9, explicitness: 'explicit',
          evidence: [{ message_id: user.id, quote: '先讲思路' }],
        },
      ],
    }, [{ id: user.id, content: user.content }]); // 只允许学生消息作为证据
    expect(candidates).toHaveLength(1);
    expect(candidates[0].memory_key).toBe('teaching.explanation_style');
  });

  it('端到端提取：explicit 自动落库，inferred 证据不足留 pending，任务幂等', async () => {
    const { conversation, user, assistant } = seedTurn('我数学函数定义域总是错。', '我们先看定义域。');
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-d', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'test',
    });
    // 同一条 assistant 消息重复入队不会新增任务
    expect(store.enqueueMemoryJob({
      owner_id: 'acc-d', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'test',
    })).toBe(jobId);

    const fake = async function* (): AsyncGenerator<ProviderChunk> {
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          candidates: [
            {
              category: 'weakness', subject: '数学', topic: '函数定义域',
              memory_key: 'subject.math.weakness.function_domain',
              content: '学生自述函数定义域容易出错。', confidence: 0.9, explicitness: 'explicit',
              evidence: [{ message_id: user.id, quote: '函数定义域总是错' }],
            },
            {
              category: 'preference', memory_key: 'teaching.pace',
              content: '学生可能喜欢更慢的节奏。', confidence: 0.7, explicitness: 'inferred',
              evidence: [{ message_id: user.id, quote: '函数定义域总是错' }],
            },
          ],
        }),
      };
      yield { type: 'end' };
    };

    const job = store.getJob(jobId)!;
    const result = await service.extractCandidatesForJob(job, { apiKey: 'test-key', callImpl: fake as never });
    expect(result.created).toBe(2);
    expect(result.autoAccepted).toBe(1);
    expect(result.pending).toBe(1);

    const memories = store.listMemories({ owner_id: 'acc-d' });
    expect(memories.map((m) => m.memory_key)).toContain('subject.math.weakness.function_domain');
    expect(memories.map((m) => m.memory_key)).not.toContain('teaching.pace');

    const candidates = store.listCandidates('acc-d', 'pending');
    expect(candidates.map((c) => c.memory_key)).toContain('teaching.pace');
  });
});
