/**
 * student-memory service — 增量提取 + 校验 + 自动接收 + 后台任务（2026-11-12）。
 *
 * 一轮聊天结束后入队一个提取任务（幂等键：owner + conversation + assistant message）。
 * 任务失败只记录错误，不影响聊天；可重试；服务重启后 pending/failed(<3) 可补跑。
 */
import { callProvider, type ProviderChunk, type ProviderId } from '../transport/api-clients';
import { resolveProviderKey } from '../transport/byokKey';
import { listXuebanMessages } from '../storage/xueban-chat';
import {
  MEMORY_CATEGORIES,
  acceptCandidate,
  attachEvidenceToMemory,
  createCandidate,
  enqueueMemoryJob,
  failJob,
  findMemoryByKey,
  finishJob,
  getJob,
  listMemories,
  markJobRunning,
  markJobWaitingCredentials,
  pendingJobs,
  type MemoryCategory,
  type MemoryJob,
} from '../storage/student-memory';

const CATEGORY_SET = new Set<string>(MEMORY_CATEGORIES);
const SUBJECTS = ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治', '科学'];
const MAX_CANDIDATES = 8;
const MAX_CONTENT = 400;
const MAX_TITLE = 60;
const KEY_RE = /^[a-z0-9][a-z0-9_.-]{2,63}$/;

export function detectSubject(text: string): string {
  for (const subject of SUBJECTS) {
    if (text.includes(subject)) return subject;
  }
  if (/函数|方程|几何|代数|分数|小数/.test(text)) return '数学';
  if (/作文|阅读|文言文|古诗/.test(text)) return '语文';
  if (/单词|语法|听力|阅读理解/.test(text)) return '英语';
  return '';
}

export interface ExtractionCandidate {
  category: string;
  subject?: string;
  topic?: string;
  memory_key: string;
  title?: string;
  content: string;
  confidence: number;
  explicitness: 'explicit' | 'inferred';
  evidence: Array<{ message_id: string; quote: string }>;
  suggested_action?: 'create' | 'update';
}

function normalize(value: string): string {
  return (value || '').replace(/[\s，。、！？；：,.!?;:（）()「」"']/g, '').toLowerCase();
}

function textOf(chunks: string[]): string {
  return chunks.join('');
}

function parseJson(text: string): unknown {
  const trimmed = text.trim().replace(/^```(?:json)?/i, '').replace(/```$/, '').trim();
  const start = trimmed.indexOf('{');
  const end = trimmed.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(trimmed.slice(start, end + 1));
  } catch {
    return null;
  }
}

function evidenceValid(
  item: { message_id?: unknown; quote?: unknown },
  messages: Array<{ id: string; content: string }>,
): { message_id: string; quote: string } | null {
  const messageId = typeof item.message_id === 'string' ? item.message_id : '';
  const quote = typeof item.quote === 'string' ? item.quote.trim() : '';
  if (!messageId || !quote) return null;
  const message = messages.find((m) => m.id === messageId);
  if (!message) return null;
  const normalize = (value: string) => value.replace(/\s+/g, '');
  if (!normalize(message.content).includes(normalize(quote))) return null;
  return { message_id: messageId, quote: quote.slice(0, 300) };
}

export function validateCandidates(
  raw: unknown,
  messages: Array<{ id: string; content: string }>,
): ExtractionCandidate[] {
  const list = (raw as { candidates?: unknown })?.candidates;
  if (!Array.isArray(list)) return [];
  const out: ExtractionCandidate[] = [];
  for (const item of list.slice(0, MAX_CANDIDATES)) {
    if (!item || typeof item !== 'object') continue;
    const source = item as Record<string, unknown>;
    const category = typeof source.category === 'string' ? source.category : '';
    if (!CATEGORY_SET.has(category)) continue;
    const content = typeof source.content === 'string' ? source.content.trim() : '';
    if (!content || content.length > MAX_CONTENT) continue;
    const memoryKey = typeof source.memory_key === 'string' ? source.memory_key.trim().toLowerCase() : '';
    if (!KEY_RE.test(memoryKey)) continue;
    const confidence = Number(source.confidence);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) continue;
    // 严格枚举：非法显式度直接丢弃，不能默认升级为 explicit。
    const explicitness = source.explicitness === 'explicit' || source.explicitness === 'inferred'
      ? source.explicitness
      : null;
    if (!explicitness) continue;
    const evidenceRaw = Array.isArray(source.evidence) ? source.evidence : [];
    const evidence: Array<{ message_id: string; quote: string }> = [];
    for (const entry of evidenceRaw.slice(0, 5)) {
      const valid = evidenceValid(entry as { message_id?: unknown; quote?: unknown }, messages);
      if (valid) evidence.push(valid);
    }
    if (evidence.length === 0) continue;
    const subject = typeof source.subject === 'string' ? source.subject.slice(0, 20) : '';
    const topic = typeof source.topic === 'string' ? source.topic.slice(0, 40) : '';
    out.push({
      category,
      subject,
      topic,
      memory_key: memoryKey,
      title: typeof source.title === 'string' ? source.title.slice(0, MAX_TITLE) : content.slice(0, MAX_TITLE),
      content,
      confidence,
      explicitness,
      evidence,
      suggested_action: source.suggested_action === 'update' ? 'update' : 'create',
    });
  }
  return out;
}

