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
    const extracted = memories.find((m) => m.memory_key === 'subject.math.weakness.function_domain')!;
    expect(store.memoryEvidence('acc-d', extracted.id)[0]?.conversation_id).toBe(conversation.id);
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


describe('第三轮边界修复', () => {
  it('冲突候选可以被拒绝', () => {
    const { conversation, user } = seedTurn('冲突拒绝。');
    const make = (content: string) => store.createCandidate({
      owner_id: 'acc-reject', category: 'preference', memory_key: 'teaching.style',
      content, confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const first = make('旧说法');
    store.acceptCandidate('acc-reject', first.id, { auto: true });
    const second = make('新说法');
    store.acceptCandidate('acc-reject', second.id, { auto: true });
    expect(store.getCandidate('acc-reject', second.id)?.status).toBe('conflict');
    expect(store.rejectCandidate('acc-reject', second.id, '不接受')).toBe(true);
    expect(store.getCandidate('acc-reject', second.id)?.status).toBe('rejected');
  });

  it('归档后再次明确表达会重新激活并保留版本', () => {
    const { conversation, user } = seedTurn('重新激活。');
    const candidate = store.createCandidate({
      owner_id: 'acc-react', category: 'preference', memory_key: 'teaching.react',
      content: '重新激活的偏好', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate('acc-react', candidate.id, { auto: true }).memory!;
    expect(store.archiveMemory('acc-react', memory.id)).toBe(true);
    const reactivated = store.reactivateMemory('acc-react', memory.id, '学生再次表达')!;
    expect(reactivated.status).toBe('active');
    expect(reactivated.version).toBe(2);
    expect(store.memoryVersions('acc-react', memory.id)).toHaveLength(2);
    expect(store.memoryVersions('acc-react', memory.id).at(-1)?.reason).toContain('再次表达');
  });
});


describe('Outbox 超时、失败与清理', () => {
  function seedOutbox(owner: string, content = '测试轮次') {
    const conversation = chat.createXuebanConversation('co-1', 'single', 'outbox');
    const user = chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'user', sender_name: '学生', content, status: 'done',
    });
    const id = store.createMemoryOutbox({
      owner_id: owner, conversation_id: conversation.id, user_message_id: user.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    return { conversation, user, id };
  }

  it('明确失败的轮次标记 abandoned', () => {
    const { conversation, id } = seedOutbox('acc-abandon');
    chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: '本轮失败', status: 'failed',
    });
    service.recoverMemoryOutbox();
    expect(store.listMemoryOutbox('acc-abandon').find((row) => row.id === id)?.status).toBe('abandoned');
  });

  it('等待审批的轮次不会被放弃', () => {
    const { conversation, id } = seedOutbox('acc-approval');
    chat.appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: '需要你确认', status: 'pending',
    });
    service.recoverMemoryOutbox();
    expect(store.listMemoryOutbox('acc-approval').find((row) => row.id === id)?.status).toBe('awaiting_assistant');
  });

  it('长时间无最终回答且无活动任务 → abandoned；可显式恢复', () => {
    const { id } = seedOutbox('acc-timeout');
    sqlite.getDb().prepare('UPDATE student_memory_outbox SET updated_at = ? WHERE id = ?')
      .run(Date.now() - 25 * 60 * 60_000, id);
    service.recoverMemoryOutbox();
    expect(store.listMemoryOutbox('acc-timeout')[0].status).toBe('abandoned');

    expect(store.restoreOutbox('acc-timeout', id)).toBe(true);
    expect(store.listMemoryOutbox('acc-timeout')[0].status).toBe('awaiting_assistant');
  });

  it('done 记录按时间清理，不影响事件与正式记忆', () => {
    const { conversation, user } = seedTurn('清理测试。');
    store.recordLearningEvent({
      owner_id: 'acc-cleanup', conversation_id: conversation.id, message_id: user.id,
      content: user.content, subject: '数学',
    });
    const candidate = store.createCandidate({
      owner_id: 'acc-cleanup', category: 'preference', memory_key: 'teaching.cleanup',
      content: '保留的记忆', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    store.acceptCandidate('acc-cleanup', candidate.id, { auto: true });

    const oldId = store.createMemoryOutbox({
      owner_id: 'acc-cleanup', conversation_id: conversation.id, user_message_id: user.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    store.markOutboxDispatched('acc-cleanup', conversation.id, user.id, 'a-1');
    sqlite.getDb().prepare('UPDATE student_memory_outbox SET updated_at = ? WHERE id = ?')
      .run(Date.now() - 100 * 86_400_000, oldId);

    const removed = store.cleanupMemoryOutbox(90);
    expect(removed).toBeGreaterThanOrEqual(1);
    expect(store.listMemoryOutbox('acc-cleanup')).toHaveLength(0);
    expect(store.listLearningEvents('acc-cleanup')).toHaveLength(1);
    expect(store.listMemories({ owner_id: 'acc-cleanup' })).toHaveLength(1);
  });
});


describe('编辑版本与永久删除', () => {
  it('旧 revision 的任务会被作废，不覆盖新编辑结果', async () => {
    const { conversation, user, assistant } = seedTurn('旧版本内容。');
    const jobId = store.enqueueMemoryJob({
      owner_id: 'acc-rev', conversation_id: conversation.id,
      user_message_id: user.id, assistant_message_id: assistant.id,
      subject: '数学', provider: 'deepseek', model: 'x', turn_revision: 1,
    });
    store.createMemoryOutbox({
      owner_id: 'acc-rev', conversation_id: conversation.id, user_message_id: user.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    sqlite.getDb().prepare(`UPDATE student_memory_outbox SET turn_revision = 2 WHERE owner_id = ? AND conversation_id = ? AND user_message_id = ?`)
      .run('acc-rev', conversation.id, user.id);
    expect(await service.processMemoryJob(jobId, { apiKey: 'k' })).toBe(false);
    expect(store.getJob(jobId)?.status).toBe('superseded');
  });

  it('永久删除清理证据且不再被召回，也不会被提取重建', () => {
    const { conversation, user } = seedTurn('永久删除测试。');
    const candidate = store.createCandidate({
      owner_id: 'acc-delete', category: 'preference', memory_key: 'teaching.deleted',
      content: '删除我', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate('acc-delete', candidate.id, { auto: true }).memory!;
    expect(store.deleteMemoryPermanently('acc-delete', memory.id)).toBe(true);
    expect(store.getMemory('acc-delete', memory.id)?.status).toBe('deleted');
    expect(store.memoryEvidence('acc-delete', memory.id)).toHaveLength(0);
    expect(store.recallMemories({ owner_id: 'acc-delete' })).toHaveLength(0);
    expect(store.findMemoryByKey('acc-delete', 'teaching.deleted')?.status).toBe('deleted');
  });

  it('“不再学习”保留墓碑并阻止重组建', () => {
    const { conversation, user } = seedTurn('不再学习测试。');
    const candidate = store.createCandidate({
      owner_id: 'acc-block', category: 'preference', memory_key: 'teaching.blocked',
      content: '不要记住', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate('acc-block', candidate.id, { auto: true }).memory!;
    expect(store.blockMemory('acc-block', memory.id)).toBe(true);
    expect(store.getMemory('acc-block', memory.id)?.status).toBe('blocked');
    expect(store.recallMemories({ owner_id: 'acc-block' })).toHaveLength(0);
  });
});


describe('墓碑不可逆与 Outbox 审计', () => {
  it('deleted 记忆不能被自动或人工接受复活', () => {
    const { conversation, user } = seedTurn('删除后不可复活。');
    const make = (content: string) => store.createCandidate({
      owner_id: 'acc-tomb', category: 'preference', memory_key: 'teaching.tomb',
      content, confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const first = make('原始内容');
    const memory = store.acceptCandidate('acc-tomb', first.id, { auto: true }).memory!;
    expect(store.deleteMemoryPermanently('acc-tomb', memory.id)).toBe(true);

    const auto = make('自动提取的新内容');
    expect(store.acceptCandidate('acc-tomb', auto.id, { auto: true }).action).toBe('rejected');
    expect(store.getCandidate('acc-tomb', auto.id)?.status).toBe('rejected');

    const manual = make('人工接受的新内容');
    expect(store.acceptCandidate('acc-tomb', manual.id, { changedBy: 'manual' }).action).toBe('rejected');
    expect(store.getMemory('acc-tomb', memory.id)?.status).toBe('deleted');
  });

  it('blocked 记忆不能被人工接受复活，且清理版本与旧候选', () => {
    const { conversation, user } = seedTurn('拉黑后不可复活。');
    const make = (content: string) => store.createCandidate({
      owner_id: 'acc-block2', category: 'preference', memory_key: 'teaching.block2',
      content, confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const first = make('旧内容');
    const memory = store.acceptCandidate('acc-block2', first.id, { auto: true }).memory!;
    store.correctMemory('acc-block2', memory.id, { content: '修改后的内容', reason: '测试版本' });
    const pending = make('待审内容');
    expect(store.blockMemory('acc-block2', memory.id)).toBe(true);
    expect(store.memoryVersions('acc-block2', memory.id)).toHaveLength(0);
    expect(store.getCandidate('acc-block2', pending.id)?.status).toBe('rejected');

    const manual = make('再次人工接受');
    expect(store.acceptCandidate('acc-block2', manual.id, { changedBy: 'manual' }).action).toBe('rejected');
    expect(store.getMemory('acc-block2', memory.id)?.status).toBe('blocked');
  });

  it('Outbox 恢复会重置 run_state 并记录审计信息', () => {
    const { conversation, user } = seedTurn('Outbox 恢复审计。');
    const id = store.createMemoryOutbox({
      owner_id: 'acc-audit', conversation_id: conversation.id, user_message_id: user.id,
      subject: '数学', provider: 'deepseek', model: 'x',
    });
    expect(store.markOutboxAbandoned('acc-audit', id, '轮次失败')).toBe(true);
    let row = store.listMemoryOutbox('acc-audit')[0];
    expect(row.status).toBe('abandoned');
    expect(row.abandoned_reason).toBe('轮次失败');
    expect(row.abandoned_at).toBeTruthy();

    expect(store.restoreOutbox('acc-audit', id)).toBe(true);
    row = store.listMemoryOutbox('acc-audit')[0];
    expect(row.status).toBe('awaiting_assistant');
    expect(row.run_state).toBe('running');
    expect(row.restore_count).toBe(1);
    expect(row.restored_at).toBeTruthy();
  });
});


describe('归档记忆的重新激活规则', () => {
  function archivedMemory(owner: string) {
    const { conversation, user } = seedTurn('归档规则测试。');
    const candidate = store.createCandidate({
      owner_id: owner, category: 'preference', memory_key: `teaching.archived.${owner}`,
      content: '先讲思路再给答案', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate(owner, candidate.id, { auto: true }).memory!;
    store.archiveMemory(owner, memory.id);
    return { conversation, user, memory };
  }

  it('自动提取相同内容不会复活 archived，候选保持 pending', () => {
    const owner = 'acc-arch-auto';
    const { conversation, user, memory } = archivedMemory(owner);
    const candidate = store.createCandidate({
      owner_id: owner, category: 'preference', memory_key: memory.memory_key,
      content: '先讲思路再给答案', confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const result = store.acceptCandidate(owner, candidate.id, { auto: true });
    expect(result.action).toBe('pending');
    expect(store.getMemory(owner, memory.id)?.status).toBe('archived');
    expect(store.getCandidate(owner, candidate.id)?.status).toBe('pending');
  });

  it('学生人工确认后才重新激活 archived，并保留版本', () => {
    const owner = 'acc-arch-manual';
    const { conversation, user, memory } = archivedMemory(owner);
    const candidate = store.createCandidate({
      owner_id: owner, category: 'preference', memory_key: memory.memory_key,
      content: '先讲思路再给答案', confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const result = store.acceptCandidate(owner, candidate.id, { changedBy: 'manual' });
    expect(result.action).toBe('merged');
    expect(store.getMemory(owner, memory.id)?.status).toBe('active');
    expect(store.memoryVersions(owner, memory.id).length).toBeGreaterThanOrEqual(1);
  });
});

describe('分页、搜索与统计', () => {
  it('offset 分页不重复，q 过滤生效，统计不是当前页数量', () => {
    for (let i = 0; i < 7; i += 1) {
      const { user, conversation } = seedTurn(`分页记忆 ${i} 关于三角函数。`);
      const candidate = store.createCandidate({
        owner_id: 'acc-page', category: 'weakness', subject: i % 2 === 0 ? '数学' : '英语',
        memory_key: `subject.page.${i}`, content: `分页记忆第 ${i} 条内容`,
        confidence: 0.9, explicitness: 'explicit',
        evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
      });
      store.acceptCandidate('acc-page', candidate.id, { auto: true });
    }
    const first = store.listMemories({ owner_id: 'acc-page', limit: 3, offset: 0 });
    const second = store.listMemories({ owner_id: 'acc-page', limit: 3, offset: 3 });
    expect(first).toHaveLength(3);
    expect(second).toHaveLength(3);
    expect(first.map((x) => x.id).filter((id) => second.some((x) => x.id === id))).toHaveLength(0);

    const search = store.listMemories({ owner_id: 'acc-page', query: '第 5 条' });
    expect(search).toHaveLength(1);
    const stats = store.memoryStats('acc-page');
    expect(stats.active).toBe(7);
    expect(stats.by_category.weakness).toBe(7);
    expect(stats.latest_update).toBeGreaterThan(0);
  });
});


describe('归档后出现不同的新偏好', () => {
  it('人工确认新候选会更新原记忆并重新激活，旧版本保留', () => {
    const { conversation, user } = seedTurn('旧偏好：喜欢直接看答案。');
    const oldCandidate = store.createCandidate({
      owner_id: 'acc-arch-new', category: 'preference', memory_key: 'teaching.style',
      content: '喜欢直接看答案', confidence: 0.9, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const memory = store.acceptCandidate('acc-arch-new', oldCandidate.id, { auto: true }).memory!;
    store.archiveMemory('acc-arch-new', memory.id);

    const newCandidate = store.createCandidate({
      owner_id: 'acc-arch-new', category: 'preference', memory_key: 'teaching.style',
      content: '现在希望先看提示，再自己思考', confidence: 0.95, explicitness: 'explicit',
      evidence: [{ message_id: user.id, quote: user.content, conversation_id: conversation.id }],
    });
    const result = store.acceptCandidate('acc-arch-new', newCandidate.id, { changedBy: 'manual' });
    expect(result.action).toBe('updated');
    const updated = store.getMemory('acc-arch-new', memory.id)!;
    expect(updated.status).toBe('active');
    expect(updated.content).toBe('现在希望先看提示，再自己思考');
    expect(updated.version).toBeGreaterThan(1);
    expect(store.memoryVersions('acc-arch-new', memory.id).length).toBeGreaterThanOrEqual(2);
  });
});
