import { randomUUID } from 'node:crypto';
import { callProvider, isProviderId, PROVIDERS_NO_KEY, type ProviderId } from '../transport/api-clients';
import { planWithLlm } from '../lib/orchestrator-parse';
import { describePlan, heuristicPlan, isCasualChat, type ChatPlan, type StrategyAgent } from '../lib/chat-strategy';
import { ThinkingDeltaParser, type XuebanChatActivity, type XuebanChatStreamEvent } from './xueban-chat-stream';
import {
  appendXuebanMessage,
  createXuebanApproval,
  createXuebanCheckpoint,
  decideXuebanApproval,
  editLatestXuebanUserMessage,
  getXuebanApproval,
  getXuebanConversation,
  listXuebanApprovals,
  listXuebanMessages,
  updateXuebanMessage,
  renameXuebanConversation,
  restoreXuebanCheckpoint,
  type XuebanApprovalDecision,
  type XuebanApprovalOutcome,
  type XuebanChatApproval,
  type XuebanChatMessage,
} from '../storage/xueban-chat';
import { PYTHON_BACKEND_URL } from '../python-backend';
import { fetchMemoryBankHits, renderKnowledgeBlock } from '../lib/memory-bank';
import { listKnowledgePackIds } from '../storage/xueban-knowledge';
import { recordUsage } from '../storage/token-usage';
import type { XuebanChatAttachment } from './xueban-attachments';
import type { ContentBlock, ConversationMessage } from '../lib/conversation-types';
import { executeXuebanTool, xuebanToolsForEmployee } from './xueban-tools';

export class XuebanChatError extends Error {
  constructor(public status: number, public code: string, message: string, public thinking?: string) {
    super(message);
  }
}

interface Employee {
  id: string;
  name: string;
  title?: string;
  role?: string;
  system_prompt?: string;
  skills?: string[];
  tools?: string[];
  status?: string;
  employment?: string;
}

export interface XuebanChatModelConfig {
  provider: ProviderId;
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  companyId?: string;
  sessionId?: string;
  attachments?: XuebanChatAttachment[];
  toolEnv?: Readonly<Record<string, string>>;
  /** 用户选择的思考强度：off=关闭思考，low/medium/high 依次递增。 */
  thinkingEffort?: 'off' | 'low' | 'medium' | 'high';
  /** 学生学段年级背景（浏览器端算好，仅用于提示词）。 */
  studentProfile?: string;
}

export interface XuebanDagNodeMetadata {
  id: string;
  planner_task_id: string | null;
  agent_id: string;
  name: string;
  title: string;
  prompt: string;
  depends_on: string[];
  declared_depends_on: string[];
  requires_approval: boolean;
  /** 给老师看的审批摘要（不含角色/系统提示词）；仅需审批的节点有值。 */
  approval_brief?: string;
  validation_error: string | null;
}

export interface XuebanChatTurnResult {
  messages: XuebanChatMessage[];
  plan?: {
    strategy: string;
    source: string;
    assignments: Array<{ agent_id: string; name: string; title: string; depends_on: string[] }>;
    dag: { nodes: XuebanDagNodeMetadata[] };
  };
}

async function pythonGet<T>(path: string, authorization: string): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const response = await fetch(`${PYTHON_BACKEND_URL}${path}`, {
      headers: { Authorization: authorization }, signal: ctrl.signal,
    });
    if (!response.ok) throw new XuebanChatError(response.status, response.status === 401 ? 'UNAUTHORIZED' : 'COMPANY_API_ERROR', '账号或教研组数据校验失败');
    return await response.json() as T;
  } catch (error) {
    if (error instanceof XuebanChatError) throw error;
    throw new XuebanChatError(503, 'PYTHON_BACKEND_UNAVAILABLE', '教研组业务后端暂时不可用');
  } finally {
    clearTimeout(timer);
  }
}

export async function xuebanChatCompany(authorization: string): Promise<{ id: string; name: string }> {
  if (!authorization.startsWith('Bearer ')) throw new XuebanChatError(401, 'UNAUTHORIZED', '请先登录');
  const body = await pythonGet<{ company: { id: string; name: string } }>('/api/xueban/auth/me', authorization);
  return body.company;
}

/** 同一教研员同一时刻只跑一个任务：多对话并发时排队，避免互相打断/覆盖成果。 */
const agentTails = new Map<string, Promise<unknown>>();
function withAgentLock<T>(agentId: string, fn: () => Promise<T>): Promise<T> {
  const tail = agentTails.get(agentId) ?? Promise.resolve();
  const run = tail.catch(() => undefined).then(fn);
  agentTails.set(agentId, run.catch(() => undefined));
  return run;
}