function buildPrompt(input: {
  currentMessageId: string;
  userText: string;
  history: Array<{ id: string; content: string }>;
  assistantText: string;
  subject: string;
  existing: Array<{ category: string; memory_key: string; content: string; confidence: number }>;
}): { system: string; user: string } {
  const system = [
    '你是学生长期记忆提取器。只从「本轮学生原话」中提取有长期价值的信息，输出严格 JSON。',
    '允许的 category：profile, learning_goal, preference, learning_style, weakness, misconception, learning_context, feedback。',
    'explicitness：学生明确表达=explicit；只能从多次行为推断=inferred。单次答错不能成为 weakness/misconception。',
    '禁止：保存助手生成的内容、猜测的性格/心理/能力、密码/密钥/住址/账号等敏感信息、一次性临时任务。',
    'evidence 必须引用真实 message_id 与学生原话片段（quote 必须能在原消息中原样找到）。',
    '如果本轮没有值得长期保存的信息，返回 {"candidates":[]}。',
    '输出：{"candidates":[{category,subject,topic,memory_key,title,content,confidence,explicitness,evidence:[{message_id,quote}],suggested_action:"create"|"update"}]}',
    'memory_key 用英文小写点分：如 teaching.explanation_style、subject.math.weakness.function_domain。',
    'subject 用「数学/语文/英语/物理/化学/生物/历史/地理/政治」，通用留空。',
    'content 用一句可独立理解的中文陈述，≤200 字，不要写“本轮/这次对话”。',
  ].join('\n');
  const existing = input.existing.length
    ? input.existing.map((m) => `- [${m.category}] ${m.memory_key}: ${m.content}`).join('\n')
    : '（暂无）';
  const history = input.history.length
    ? input.history.map((message) => `[message_id=${message.id}] ${message.content.slice(0, 300)}`).join('\n')
    : '（暂无）';
  const user = [
    `当前学科：${input.subject || '未识别'}`,
    `学生本轮消息（真实 message_id，只能引用这些 ID）：\n[message_id=${input.currentMessageId}] ${input.userText.slice(0, 4000)}`,
    `最近学生历史消息（推断类记忆需要多次独立证据时引用）：\n${history}`,
    `助手本轮回复（仅供参考，不能作为学生事实）：\n${input.assistantText.slice(0, 2000)}`,
    `已有正式记忆：\n${existing}`,
    '请只提取学生本轮新增/变化的信息；与已有记忆重复的不要重复输出。',
    'evidence 的 message_id 必须来自上面给出的学生消息；quote 必须是该消息里真实出现的片段。',
  ].join('\n\n');
  return { system, user };
}

