/**
 * run-suggestions.ts — 完成态「后续建议」生成（2026-08-23）。
 *
 * run 跑完后给用户提供 2-4 条下一步建议（GAP.md 未做项 / clawsgo 通用体验）。
 * 纯逻辑层：上下文提取、prompt 组装、模型输出解析、LLM 编排；HTTP 形状转换
 * 在 routes/run-suggestions.ts，key 解析走 transport/byokKey。
 *
 * 铁律：LLM 不可用 / 失败 / 解析为空 → 返回 null，绝不编造静态假建议。
 *
 * 调用注入模式与 orchestrator-parse.ts 的 decomposeWithLlm 一致
 * （opts.callImpl ?? callProvider），测试无需真实 key / 网络。
 */

import { callProvider, type ProviderChunk, type ProviderId } from '../transport/api-clients';
import type { TimelineMessage } from './contracts';

/** 建议生成的输入上下文（goal + 产物摘要 + 最终回复一句话）。 */
export interface RunSuggestionContext {
  /** 本轮任务目标（首个 user_turn 文本 / RunRecord.goal）。 */
  goal: string;
  /** 产物标签（artifact_card 标题/文件名、commit_record 文件、file_diff 路径），去重。 */
  artifacts: string[];
  /** 本轮最后一条 assistant 回复的首行摘要（截断）。 */
  summary?: string;
}

export interface GenerateSuggestionsOptions {
  ctx: RunSuggestionContext;
  provider: ProviderId;
  apiKey?: string;
  model?: string;
  signal?: AbortSignal;
  /** 测试注入；默认走 transport/api-clients 的 callProvider。 */
  callImpl?: typeof callProvider;
}

/** 建议条数上限（prompt 要 2-4 条，解析层也截到 4）。 */
export const MAX_SUGGESTIONS = 4;

/** 单条建议的最大长度（超出截断，防止模型输出长段落撑爆卡片）。 */
const MAX_SUGGESTION_LEN = 60;

/**
 * 从终态 TimelineMessage 集提取建议上下文。
 * goal 取首个 user_turn；产物三类真产物卡（与前端 NeoDeliveryCard 同一真相源）；
 * summary 取最后一条 assistant_text 首行。
 */
export function suggestionContextFromTimeline(
  messages: Iterable<TimelineMessage>,
): RunSuggestionContext {
  let goal = '';
  let lastAssistant = '';
  const artifacts: string[] = [];
  const seen = new Set<string>();
  const push = (label: string) => {
    const key = label.trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    artifacts.push(key);
  };
  for (const m of messages) {
    if (m.kind === 'user_turn') {
      if (!goal) goal = m.text.trim();
    } else if (m.kind === 'artifact_card') {
      push(m.title?.trim() || m.filename);
    } else if (m.kind === 'commit_record') {
      for (const f of m.files ?? []) push(f.path);
    } else if (m.kind === 'file_diff') {
      push(m.path);
    } else if (m.kind === 'assistant_text') {
      lastAssistant = m.body;
    }
  }
  const firstLine = (lastAssistant.split('\n')[0] ?? '').replace(/\s+/g, ' ').trim();
  const summary = firstLine
    ? firstLine.length > 80
      ? `${firstLine.slice(0, 80)}…`
      : firstLine
    : undefined;
  return { goal, artifacts, ...(summary ? { summary } : {}) };
}

/** 组装 system / user prompt（约束模型只输出 JSON 字符串数组）。 */
export function buildSuggestionPrompts(ctx: RunSuggestionContext): {
  systemPrompt: string;
  userMessage: string;
} {
  const systemPrompt = [
    '你是 Xueban 的任务助手。用户刚完成一轮 Agent 任务，你要给出 2-4 条「下一步建议」。',
    '要求：',
    '1. 全程使用简体中文；',
    '2. 每条是一句可直接发送给 Agent 的简短指令（不超过 40 字），例如：追问产物细节、基于产物继续加工、验证或导出结果、扩展到相邻场景；',
    '3. 建议必须紧扣任务目标与实际产物，不要泛泛而谈；',
    '4. 只输出一个 JSON 字符串数组，不要任何解释、标题或代码块包裹。',
  ].join('\n');

  const lines = [`任务目标：${ctx.goal || '（未记录）'}`];
  if (ctx.artifacts.length > 0) {
    lines.push(`本轮产物：${ctx.artifacts.slice(0, 8).join('、')}`);
  }
  if (ctx.summary) {
    lines.push(`最终回复摘要：${ctx.summary}`);
  }
  return { systemPrompt, userMessage: lines.join('\n') };
}