async function complete(
  config: XuebanChatModelConfig,
  systemPrompt: string,
  userMessage: string,
  callImpl: typeof callProvider,
  stream?: { clientId: string; emit: (event: XuebanChatStreamEvent) => void },
  usageContext?: { companyId: string; sessionId: string },
  toolContext?: {
    permissions?: string[];
    onActivity: (patch: Partial<XuebanChatActivity> & Pick<XuebanChatActivity, 'kind' | 'name' | 'title' | 'status' | 'actor'>) => void;
  },
  /** 执行型任务可以关掉思考，显著缩短耗时（目前只有 MiMo 支持）。 */
  thinkingMode?: 'enabled' | 'disabled',
): Promise<{ text: string; thinking: string; durationMs: number }> {
  const startedAt = Date.now();
  const tools = toolContext ? xuebanToolsForEmployee(toolContext.permissions) : [];
  // 用户在输入框选的思考强度；执行型工单仍显式关闭长思考。
  const effort: 'off' | 'low' | 'medium' | 'high' = thinkingMode === 'disabled'
    ? 'off'
    : (config.thinkingEffort ?? 'medium');
  const thinkingEnabled = effort !== 'off';
  const reasoningEffort = effort === 'low' ? 'low' : effort === 'high' ? 'max' : 'high';
  // 低档已经是最低的思考预算，再补一条“少想快答”的约束，缩短多岗位串联的总耗时。
  const effectiveSystemPrompt = effort === 'low'
    ? `${systemPrompt}\n\n【效率要求】用尽量少的思考步骤直接给出可用答案，不要反复推演；把篇幅留给最终结果。`
    : systemPrompt;
  const messages: ConversationMessage[] = [{ role: 'user', blocks: [
    { kind: 'text', text: userMessage },
    ...(config.attachments ?? []).filter((item): item is XuebanChatAttachment & { kind: 'image' | 'video'; data_url: string } => (item.kind === 'image' || item.kind === 'video') && Boolean(item.data_url))
      .map(item => ({ kind: item.kind, data_url: item.data_url })),
  ] }];
  const thinking: string[] = [];
  // 学习类任务经常需要多轮检索：默认允许 12 轮工具调用，可用环境变量调整。
  const maxToolRounds = Math.max(1, Math.min(40, Number(process.env.XUEBAN_MAX_TOOL_ROUNDS || 12)));
  for (let round = 0; round < maxToolRounds; round += 1) {
    const parser = new ThinkingDeltaParser(stream?.clientId ?? '', stream?.emit);
    const toolCalls: Array<{ id: string; name: string; input: unknown }> = [];
    let failure: { code?: string; message: string } | null = null;
    // 最后一轮不再给工具：强制模型基于已有检索结果直接作答，避免“轮次耗尽就失败”。
    const roundTools = round === maxToolRounds - 1 ? [] : tools;
    for await (const chunk of callImpl(config.provider, {
      systemPrompt: effectiveSystemPrompt, userMessage, messages, tools: roundTools,
      model: config.model, api_key: config.apiKey, base_url: config.baseUrl,
      thinking: thinkingEnabled ? 'enabled' : 'disabled',
      reasoning_effort: thinkingEnabled ? reasoningEffort : undefined,
      // MiMo 默认开启思考，reasoning token 与正文共用同一个输出上限。旧的
      // 2600 会让“思考较长 + 长报告”把预算吃光：轻则表格/报告被截断（历史
      // 记录里能看到半截表格），重则正文为空、整单报“模型没有返回有效内容”。
      max_tokens: 8192, temperature: 0.35,
    })) {
      if (chunk.type === 'text-delta') parser.push(chunk.text);
      if (chunk.type === 'tool-use') toolCalls.push({ id: chunk.id, name: chunk.name, input: chunk.input });
      if (chunk.type === 'usage' && (usageContext || config.companyId)) recordUsage({
        company_id: usageContext?.companyId ?? config.companyId!, session_id: usageContext?.sessionId ?? config.sessionId ?? '', model: config.model,
        input_tokens: chunk.usage.input_tokens, cached_input_tokens: chunk.usage.cache_read_input_tokens,
        output_tokens: chunk.usage.output_tokens,
      });
      if (chunk.type === 'error') {
        failure = { code: chunk.code, message: chunk.message };
        break;
      }
    }
    if (failure) {
      const status = failure.code === 'NO_API_KEY' ? 401 : failure.code === 'RATE_LIMITED' ? 429 : 502;
      throw new XuebanChatError(status, failure.code ?? 'LLM_UPSTREAM_ERROR', failure.message);
    }
    const text = parser.finish();
    if (parser.thinking) thinking.push(parser.thinking);
    const reasoning = thinking.join('\n\n').trim();
    if (!toolCalls.length) {
      if (!text && reasoning && effort !== 'off') {
        // 思考把输出上限吃光：关掉思考重答一次，避免整轮直接失败。
        const fallback = await complete(
          { ...config, thinkingEffort: 'off' }, systemPrompt, userMessage, callImpl,
          stream, usageContext, toolContext, 'disabled',
        );
        return { text: fallback.text, thinking: reasoning, durationMs: Math.max(0, Date.now() - startedAt) };
      }
      if (!text) {
        // 只返回思考、没有正文是真实存在的情况：既不能把思考当成果交付，
        // 也不能再笼统报“模型没有返回有效内容”。把思考原文带回上层，让
        // 失败卡片能说明发生了什么。
        throw new XuebanChatError(
          502,
          'LLM_EMPTY_RESPONSE',
          reasoning
            ? '模型只输出了思考过程，没有生成正式回答（输出上限可能被思考耗尽，可从 Checkpoint 重做该任务）'
            : '模型没有返回有效内容',
          reasoning || undefined,
        );
      }
      return { text, thinking: reasoning, durationMs: Math.max(0, Date.now() - startedAt) };
    }
    if (!tools.length) throw new XuebanChatError(502, 'TOOL_PERMISSION_DENIED', '模型请求了当前岗位未授权的工具');
    const assistantBlocks: ContentBlock[] = [
      ...(text ? [{ kind: 'text' as const, text }] : []),
      ...toolCalls.map(call => ({ kind: 'tool_use' as const, id: call.id, name: call.name, input: call.input })),
    ];
    messages.push({ role: 'assistant', blocks: assistantBlocks });
    for (const call of toolCalls) {
      const toolInput = JSON.stringify(call.input ?? {}).slice(0, 4000);
      toolContext?.onActivity({ kind: 'tool', name: call.name, title: `调用工具：${call.name}`, status: 'running', actor: '教研员', input: toolInput });
      const result = await executeXuebanTool({ companyId: config.companyId!, conversationId: config.sessionId, name: call.name, input: call.input, env: config.toolEnv });
      const output = JSON.stringify(result.output ?? {}).slice(0, 24_000);
      toolContext?.onActivity({ kind: 'tool', name: call.name, title: result.isError ? `工具执行失败：${call.name}` : `工具执行完成：${call.name}`, status: result.isError ? 'failed' : 'done', actor: '教研员', input: toolInput, output, detail: result.isError ? '未产生真实外部结果，已如实返回模型' : '真实结果已回传当前岗位' });
      messages.push({ role: 'tool', blocks: [{ kind: 'tool_result', tool_use_id: call.id, tool_name: call.name, output, is_error: result.isError }] });
    }
  }
  throw new XuebanChatError(502, 'TOOL_ROUND_LIMIT', `本轮工具调用达到 ${maxToolRounds} 轮上限，已停止；可以缩小问题范围或重试`);
}

function transcript(messages: XuebanChatMessage[]): string {
  return messages.slice(-24).map((message) => {
    const role = message.sender_kind === 'user' ? '老师' : message.sender_name;
    return `${role}：${message.content}`;
  }).join('\n').slice(-30_000);
}

interface DagNode extends XuebanDagNodeMetadata {
  systemPrompt: string;
  preblocked: boolean;
}

type TaskResult = {
  status: 'done' | 'failed' | 'blocked' | 'rejected';
  message?: XuebanChatMessage;
  handoff?: string;
};

interface XuebanResumeState {
  content: string;
  attachments: XuebanChatAttachment[];
  previous: XuebanChatMessage[];
  plan: ChatPlan;
  planText: string;
  employees: Employee[];
  knowledgeContext: string;
  results: Array<[string, TaskResult]>;
  /** 快照时的 DAG 定义，用于恢复时判断哪些任务属于同一失败分支。 */
  nodes?: Array<{ id: string; title: string; agent_id: string; depends_on: string[]; name: string }>;
  /** 本次恢复要重跑的根任务；恢复时保留不在该任务下游的并行成果。 */
  resume_task_id?: string;
}

const approvalWaiters = new Map<string, {
  resolve: (outcome: XuebanApprovalOutcome) => void;
  emit?: (event: XuebanChatStreamEvent) => void;
}>();