export async function extractCandidatesForJob(job: MemoryJob, options: {
  apiKey?: string;
  callImpl?: typeof callProvider;
} = {}): Promise<{ created: number; autoAccepted: number; pending: number }> {
  const messages = listXuebanMessages(job.conversation_id);
  const userMessage = messages.find((m) => m.id === job.user_message_id);
  const assistantMessage = messages.find((m) => m.id === job.assistant_message_id);
  if (!userMessage || !assistantMessage) throw new Error('SOURCE_MESSAGE_MISSING');
  const apiKey = options.apiKey || resolveProviderKey(job.provider as ProviderId, {});
  if (!apiKey) throw new Error('NO_API_KEY');
  const existing = listMemories({ owner_id: job.owner_id, status: 'active', limit: 30 })
    .map((m) => ({ category: m.category, memory_key: m.memory_key, content: m.content, confidence: m.confidence }));
  const studentMessages = messages.filter((m) => m.sender_kind === 'user');
  const history = studentMessages
    .filter((m) => m.id !== userMessage.id)
    .slice(-12)
    .map((m) => ({ id: m.id, content: m.content }));
  const prompt = buildPrompt({
    currentMessageId: userMessage.id,
    userText: userMessage.content,
    history,
    assistantText: assistantMessage.content,
    subject: job.subject,
    existing,
  });
  const chunks: string[] = [];
  const call = options.callImpl ?? callProvider;
  const stream = call(job.provider as ProviderId, {
    systemPrompt: prompt.system,
    userMessage: prompt.user,
    model: job.model || undefined,
    api_key: apiKey,
    base_url: job.base_url || undefined,
    max_tokens: 1600,
    temperature: 0.1,
  });
  for await (const chunk of stream as AsyncIterable<ProviderChunk>) {
    if (chunk.type === 'text-delta') chunks.push(chunk.text);
  }
  const parsed = parseJson(textOf(chunks));
  // 证据只能引用学生本人消息：助手生成的内容不能作为学生事实。
  const evidenceMessages = studentMessages.map((m) => ({ id: m.id, content: m.content }));
  const candidates = validateCandidates(parsed, evidenceMessages);
  let created = 0;
  let autoAccepted = 0;
  let pending = 0;
  for (const candidate of candidates) {
    // 任务重试/重复提取：已有同 key 同内容正式记忆时只补证据，不再造候选。
    const existingMemory = findMemoryByKey(job.owner_id, candidate.memory_key);
    if (existingMemory && normalize(candidate.content) === normalize(existingMemory.content)) {
      attachEvidenceToMemory(job.owner_id, existingMemory.id, candidate.evidence, job.conversation_id);
      continue;
    }
    const record = createCandidate({
      owner_id: job.owner_id,
      category: candidate.category as MemoryCategory,
      subject: candidate.subject || job.subject || '',
      topic: candidate.topic || '',
      memory_key: candidate.memory_key,
      title: candidate.title,
      content: candidate.content,
      confidence: candidate.confidence,
      explicitness: candidate.explicitness,
      evidence: candidate.evidence,
      suggested_action: candidate.suggested_action,
    });
    created += 1;
    const explicitOk = candidate.explicitness === 'explicit' && candidate.confidence >= 0.85 && candidate.evidence.length >= 1;
    const inferredOk = candidate.explicitness === 'inferred'
      && candidate.confidence >= 0.75
      && new Set(candidate.evidence.map((e) => e.message_id)).size >= 3;
    if (!explicitOk && !inferredOk) {
      pending += 1;
      continue;
    }
    const result = acceptCandidate(job.owner_id, record.id, { auto: true });
    if (result.action === 'conflict') pending += 1;
    else autoAccepted += 1;
  }
  return { created, autoAccepted, pending };
}

export async function processMemoryJob(jobId: string, options: {
  apiKey?: string;
  /** 传入 key 时只用于匹配的 provider，避免拿 A 的 key 调 B。 */
  provider?: string;
  callImpl?: typeof callProvider;
} = {}): Promise<boolean> {
  const job = getJob(jobId);
  if (!job) return false;
  if (!markJobRunning(jobId)) return false;
  const apiKey = options.apiKey && (!options.provider || options.provider === job.provider)
    ? options.apiKey
    : undefined;
  if (!apiKey && !resolveProviderKey(job.provider as ProviderId, {})) {
    // 不把“缺 key”记成失败尝试：等学生下次带 key 请求时恢复。
    markJobWaitingCredentials(jobId, 'WAITING_FOR_API_KEY');
    return false;
  }
  try {
    await extractCandidatesForJob(job, { apiKey, callImpl: options.callImpl });
    finishJob(jobId);
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message === 'NO_API_KEY') {
      markJobWaitingCredentials(jobId, 'WAITING_FOR_API_KEY');
      return false;
    }
    failJob(jobId, message);
    return false;
  }
}

/** 补跑遗留任务（服务重启/失败重试）；返回处理成功数。 */
export async function runPendingMemoryJobs(limit = 3, options: {
  ownerId?: string;
  apiKey?: string;
  provider?: string;
  callImpl?: typeof callProvider;
} = {}): Promise<number> {
  let done = 0;
  for (const job of pendingJobs(limit, options.ownerId)) {
    if (await processMemoryJob(job.id, options)) done += 1;
  }
  return done;
}

export { enqueueMemoryJob };
