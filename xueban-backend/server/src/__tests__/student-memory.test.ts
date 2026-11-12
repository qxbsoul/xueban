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


describe('第二轮可靠性修复', () => {
  it('严格 explicitness：非法值不会默认成 explicit', () => {
    const { user } = seedTurn('我希望先讲思路。');
    const result = service.validateCandidates({
      candidates: [{
        category: 'preference', memory_key: 'teaching.explanation_style', content: '偏好',
        confidence: 0.9, explicitness: 'maybe',
        evidence: [{ message_id: user.id, quote: '先讲思路' }],
      }],
    }, [{ id: user.id, content: user.content }]);
    expect(result).toEqual([]);
  });

  it('纠错先校验：非法 confidence / 空内容不留下版本记录', () => {
    const { conversation, user } = seedTurn('纠错测试。');
    const candidate = store.createCandidate({
      owner_id: 'acc-fix', category: 'preference', memory_key: 'teaching.fix',
      content: '原始内容', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate('acc-fix', candidate.id, { auto: true }).memory!;
    expect(store.correctMemory('acc-fix', memory.id, { content: '', reason: '空' })).toBeUndefined();
    expect(store.correctMemory('acc-fix', memory.id, { content: '新内容', confidence: 1.4 })).toBeUndefined();
    expect(store.memoryVersions('acc-fix', memory.id)).toHaveLength(0);
    expect(store.getMemory('acc-fix', memory.id)?.content).toBe('原始内容');
  });

  it('任务重试不会重复造候选：同 key 同内容只保留一条', () => {
    const { conversation, user } = seedTurn('重复候选测试。');
    const input = {
      owner_id: 'acc-dup', category: 'preference' as const, memory_key: 'teaching.dup',
      content: '同一条候选', confidence: 0.7, explicitness: 'inferred' as const,
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    };
    const first = store.createCandidate(input);
    const second = store.createCandidate(input);
    expect(second.id).toBe(first.id);
    expect(store.listCandidates('acc-dup', 'pending')).toHaveLength(1);
  });

  it('自动冲突标记 conflict 状态，确认后可以正常更新', () => {
    const { conversation, user } = seedTurn('旧偏好：直接给答案。');
    const make = (content: string, quote: string) => store.createCandidate({
      owner_id: 'acc-conflict', category: 'preference', memory_key: 'teaching.style',
      content, confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote, conversation_id: conversation.id }],
    });
    const first = make('喜欢直接给答案', user.content);
    store.acceptCandidate('acc-conflict', first.id, { auto: true });
    const second = make('现在希望先讲思路再给答案', user.content);
    expect(store.acceptCandidate('acc-conflict', second.id, { auto: true }).action).toBe('conflict');
    expect(store.getCandidate('acc-conflict', second.id)?.status).toBe('conflict');
    const accepted = store.acceptCandidate('acc-conflict', second.id, { changedBy: 'manual' });
    expect(accepted.action).toBe('updated');
    expect(store.getCandidate('acc-conflict', second.id)?.status).toBe('accepted');
  });

  it('租约：正常 running 不被抢占，过期 running 可以恢复', () => {
    const { conversation, user, assistant } = seedTurn('租约测试。');
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-lease', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    expect(store.markJobRunning(jobId)).toBe(true);
    expect(store.markJobRunning(jobId)).toBe(false);
    expect(store.pendingJobs(5, 'acc-lease').some((job) => job.id === jobId)).toBe(false);

    sqlite.getDb().prepare('UPDATE student_memory_jobs SET started_at = ? WHERE id = ?')
      .run(Date.now() - 11 * 60_000, jobId);
    expect(store.pendingJobs(5, 'acc-lease').some((job) => job.id === jobId)).toBe(true);
    expect(store.markJobRunning(jobId)).toBe(true);
  });

  it('没有可用 Key 时任务进入 waiting_credentials，不消耗重试次数', async () => {
    const saved = process.env.DEEPSEEK_API_KEY;
    delete process.env.DEEPSEEK_API_KEY;
    const { conversation, user, assistant } = seedTurn('等待凭证。');
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-key', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    expect(await service.processMemoryJob(jobId)).toBe(false);
    const job = store.getJob(jobId)!;
    expect(job.status).toBe('waiting_credentials');
    expect(job.attempts).toBe(0);
    if (saved === undefined) delete process.env.DEEPSEEK_API_KEY;
    else process.env.DEEPSEEK_API_KEY = saved;
  });

  it('提取 Prompt 提供真实 message_id 与历史学生消息', async () => {
    const conversation = chat.createXuebanConversation('co-1', 'single', '历史消息');
    const oldMessage = chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'user', sender_name: '学生', content: '我以前喜欢直接看答案。', status: 'done',
    });
    const current = chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'user', sender_name: '学生', content: '现在我更想先讲思路。', status: 'done',
    });
    const assistant = chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: '好的。', status: 'done',
    });
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-prompt', conversation_id: conversation.id,
      user_message_id: current.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    let prompt = '';
    const fake = async function* (_provider: string, input: { userMessage: string }) {
      prompt = input.userMessage;
      yield { type: 'text-delta' as const, text: '{"candidates":[]}' };
    };
    const job = store.getJob(jobId)!;
    await service.extractCandidatesForJob(job, { apiKey: 'k', callImpl: fake as never });
    expect(prompt).toContain(`message_id=${current.id}`);
    expect(prompt).toContain(`message_id=${oldMessage.id}`);
    expect(prompt).toContain('只能引用这些 ID');
  });

  it('LLM 提取失败不影响已经记录的学习事件，事件可幂等重记', async () => {
    const { conversation, user, assistant } = seedTurn('提取会失败，但事件要留下。');
    store.recordLearningEvent({
      owner_id: 'acc-event', conversation_id: conversation.id, message_id: user.id,
      content: user.content, subject: '数学',
    });
    const duplicate = store.recordLearningEvent({
      owner_id: 'acc-event', conversation_id: conversation.id, message_id: user.id,
      content: user.content, subject: '数学',
    });
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-event', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    const failing = async function* (): AsyncGenerator<ProviderChunk> {
      throw new Error('model down');
    };
    const job = store.getJob(jobId)!;
    await expect(service.extractCandidatesForJob(job, { apiKey: 'k', callImpl: failing as never }))
      .rejects.toThrow('model down');
    const events = store.listLearningEvents('acc-event');
    expect(events).toHaveLength(1);
    expect(duplicate).toBe(events[0].id);
  });

  it('召回按 Prompt 行文本限长，超长记忆不会硬塞', () => {
    const { conversation, user } = seedTurn('长记忆。');
    const candidate = store.createCandidate({
      owner_id: 'acc-char', category: 'learning_context', memory_key: 'subject.math.context.long',
      content: '这是一段很长的记忆内容'.repeat(5), confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    store.acceptCandidate('acc-char', candidate.id, { auto: true });
    expect(store.recallMemories({ owner_id: 'acc-char', subject: '数学', charLimit: 10 })).toHaveLength(0);
  });
});