export function decideXuebanChatApproval(input: {
  approvalId: string;
  conversationId: string;
  companyId: string;
  decision: XuebanApprovalDecision;
  customText?: string;
  outcome?: XuebanApprovalOutcome;
}): { approval: XuebanChatApproval; message: XuebanChatMessage } {
  const existing = getXuebanApproval(input.approvalId, input.companyId);
  if (!existing || existing.conversation_id !== input.conversationId) {
    throw new XuebanChatError(404, 'APPROVAL_NOT_FOUND', '审批记录不存在');
  }
  if (existing.status !== 'pending') {
    throw new XuebanChatError(409, 'APPROVAL_ALREADY_DECIDED', '该审批已经提交过决定');
  }
  const customText = String(input.customText ?? '').trim();
  if (input.decision === 'custom' && !customText) {
    throw new XuebanChatError(422, 'CUSTOM_DECISION_REQUIRED', '自定义决定必须填写内容');
  }
  if (customText.length > 4000) {
    throw new XuebanChatError(413, 'CUSTOM_DECISION_TOO_LARGE', '自定义决定不能超过 4000 字');
  }
  const outcome = input.decision === 'approve' ? 'continue' : input.decision === 'reject' ? 'reject' : input.outcome;
  if (!outcome || !['continue', 'reject'].includes(outcome)) {
    throw new XuebanChatError(422, 'APPROVAL_OUTCOME_REQUIRED', '自定义决定必须明确 continue 或 reject');
  }
  const approval = decideXuebanApproval(input.approvalId, input.companyId, input.decision, customText, outcome);
  if (!approval) throw new XuebanChatError(409, 'APPROVAL_ALREADY_DECIDED', '该审批已经提交过决定');
  const decisionLabel = input.decision === 'approve' ? '批准' : input.decision === 'reject' ? '驳回' : '自定义决定';
  // 原审批卡片改成「已决定」，不再新增第二张审批卡；决定本身只留一条时间线消息。
  for (const message of listXuebanMessages(existing.conversation_id)) {
    if (!message.metadata_json) continue;
    try {
      const metadata = JSON.parse(message.metadata_json) as { approval?: { id?: string } };
      if (metadata.approval?.id !== approval.id) continue;
      updateXuebanMessage(message.id, { metadata: { ...metadata, approval } as never, status: 'done' });
    } catch { /* 忽略坏 metadata */ }
  }
  const message = appendXuebanMessage(existing.conversation_id, {
    sender_kind: 'user', sender_name: '老师', channel: 'workflow', run_id: existing.run_id,
    task_id: existing.task_id, task_title: existing.task_title,
    content: `${decisionLabel}《${existing.task_title}》：${outcome === 'continue' ? '继续执行' : '停止该任务及其下游'}${customText ? `\n${customText}` : ''}`,
    status: 'done',
  });
  const waiter = approvalWaiters.get(approval.id);
  approvalWaiters.delete(approval.id);
  waiter?.emit?.({ type: 'approval', approval });
  waiter?.resolve(outcome);
  return { approval, message };
}

/**
 * 什么必须老师审批：会改变外部世界、涉及真实学生个人数据、花钱或删除既有成果。
 *
 * 刻意用「动词＋宾语」而不是单独的高危词：写学情报告、做学情分析、排学习计划
 * 都属于内部产出，永不需要审批；只有真正改变外部世界或触碰真实个人数据的动作才拦。
 *
 * 只看任务标题，不扫工单正文——正文里常有“未经老师确认前不对外发布”这类否定句，
 * 扫正文会把「生成学情阶段小结」这种普通汇报误判成审批。
 */
const APPROVAL_ACTION_RE = new RegExp([
  '对外发布', '发布(?:到|至|上线)', '发布(?:公告|新闻稿|页面|上线)',
  '公开上线', '上线(?:发布|新)',
  '导出(?:真实)?学生(?:个人)?数据', '上传真实学生数据', '删除(?:数据|记录|文件|成果)',
  '群发', '批量发送', '发送给家长', '发送给学生', '邮件发送',
  '付款', '收款', '退款', '转账', '打款', '签署', '签合同',
  '投放(?:广告|预算)', '购买(?:广告|流量)', '合规放行', '风险豁免',
].join('|'));

/** 纯内容生产/内部汇报：即使规划器误标，也永不触发任务级审批。 */
const NON_APPROVAL_TITLE_RE = /小结|总结|汇总|报告|简报|周报|日报|调研|分析|方案|文案|草稿|计划|整理|梳理|复盘|记录|清单|排期|规划|撰写|草拟|答复|回复/;

function explicitApproval(title: string, prompt: string, _declared: boolean | undefined): boolean {
  const headline = title.trim();
  if (APPROVAL_ACTION_RE.test(headline)) return true;
  if (NON_APPROVAL_TITLE_RE.test(headline)) return false;
  // 标题里没有明确的硬动作（如“生成阶段小结”），一律不作为审批。
  return false;
}

/** 审批卡片上要显示的具体内容：老师能看懂的摘要 + 上游交付，不贴系统提示词，也不能只有标题。 */
function approvalContentOf(input: {
  goal: string;
  title: string;
  brief?: string;
  handoffs?: string[];
}): string {
  const brief = (input.brief ?? '').trim();
  const parts: string[] = [];
  parts.push(brief || `工单：${input.title}\n老师目标：${input.goal}`);
  const handoff = input.handoffs?.filter(Boolean).join('\n\n').slice(-4000) ?? '';
  if (handoff) parts.push(`上游交付摘要：\n${handoff}`);
  return parts.join('\n\n').slice(0, 12_000);
}

/** 目标任务 + 所有依赖它的下游（回滚时要重跑的集合）。 */
function checkpointScope(nodes: XuebanResumeState['nodes'], taskId: string): Set<string> {
  const scope = new Set<string>([taskId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of nodes ?? []) {
      if (!scope.has(node.id) && node.depends_on.some(dep => scope.has(dep))) {
        scope.add(node.id);
        changed = true;
      }
    }
  }
  return scope;
}

/**
 * 回滚只重跑失败任务及其下游；同一轮里其它并行分支已经产出的成果要保留，
 * 否则「重做 A」会把并行同事 B 的成果一起滚掉（滚多）。
 */
function mergePeerResults(conversationId: string, state: XuebanResumeState, scope: Set<string>, runId: string): void {
  const latest = new Map<string, XuebanChatMessage>();
  for (const message of listXuebanMessages(conversationId)) {
    if (message.sender_kind !== 'agent' || !message.task_id || message.status !== 'done' || !message.content) continue;
    if (message.run_id !== runId) continue;
    latest.set(message.task_id, message);
  }
  const results = new Map<string, TaskResult>(state.results ?? []);
  for (const [taskId, message] of latest) {
    if (scope.has(taskId)) continue;
    if (results.get(taskId)?.status === 'done') continue;
    results.set(taskId, {
      status: 'done', message,
      handoff: `${message.sender_name}（教研员）交付：\n${message.content}`,
    });
  }
  state.results = [...results];
}

/** 被回滚覆盖的旧产出打上 superseded，产物列表里显示为历史版本。 */
function markSuperseded(conversationId: string, scope: Set<string>, checkpointId: string, runId: string): void {
  for (const message of listXuebanMessages(conversationId)) {
    if (!message.task_id || !scope.has(message.task_id) || message.run_id !== runId) continue;
    try {
      const metadata = JSON.parse(message.metadata_json ?? '{}') as Record<string, unknown>;
      if (metadata.superseded) continue;
      updateXuebanMessage(message.id, { metadata: { ...metadata, superseded: true, superseded_by: checkpointId } as never });
    } catch { /* 历史消息 metadata 损坏时跳过 */ }
  }
}