/** 去掉行首的列表记号（- / * / • / 1. / 1、/ 1））与首尾引号。 */
function stripBullet(line: string): string {
  return line
    .replace(/^\s*(?:[-*•]|\d+\s*[.、)）])\s*/, '')
    .trim()
    .replace(/^[「"']+|[」"']+$/g, '')
    .trim();
}

/**
 * 解析模型输出 → 建议数组。优先按 JSON 数组解析（模型被要求只输出 JSON），
 * 失败则按行解析（容错 markdown 列表 / 编号）。去重、截长、截到 max 条；
 * 一条有效建议都没有 → 空数组（调用方按失败处理）。
 *
 * 行解析的防闲聊门槛：单行且无列表记号（如「无话可说」「好的」）不当作建议；
 * 多行输出或有 - / * / 编号记号的行才算候选。
 */
export function parseSuggestions(raw: string, max: number = MAX_SUGGESTIONS): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (v: string) => {
    if (v.length < 2) return;
    let s = v;
    if (s.length > MAX_SUGGESTION_LEN) s = `${s.slice(0, MAX_SUGGESTION_LEN)}…`;
    if (seen.has(s)) return;
    seen.add(s);
    out.push(s);
  };

  const text = raw.trim();
  const jsonMatch = text.match(/\[[\s\S]*\]/);
  if (jsonMatch) {
    try {
      const arr = JSON.parse(jsonMatch[0]) as unknown;
      if (Array.isArray(arr)) {
        for (const item of arr) {
          if (typeof item === 'string') push(stripBullet(item));
        }
        if (out.length > 0) return out.slice(0, max);
      }
    } catch {
      /* 落回行解析 */
    }
  }
  const candidates: { text: string; marked: boolean }[] = [];
  for (const line of text.split('\n')) {
    const rawLine = line.trim();
    if (!rawLine) continue;
    const marked = /^(?:[-*•]|\d+\s*[.、)）])\s*/.test(rawLine);
    const stripped = stripBullet(rawLine);
    if (!stripped) continue; // 空行/只有记号的行不算候选（也不计入多行门槛）
    candidates.push({ text: stripped, marked });
  }
  const qualified = candidates.length >= 2 ? candidates : candidates.filter((c) => c.marked);
  for (const c of qualified) push(c.text);
  return out.slice(0, max);
}

/**
 * LLM 编排：组装 prompt → callImpl 流式收全文 → parseSuggestions。
 * 任何失败（无 key / error chunk / 抛错 / 解析为空）→ null，绝不返回假建议。
 */
export async function generateRunSuggestions(
  opts: GenerateSuggestionsOptions,
): Promise<string[] | null> {
  const call = opts.callImpl ?? callProvider;
  const { systemPrompt, userMessage } = buildSuggestionPrompts(opts.ctx);

  let text = '';
  try {
    const stream: AsyncGenerator<ProviderChunk> = call(opts.provider, {
      systemPrompt,
      userMessage,
      model: opts.model,
      max_tokens: 512,
      temperature: 0.7,
      api_key: opts.apiKey,
      signal: opts.signal,
    });
    for await (const chunk of stream) {
      if (opts.signal?.aborted) return null;
      if (chunk.type === 'text-delta' && typeof chunk.text === 'string') {
        text += chunk.text;
      } else if (chunk.type === 'error') {
        return null;
      }
    }
  } catch {
    return null;
  }
  const parsed = parseSuggestions(text);
  return parsed.length > 0 ? parsed : null;
}