export async function runXuebanChatTurn(input: {
  conversationId: string;
  companyId: string;
  content: string;
  attachments?: XuebanChatAttachment[];
  config: XuebanChatModelConfig;
  authorization: string;
  embeddingHeaders?: Record<string, string>;
  assignedAgentId?: string;
  callImpl?: typeof callProvider;
  onEvent?: (event: XuebanChatStreamEvent) => void;
  editMessageId?: string;
  checkpointId?: string;
}): Promise<XuebanChatTurnResult> {
  const conversation = getXuebanConversation(input.conversationId, input.companyId);
  if (!conversation) throw new XuebanChatError(404, 'CONVERSATION_NOT_FOUND', '对话不存在');
  if (!isProviderId(input.config.provider)) throw new XuebanChatError(422, 'INVALID_PROVIDER', '模型提供商不受支持');
  if (!input.config.apiKey && !PROVIDERS_NO_KEY.has(input.config.provider)) {
    throw new XuebanChatError(401, 'NO_API_KEY', '请先在设置中配置当前模型的 API Key');
  }
  input.config.companyId = input.companyId;
  input.config.sessionId = input.conversationId;
  const call = input.callImpl ?? callProvider;
  let content = input.content.trim();
  let runId: string;
  let previous: XuebanChatMessage[];
  let resumeState: XuebanResumeState | undefined;
  let resumeTaskId: string | undefined;
  let resumeCheckpointId: string | undefined;

  if (input.checkpointId) {
    try {
      const restored = restoreXuebanCheckpoint(input.checkpointId, input.companyId, conversation.id);
      resumeState = restored.snapshot.state as XuebanResumeState;
      if (!resumeState?.plan || !Array.isArray(resumeState.employees) || !Array.isArray(resumeState.results)) {
        throw new Error('CHECKPOINT_INVALID');
      }
      runId = restored.checkpoint.run_id;
      resumeTaskId = restored.checkpoint.task_id;
      resumeCheckpointId = restored.checkpoint.id;
      // 回滚范围 = 失败任务 + 下游；并行分支的既有成果合并回来，避免滚多。
      const scope = checkpointScope(resumeState.nodes, restored.checkpoint.task_id);
      markSuperseded(conversation.id, scope, restored.checkpoint.id, runId);
      mergePeerResults(conversation.id, resumeState, scope, runId);
      content = resumeState.content;
      previous = resumeState.previous;
      input.attachments = resumeState.attachments;
      input.onEvent?.({ type: 'conversation-restored', messages: listXuebanMessages(conversation.id) });
    } catch (error) {
      if (error instanceof XuebanChatError) throw error;
      const code = error instanceof Error ? error.message : 'CHECKPOINT_INVALID';
      throw new XuebanChatError(code === 'CHECKPOINT_NOT_FOUND' ? 404 : 422, code,
        code === 'CHECKPOINT_NOT_FOUND' ? 'Checkpoint 不存在或不属于当前教研组' : 'Checkpoint 数据无效，无法恢复');
    }
  } else {
    content ||= input.attachments?.length ? '请分析我发送的附件' : '';
    if (!content) throw new XuebanChatError(422, 'EMPTY_MESSAGE', '请输入对话内容或添加附件');
    if (content.length > 12_000) throw new XuebanChatError(413, 'MESSAGE_TOO_LARGE', '单条消息不能超过 12000 字');
    if (input.editMessageId) {
      try {
        const edited = editLatestXuebanUserMessage({ conversationId: conversation.id, companyId: input.companyId, messageId: input.editMessageId, content });
        runId = edited.run_id ?? randomUUID();
        previous = listXuebanMessages(conversation.id).filter(message => message.id !== edited.id);
        if (previous.length === 0) renameXuebanConversation(conversation.id, content);
        if (!input.attachments?.length && edited.metadata_json) {
          const metadata = JSON.parse(edited.metadata_json) as { attachments?: XuebanChatAttachment[] };
          input.attachments = metadata.attachments ?? [];
        }
        input.onEvent?.({ type: 'conversation-restored', messages: listXuebanMessages(conversation.id) });
      } catch (error) {
        const code = error instanceof Error ? error.message : 'MESSAGE_EDIT_FAILED';
        if (code === 'ONLY_LATEST_USER_MESSAGE_EDITABLE') throw new XuebanChatError(409, code, '只能编辑当前对话中最近一条自己的发言');
        if (code === 'MESSAGE_NOT_FOUND') throw new XuebanChatError(404, code, '要编辑的发言不存在');
        throw error;
      }
    } else {
      runId = randomUUID();
      previous = listXuebanMessages(conversation.id);
      appendXuebanMessage(conversation.id, { sender_kind: 'user', sender_name: '老师', content, run_id: runId,
        metadata: input.attachments?.length ? { attachments: input.attachments } : null });
      if (previous.length === 0) renameXuebanConversation(conversation.id, content);
    }
  }
  input.config.attachments = input.attachments ?? [];

  const beginMessage = (
    senderKind: 'ceo' | 'agent', senderName: string, agentId?: string,
    channel: XuebanChatMessage['channel'] = 'chat', task?: { id: string; title: string }, workflow = channel === 'workflow' || Boolean(task),
  ) => {
    const clientId = randomUUID();
    input.onEvent?.({
      type: 'message-start', client_id: clientId, sender_kind: senderKind, sender_name: senderName,
      agent_id: agentId, channel, run_id: runId, task_id: task?.id, task_title: task?.title, workflow,
    });
    return clientId;
  };
  const endMessage = (clientId: string, message: XuebanChatMessage) => {
    input.onEvent?.({ type: 'message-end', client_id: clientId, message });
  };
  const activity = (
    id: string,
    patch: Partial<XuebanChatActivity> & Pick<XuebanChatActivity, 'kind' | 'name' | 'title' | 'status' | 'actor'>,
  ): XuebanChatActivity => {
    const value: XuebanChatActivity = { id, run_id: runId, created_at: new Date().toISOString(), ...patch };
    input.onEvent?.({ type: 'activity', activity: value });
    if (value.status !== 'running') {
      appendXuebanMessage(conversation.id, {
        sender_kind: 'system', sender_name: value.actor, agent_id: value.agent_id,
        content: `${value.title}${value.detail ? `：${value.detail}` : ''}`,
        channel: 'workflow', run_id: runId, task_id: value.task_id, status: value.status,
        duration_ms: value.duration_ms, metadata: { activity: value },
      });
    }
    return value;
  };

  // 单聊是“老师只面对教学主管”的沟通方式，不是教学主管亲自包办任务。
  // 只有纯寒暄直接应答；咨询和任务都进入同一套教研员编排与真实模型调用。
  if (!resumeState && conversation.kind === 'single' && isCasualChat(content)) {
    const clientId = beginMessage('ceo', '教学主管');
    const result = await complete(input.config, [
      '你是智慧学伴的学习主管，直接和学习者本人对话。',
      '说话用中学生听得懂的话：简短、具体、友好，不要用验收、交付、口径、审批这类内部词。',
      '寒暄时就自然聊两句，并顺势问问对方最近在学什么、卡在哪里；不得虚构已经执行的外部操作。',
    ].join('\n'), `以下是历史对话：\n${transcript(previous)}\n\n学生最新消息：${content}`, call,
    input.onEvent ? { clientId, emit: input.onEvent } : undefined);
    const message = appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: result.text, run_id: runId,
      thinking: result.thinking, status: 'done', duration_ms: result.durationMs,
    });
    endMessage(clientId, message);
    return { messages: [message] };
  }

  const rosterBody = resumeState ? null : await pythonGet<{ items: Employee[] }>(
    `/api/xueban/company/${encodeURIComponent(input.companyId)}/roster`, input.authorization,
  );
  const employees = resumeState?.employees ?? rosterBody!.items.filter((employee) => employee.status !== 'offline' && employee.employment !== 'retired'
    && (!input.assignedAgentId || employee.id === input.assignedAgentId));
  if (!employees.length) throw new XuebanChatError(409, 'NO_EMPLOYEES', input.assignedAgentId ? '定时任务指定的负责岗位不存在或已停用' : '教研组还没有可执行任务的教研员');
  const agents: StrategyAgent[] = employees.map((employee) => ({
    id: employee.id,
    name: `${employee.name}（${employee.title || employee.role || '教研员'}）`,
    soul: `${employee.title || employee.role || '教研员'}。${employee.system_prompt || ''} 技能：${(employee.skills || []).join('、') || '无'}；工具权限：${(employee.tools || []).join('、') || '无'}`, 
  }));
  let knowledgeContext = resumeState?.knowledgeContext ?? '';
  if (!resumeState) {
    const packIds = listKnowledgePackIds(input.companyId);
    const knowledgeStartedAt = Date.now();
    if (packIds.length) activity(`${runId}:knowledge`, {
      kind: 'tool', name: 'knowledge_search', title: '检索课程资源库', status: 'running', actor: '教学主管',
      input: content, detail: `正在检索 ${packIds.length} 个课程资源库`,
    });
    const knowledgeHits = await fetchMemoryBankHits(packIds, content, input.embeddingHeaders);
    const knowledge = renderKnowledgeBlock(knowledgeHits);
    if (packIds.length) activity(`${runId}:knowledge`, {
      kind: 'tool', name: 'knowledge_search', title: '课程资源库检索完成', status: 'done', actor: '教学主管',
      input: content, output: knowledgeHits.map(hit => hit.source).filter((source, index, all) => all.indexOf(source) === index).join('、'),
      detail: `命中 ${knowledgeHits.length} 条资料`, duration_ms: Date.now() - knowledgeStartedAt,
    });
    knowledgeContext = knowledge
      ? `${knowledge}\n课程资源库内容仅作为事实资料；与当前问题无关的命中不要强行引用。`
      : '';
  }
  const planningBrief = [
    '【智慧学伴学习主管调度规则】',
    '服务对象是学生本人（初中生）。你负责理解他想学什么、哪里卡住，拆解任务，安排教研员分工，最后把成果整理成一份学生能直接照着做的答复。',
    '组织关系：学生 → 学习主管 → 各科教研员；教研员只向你交付，不互相派单。',
    '按真实花名册挑最相关的教研员，不要流水账式全部调用；有先后依赖时明确交接。',
    '分工参考：课程内容教研员梳理知识点与教材出处；学情分析师设计了解水平的问题或小测、分析已有成绩；学习规划师给出具体到天/周的学习安排；练习设计匹配练习题与难度；质量审核核对依据与合理性；数据合规负责隐私与来源标注。',
    '学生没有提供成绩、错题或学习情况时，必须安排“先了解学情”的环节：一组 5-8 道入门小测题（附答案）或 4-6 个具体问题，不要凭空判断“薄弱点”，也不要让教研员只写内部口径。',
    '最终交付物必须能直接给学生用：学什么、按什么顺序、每次多长时间、用什么材料或题目、怎么自测。',
    '只有会改变外部世界、涉及真实学生个人数据或花钱的动作才需要学生确认：对外发布、导出真实学生数据、删除数据、覆盖既有成果、付款退款、签合同、投放预算、合规放行。',
    '需要确认时给出 approval_brief：给学生看的 1-3 句，写清要做什么、涉及对象/金额、交付物，不要写角色设定或系统提示词。',
    '写小结、总结、汇总、报告、简报、调研、分析、方案、文案、草稿、计划、整理、复盘一律不需要审批，不要为了凑流程设置审批。',
    '',
    ...(input.config.studentProfile ? ['', '【学生学段】', input.config.studentProfile] : []),
    '【本轮对话历史】',
    transcript(previous) || '暂无',
    '',
    '【学生本轮目标】',
    content,
    input.attachments?.filter(item => item.kind === 'text').map(item => `【TXT 附件：${item.name}】\n${item.text ?? ''}`).join('\n\n') ?? '',
    input.attachments?.some(item => item.kind === 'image' || item.kind === 'video') ? '本轮包含图片或视频附件，请结合多模态内容理解和分工。' : '',
    knowledgeContext,
  ].filter(Boolean).join('\n');
  const planningStartedAt = Date.now();
  const managerModel = input.config.model || input.config.provider;
  if (!resumeState) activity(`${runId}:planning`, {
    kind: 'model', name: 'manager_planning', title: '教学主管正在分析目标与分工', status: 'running', actor: '教学主管',
    detail: `结合对话、知识资料与教研员能力制定 DAG 执行计划 · 模型 ${managerModel}`,
  });
  let planError: { message: string; code?: string } | undefined;
  const plan = resumeState?.plan ?? await planWithLlm({
    userText: planningBrief,
    agents,
    provider: input.config.provider,
    apiKey: input.config.apiKey,
    model: input.config.model,
    baseUrl: input.config.baseUrl,
    // 规划同样受 MiMo 思考 token 与正文共用上限的约束；1600 很容易被思考吃光，
    // 导致计划 JSON 缺失后静默降级为规则分工。这里给足预算，仍按实际用量计费。
    maxTokens: 8192,
    thinking: (input.config.thinkingEffort ?? 'medium') === 'off' ? 'disabled' as const : 'enabled' as const,
    reasoning_effort: (input.config.thinkingEffort ?? 'medium') === 'off' ? undefined : ((input.config.thinkingEffort === 'low') ? 'low' as const : 'high' as const),
    attachments: input.config.attachments?.filter((item): item is XuebanChatAttachment & { kind: 'image' | 'video'; data_url: string } => (item.kind === 'image' || item.kind === 'video') && Boolean(item.data_url))
      .map(item => ({ kind: item.kind, data_url: item.data_url })),
    callImpl: call,
    onUsage: usage => recordUsage({
      company_id: input.companyId, session_id: input.conversationId, model: input.config.model,
      input_tokens: usage.input_tokens, cached_input_tokens: usage.cache_read_input_tokens, output_tokens: usage.output_tokens,
    }),
    onThinkingDelta: (_delta, thinking) => activity(`${runId}:planning`, {
      kind: 'model', name: 'manager_planning', title: '教学主管正在分析目标与分工', status: 'running', actor: '教学主管',
      thinking, detail: `结合对话、知识资料与教研员能力制定 DAG 执行计划 · 模型 ${managerModel}`,
    }),
    // 模型调用失败（错误 Key / 连不上 / 限流）不能悄悄降级成规则分工，否则
    // 老师会以为教学主管“还能用”，只有教研员报错。这里直接失败并在异常栏说明。
    onError: (message, code) => { planError = { message, code }; },
  }) ?? heuristicPlan(content, agents);
  if (planError) {
    const detail = `教学主管规划失败（模型 ${managerModel}）：${planError.message}`;
    activity(`${runId}:planning`, {
      kind: 'model', name: 'manager_planning', title: '教学主管规划失败', status: 'failed', actor: '教学主管', detail,
      duration_ms: Date.now() - planningStartedAt,
    });
    throw new XuebanChatError(502, planError.code ?? 'MANAGER_PLAN_FAILED', detail);
  }
  const byId = new Map(employees.map((employee) => [employee.id, employee]));
  // 展示统一用教研员姓名；岗位/职务只出现在规划器名单与岗位信息里，
  // 不再把「林研（市场研究员）」写进 DAG、消息发送人和活动记录。
  const nameOf = (id: string) => byId.get(id)?.name ?? id;
  const executionChannel: XuebanChatMessage['channel'] = conversation.kind === 'single' ? 'workflow' : 'chat';

  const nodes: DagNode[] = plan.tasks.map((task, index) => {
    const employee = byId.get(task.agentId);
    return {
      id: `${runId}:task:${index + 1}`,
      planner_task_id: task.id ?? null,
      agent_id: task.agentId,
      name: nameOf(task.agentId),
      title: task.title,
      prompt: task.prompt,
      depends_on: [],
      declared_depends_on: task.depends_on ?? [],
      requires_approval: explicitApproval(task.title, task.prompt, task.requires_approval),
      approval_brief: task.requires_approval ? task.approval_brief?.trim() : undefined,
      validation_error: employee ? null : `执行教研员不存在：${task.agentId}`,
      systemPrompt: [
        employee?.system_prompt || `你是教研组的${employee?.title || employee?.role || '教研员'}${employee?.name || ''}，只负责自己的专业工单。`,
        `你的岗位是${employee?.title || employee?.role || '教研员'}，你只执行学习主管派发且属于本岗位的任务，并向学习主管汇报。`,
        `你的技能：${(employee?.skills || []).join('、') || '未配置'}。允许使用的工具权限：${(employee?.tools || []).join('、') || '无'}；不得声称使用未授权工具。`,
        '你写的内容最终会整理成给中学生看的答复：结论要具体、能直接照做（学什么、做什么题、花多久、怎么检查），不要只写内部建议、口径、风险清单或待审批事项。',
        '如果缺少学生的真实情况，先给出用来了解学情的小测题或问题清单，并说明怎么判断水平，不要编造学生的分数、排名或薄弱点。',
        '不要在成果里出现“老师”“教研组内部”“验收”“工单”“DAG”等称呼或术语。',
      ].join('\n'),
      preblocked: !employee,
    };
  });
  const byPlannerId = new Map(nodes.filter(node => node.planner_task_id).map(node => [node.planner_task_id as string, node]));
  const byAgentId = new Map(nodes.map(node => [node.agent_id, node]));
  const byRuntimeId = new Map(nodes.map(node => [node.id, node]));
  for (const node of nodes) {
    for (const ref of node.declared_depends_on) {
      const upstream = byPlannerId.get(ref) ?? byRuntimeId.get(ref) ?? byAgentId.get(ref);
      if (!upstream) {
        node.validation_error = `缺失依赖：${ref}`;
        node.preblocked = true;
      } else {
        node.depends_on.push(upstream.id);
      }
    }
  }

  // Mark only actual cycle members. Other valid branches remain executable.
  const visitState = new Map<string, 'visiting' | 'visited'>();
  const visitStack: DagNode[] = [];
  const detectCycle = (node: DagNode): void => {
    if (node.preblocked || visitState.get(node.id) === 'visited') return;
    if (visitState.get(node.id) === 'visiting') {
      const start = visitStack.findIndex(item => item.id === node.id);
      for (const cyclic of visitStack.slice(start)) {
        cyclic.validation_error = '依赖关系存在循环';
        cyclic.preblocked = true;
      }
      return;
    }
    visitState.set(node.id, 'visiting');
    visitStack.push(node);
    for (const dependency of node.depends_on) {
      const upstream = byRuntimeId.get(dependency);
      if (upstream) detectCycle(upstream);
    }
    visitStack.pop();
    visitState.set(node.id, 'visited');
  };
  for (const node of nodes) detectCycle(node);

  const dag = {
    nodes: nodes.map(node => ({
      id: node.id,
      planner_task_id: node.planner_task_id,
      agent_id: node.agent_id,
      name: node.name,
      title: node.title,
      prompt: node.prompt,
      depends_on: node.depends_on,
      declared_depends_on: node.declared_depends_on,
      requires_approval: node.requires_approval,
      validation_error: node.validation_error,
    })),
  };
  const planText = resumeState?.planText ?? `${describePlan(plan, nameOf)}\n${plan.tasks.map((task, index) => `${index + 1}. ${nameOf(task.agentId)}：${task.title}`).join('\n')}`;
  let planMessage: XuebanChatMessage;
  if (resumeState) {
    const existing = listXuebanMessages(conversation.id).find(message => message.run_id === runId && message.sender_kind === 'ceo' && message.metadata_json?.includes('"dag"'));
    if (!existing) throw new XuebanChatError(422, 'CHECKPOINT_INVALID', 'Checkpoint 缺少原分工消息');
    planMessage = existing;
  } else {
    const planClientId = beginMessage('ceo', '教学主管', undefined, executionChannel, undefined, true);
    activity(`${runId}:planning`, {
      kind: 'model', name: 'manager_planning', title: '已安排本轮学习诊断分工', status: 'done', actor: '教学主管',
      output: planText, thinking: plan.thinking, detail: `${plan.tasks.length} 位教研员参与，有依赖的按顺序交接`, duration_ms: Date.now() - planningStartedAt,
    });
    input.onEvent?.({ type: 'content-delta', client_id: planClientId, text: planText });
    planMessage = appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: planText, channel: executionChannel,
      run_id: runId, thinking: plan.thinking ?? '', status: 'done', metadata: {
        strategy: plan.strategy,
        source: plan.source,
        assignments: nodes.map(node => ({ agent_id: node.agent_id, name: node.name, title: node.title, depends_on: node.depends_on })),
        dag,
      },
    });
    endMessage(planClientId, planMessage);
  }

  const completedResults = new Map<string, TaskResult>(resumeState?.results ?? []);
  // 计划只保留在 DAG 面板里；聊天区数据流按真实执行顺序逐条出现，
  // 不再预先铺一排“等待上游”的占位行。
  for (const node of nodes.filter(item => item.preblocked && !completedResults.has(item.id))) {
    activity(`${runId}:task:${node.id.split(':').at(-1)}`, {
      kind: 'model', name: 'employee_task', title: node.title, status: 'blocked', actor: node.name,
      agent_id: node.agent_id, task_id: node.id, depends_on: node.depends_on,
      detail: node.validation_error ?? '依赖校验失败',
    });
  }

  const results = new Map<string, Promise<TaskResult>>(
    [...completedResults].map(([id, result]) => [id, Promise.resolve(result)]),
  );
  const executeNode = (node: DagNode): Promise<TaskResult> => {
    const restored = results.get(node.id);
    if (restored) return restored;
    const promise = (async (): Promise<TaskResult> => {
      if (node.preblocked) return { status: 'blocked' };
      const upstreamResults = await Promise.all(node.depends_on.map(id => results.get(id) as Promise<TaskResult>));
      const failedUpstream = upstreamResults.findIndex(result => result.status !== 'done');
      if (failedUpstream >= 0) {
        const upstreamId = node.depends_on[failedUpstream];
        const detail = `上游任务未完成，已阻断：${upstreamId}`;
        activity(`${runId}:task:${node.id.split(':').at(-1)}`, {
          kind: 'model', name: 'employee_task', title: node.title, status: 'blocked', actor: node.name,
          agent_id: node.agent_id, task_id: node.id, depends_on: node.depends_on, detail,
        });
        return { status: 'blocked' };
      }

      const restoredApproval = resumeState
        ? listXuebanApprovals(conversation.id).find(item => item.run_id === runId && item.task_id === node.id && item.status === 'decided')
        : undefined;
      if (restoredApproval?.outcome === 'reject') return { status: 'rejected' };
      if (node.requires_approval && !restoredApproval) {
        const approval = createXuebanApproval({
          company_id: input.companyId, conversation_id: conversation.id, run_id: runId,
          task_id: node.id, task_title: node.title,
          // 审批页和对话卡片都要能看到“到底批什么”：目标 + 工单正文 + 上游交接。
          content: approvalContentOf({
            goal: content,
            title: node.title,
            brief: node.approval_brief,
            handoffs: upstreamResults.map(result => result.handoff ?? ''),
          }),
        });
        activity(`${runId}:approval:${node.id.split(':').at(-1)}`, {
          kind: 'approval', name: 'approval_required', title: `待老师审批：${node.title}`, status: 'waiting_approval', actor: '教学主管',
          agent_id: node.agent_id, task_id: node.id, depends_on: node.depends_on, approval_id: approval.id,
          detail: '批准或选择继续后执行；驳回后停止本任务及其下游',
        });
        const approvalClientId = beginMessage('ceo', '教学主管', undefined, 'workflow', { id: node.id, title: node.title });
        const approvalText = `《${node.title}》需要老师确认后才能执行。请批准、驳回，或填写自定义决定并明确是否继续。`;
        input.onEvent?.({ type: 'content-delta', client_id: approvalClientId, text: approvalText });
        const approvalMessage = appendXuebanMessage(conversation.id, {
          sender_kind: 'ceo', sender_name: '教学主管', content: approvalText, channel: 'workflow', run_id: runId,
          task_id: node.id, task_title: node.title, status: 'waiting_approval', metadata: { approval },
        });
        endMessage(approvalClientId, approvalMessage);
        const outcomePromise = new Promise<XuebanApprovalOutcome>(resolve => {
          approvalWaiters.set(approval.id, { resolve, emit: input.onEvent });
        });
        input.onEvent?.({ type: 'approval', approval });
        const outcome = await outcomePromise;
        activity(`${runId}:approval:${node.id.split(':').at(-1)}`, {
          kind: 'approval', name: 'approval_decided', title: `老师已决定：${node.title}`, status: outcome === 'continue' ? 'done' : 'rejected', actor: '老师',
          agent_id: node.agent_id, task_id: node.id, depends_on: node.depends_on, approval_id: approval.id,
          detail: outcome === 'continue' ? '继续执行任务' : '停止本任务及其下游',
        });
        if (outcome === 'reject') return { status: 'rejected' };
      }

      const employee = byId.get(node.agent_id) as Employee;
      // 审批完成后、真实模型执行前落快照；恢复时不重新规划、不追加老师消息。
      let nodeCheckpointId: string | undefined = resumeTaskId === node.id ? resumeCheckpointId : undefined;
      if (!(resumeState && resumeTaskId === node.id && resumeCheckpointId)) {
        const checkpoint = createXuebanCheckpoint({
          company_id: input.companyId, conversation_id: conversation.id, run_id: runId,
          task_id: node.id, task_title: node.title,
          snapshot: {
            messages: listXuebanMessages(conversation.id), approvals: listXuebanApprovals(conversation.id),
            state: {
              content, attachments: input.attachments ?? [], previous, plan, planText, employees, knowledgeContext,
              results: [...completedResults],
              nodes: nodes.map(item => ({ id: item.id, title: item.title, agent_id: item.agent_id, name: item.name, depends_on: item.depends_on })),
              resume_task_id: node.id,
            } satisfies XuebanResumeState,
          },
        });
        nodeCheckpointId = checkpoint.id;
        // 明确写清恢复点：保留谁、重跑谁、并行分支不受影响，避免滚多/滚少。
        const scope = checkpointScope(nodes, node.id);
        const preserved = nodes.filter(item => completedResults.has(item.id)).map(item => item.title);
        const parallel = nodes
          .filter(item => !scope.has(item.id) && !completedResults.has(item.id))
          .map(item => item.title);
        const contentLines = [
          `恢复到：执行「${node.title}」之前`,
          `已完成并保留：${preserved.length ? preserved.join('、') : '无'}`,
          `本次重跑：「${node.title}」及其下游`,
          parallel.length ? `并行/无关分支（如 ${parallel.slice(0, 4).join('、')}）的成果会保留` : '无并行分支需要保护',
        ];
        const checkpointMessage = appendXuebanMessage(conversation.id, {
          sender_kind: 'system', sender_name: '教学主管', channel: 'workflow', run_id: runId,
          task_id: node.id, task_title: node.title, content: contentLines.join('\n'), status: 'done',
          metadata: { checkpoint: {
            id: checkpoint.id, task_id: node.id, task_title: node.title, sequence: checkpoint.sequence,
            resume_before: node.title, preserved_titles: preserved, rerun_titles: nodes.filter(item => scope.has(item.id)).map(item => item.title),
          } },
        });
        input.onEvent?.({ type: 'checkpoint', message: checkpointMessage });
      }
      activity(`${runId}:task:${node.id.split(':').at(-1)}`, {
        kind: 'model', name: 'employee_task', title: node.title, status: 'running', actor: employee.name,
        agent_id: employee.id, task_id: node.id, depends_on: node.depends_on, checkpoint_id: nodeCheckpointId,
      });
      const handoff = node.depends_on.map((dependency, index) => upstreamResults[index].handoff).filter(Boolean).join('\n\n');
      const prompt = [
        `教研组对话历史：\n${transcript(previous)}`,
        input.config.studentProfile ? `【学生学段】\n${input.config.studentProfile}` : '',
        `学生本轮目标：${content}`,
        `学习主管分配给你的工作：${node.title}\n${node.prompt}`,
        knowledgeContext,
        handoff ? `上游教研员向教学主管回传、现由教学主管交接给你的成果（仅限声明依赖）：\n${handoff.slice(-16_000)}` : '',
        '你是执行层教研员，只向学习主管提交本岗位成果，不向其他教研员派单。',
        '请直接产出学生能用的成果：具体内容、题目或步骤，而不是只写“建议加强”“需进一步确认”。若使用了上游成果，要实际衔接其内容，不要复述调度过程。',
        '缺少真实数据时，给出获取数据的具体小测/问题，或明确写出假设；不得编造数据、来源、外部操作或审批结果。',
      ].filter(Boolean).join('\n\n');
      const clientId = beginMessage('agent', employee.name, employee.id, executionChannel, { id: node.id, title: node.title });
      const startedAt = Date.now();
      // 执行型工单关闭长思考，避免模型把时间花在推理上。
      // 实际只需要「查列表 → 提交变更」。规划与汇总仍保留思考。
      try {
        const result = await withAgentLock(employee.id, () => complete(
          input.config, node.systemPrompt, prompt, call,
          input.onEvent ? { clientId, emit: input.onEvent } : undefined,
          undefined,
          {
            permissions: employee.tools,
            onActivity: patch => activity(`${runId}:tool:${node.id.split(':').at(-1)}:${randomUUID()}`, {
              ...patch, actor: employee.name, agent_id: employee.id, task_id: node.id,
            }),
          },
          undefined,
        ));
        const message = appendXuebanMessage(conversation.id, {
          sender_kind: 'agent', sender_name: employee.name, agent_id: employee.id, content: result.text,
          channel: executionChannel, run_id: runId, task_id: node.id, task_title: node.title,
          thinking: result.thinking, status: 'done', duration_ms: result.durationMs,
          metadata: { depends_on: node.depends_on },
        });
        endMessage(clientId, message);
        activity(`${runId}:task:${node.id.split(':').at(-1)}`, {
          kind: 'model', name: 'employee_task', title: node.title, status: 'done', actor: employee.name,
          agent_id: employee.id, task_id: node.id, depends_on: node.depends_on, checkpoint_id: nodeCheckpointId,
          output: result.text, thinking: result.thinking, duration_ms: Date.now() - startedAt,
        });
        return {
          status: 'done', message,
          handoff: `${employee.name}（${employee.title || '教研员'}）交付《${node.title}》：\n${result.text}`,
        };
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        // 失败也保留模型真实产生的思考，避免卡片上只剩一句“没有返回有效内容”。
        const failedThinking = error instanceof XuebanChatError ? error.thinking : undefined;
        const message = appendXuebanMessage(conversation.id, {
          sender_kind: 'agent', sender_name: employee.name, agent_id: employee.id,
          content: detail, channel: 'workflow', run_id: runId,
          task_id: node.id, task_title: node.title, status: 'failed',
          thinking: failedThinking, metadata: { depends_on: node.depends_on },
        });
        endMessage(clientId, message);
        activity(`${runId}:task:${node.id.split(':').at(-1)}`, {
          kind: 'model', name: 'employee_task', title: node.title, status: 'failed', actor: employee.name,
          agent_id: employee.id, task_id: node.id, depends_on: node.depends_on, detail,
          checkpoint_id: nodeCheckpointId,
          thinking: failedThinking,
        });
        return { status: 'failed', message };
      }
    })();
    results.set(node.id, promise);
    void promise.then(result => {
      if (result.status === 'done') completedResults.set(node.id, result);
    });
    return promise;
  };

  // Register promises synchronously in DAG plan order so dependents can always
  // find their upstream promise; actual independent model calls overlap.
  const taskPromises = nodes.map(node => executeNode(node));
  const settled = await Promise.all(taskPromises);
  const produced: XuebanChatMessage[] = [planMessage];
  const handoffs: string[] = [];
  for (const result of settled) {
    if (result.message) produced.push(result.message);
    if (result.handoff) handoffs.push(result.handoff);
  }

  if (handoffs.length) {
    const summaryStartedAt = Date.now();
    activity(`${runId}:summary`, {
      kind: 'model', name: 'manager_summary', title: '教学主管正在汇总教研员成果', status: 'running', actor: '教学主管',
      detail: `${handoffs.length} 个成功交付，${settled.filter(result => result.status !== 'done').length} 个未执行或失败节点 · 模型 ${managerModel}`,
    });
    const summaryClientId = beginMessage('ceo', '教学主管');
    let summary: Awaited<ReturnType<typeof complete>>;
    try {
      summary = await complete(input.config, [
        '你是智慧学伴的学习主管。现在汇总教研员成果，整理成一封直接发给学生本人的回复。',
        '读者是学生（初中生），不是老师、家长或同事：用“你”称呼，语气自然、鼓励、具体，像一个耐心的学长/老师当面讲。',
        '绝对不要出现这些内部词：验收、评审、草案、交付物、口径、授权、审批、材料编号、岗位、工单、DAG、上游、回传。',
        '按下面四段来写，用简短小标题，600-900 字：',
        '一、我先了解一下你的情况：只复述学生真的说过的信息。如果他没说成绩、错题或学习时间，就直说“你还没告诉我……”，然后给 4-6 个具体问题，或 5-8 道带答案的入门小测题，让他做完你就能判断水平。',
        '二、给你的学习计划：按天或按周列，每次写清楚做什么、多长时间、用什么材料或题目；先补基础，再练综合，不要只给框架。',
        '三、怎么检查自己学会了：每阶段给一个自测标准（例如“10 道题独立做对 8 道”），以及卡住时的具体处理办法。',
        '四、下一步：一句话鼓励 + 建议他先做哪一件事。',
        '不得编造学生的成绩、排名、薄弱点、学校或老师信息；不得声称已联系老师/家长或已代为报名。不要提审批。',
        '如果某个环节没有完成：用一句自然的话说明“这部分暂时没拿到结果”，然后基于已有信息继续给出可执行建议；不要假装完成，也不要用“失败/阻塞”这类词吓学生。',
        '如果教研成果里有事实性材料，用学生能懂的话转述，必要时在句末注明出处（如“参考人教版七年级上册”）。',
      ].join('\n'), [
        input.config.studentProfile ? `【学生学段】\n${input.config.studentProfile}` : '',
        `学生目标：${content}`,
        `学习主管的分工：\n${planText}`,
        `教研员回传的成果（内部材料，请转述成学生能懂的话，不要整段粘贴）：\n${handoffs.join('\n\n').slice(-24_000)}`,
        `未完成的环节：${settled.map((result, index) => result.status === 'done' ? '' : nodes[index].title).filter(Boolean).join('、') || '无'}`,
        knowledgeContext,
        '请直接写给学生本人。不要写“本轮不出通过版”这类内部结论；需要了解学情就当场给出小测或问题。',
      ].join('\n\n'), call, input.onEvent ? { clientId: summaryClientId, emit: input.onEvent } : undefined);
    } catch (error) {
      const detail = `教学主管汇总失败（模型 ${managerModel}）：${error instanceof Error ? error.message : String(error)}`;
      activity(`${runId}:summary`, {
        kind: 'model', name: 'manager_summary', title: '教学主管汇总失败', status: 'failed', actor: '教学主管',
        detail, duration_ms: Date.now() - summaryStartedAt,
      });
      const failedMessage = appendXuebanMessage(conversation.id, {
        sender_kind: 'ceo', sender_name: '教学主管', content: detail, run_id: runId, status: 'failed',
      });
      endMessage(summaryClientId, failedMessage);
      produced.push(failedMessage);
      throw error instanceof XuebanChatError ? error : new XuebanChatError(502, 'MANAGER_SUMMARY_FAILED', detail);
    }
    const summaryMessage = appendXuebanMessage(conversation.id, {
      sender_kind: 'ceo', sender_name: '教学主管', content: summary.text, run_id: runId,
      thinking: summary.thinking, status: 'done', duration_ms: summary.durationMs,
    });
    endMessage(summaryClientId, summaryMessage);
    activity(`${runId}:summary`, {
      kind: 'model', name: 'manager_summary', title: '教学主管已完成汇总', status: 'done', actor: '教学主管',
      output: summary.text, thinking: summary.thinking, duration_ms: Date.now() - summaryStartedAt,
    });
    produced.push(summaryMessage);
  }

  return {
    messages: produced,
    plan: {
      strategy: plan.strategy,
      source: plan.source,
      assignments: nodes.map(node => ({ agent_id: node.agent_id, name: node.name, title: node.title, depends_on: node.depends_on })),
      dag,
    },
  };
}
