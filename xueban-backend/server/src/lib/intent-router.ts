/**
 * intent-router.ts — TypeScript port of `xueban/runtime/intent_router.py`.
 *
 * Lightweight keyword-based goal classifier. Only classifies intent —
 * does NOT pick agents or compose teams. Team/agent composition is decided
 * downstream by the Assembler LLM, which is fed parsed skill content plus
 * this classifier's output.
 *
 * Faithful 1:1 port of the Python version: keyword lists, priority order
 * (workflow > review > report > answer), confidence values, complexity
 * scores, and the report/team default fallback are all preserved.
 *
 * Public surface for Stream A consumers in routes/run-sessions.ts:
 *
 *   classifyTS(goal: string): ClassifyResult
 *
 * Note: the design doc spec asks for kind ∈ {chat, clarify, task} with
 * confidence + reasons. The Python source emits richer output_type/mode
 * (answer | report | review | workflow) × (single | team) along with a
 * complexity score. We expose BOTH:
 *   - classifyTS()      — design-doc-compliant {kind, confidence, reasons}
 *   - classifyIntent()  — full Python-equivalent IntentResult shape
 * so the caller (Stream A) can pick whichever it needs without forcing a
 * second rewrite later.
 */

// ─── Design-doc public types (S1.1 spec) ───────────────────────────────────

export type IntentKind = 'chat' | 'clarify' | 'task';

export interface ClassifyResult {
  kind: IntentKind;
  confidence: number; // 0..1
  reasons: string[]; // matched keywords or rule names
}

// ─── Python-equivalent richer types (for parity with intent_router.py) ─────

export type OutputType = 'answer' | 'report' | 'review' | 'workflow';
export type AssemblyMode = 'single' | 'team';

export interface IntentResult {
  outputType: OutputType;
  mode: AssemblyMode;
  confidence: number;
  complexity: number;
}

// ─── Keyword tables (verbatim from Python: order, casing, content) ─────────

export const ANSWER_KW: readonly string[] = [
  '什么是',
  '有什么区别',
  '怎么',
  '如何',
  '解释',
  'what is',
  'how to',
  'difference',
  '是什么',
];

export const REPORT_KW: readonly string[] = [
  '调研',
  '报告',
  '分析',
  'research',
  'report',
  'survey',
  '调查',
  '综述',
];

export const REVIEW_KW: readonly string[] = [
  'review',
  '审查',
  '评审',
  '代码review',
  'code review',
  '帮我看',
  '找问题',
  'review这',
];

export const WORKFLOW_KW: readonly string[] = [
  '工作流',
  '流程',
  'workflow',
  'pipeline',
  '自动化',
  'automation',
  '可复用',
];

// ─── Core classifier (Python parity) ───────────────────────────────────────

interface KeywordHit {
  outputType: OutputType;
  mode: AssemblyMode;
  confidence: number;
  complexity: number;
  matched: string;
}

/**
 * Run keyword classification with the exact Python priority order:
 *   workflow > review > report > answer.
 * Returns the first hit (and which keyword matched) or null.
 */
function keywordClassify(goal: string): KeywordHit | null {
  const g = goal.toLowerCase();

  for (const kw of WORKFLOW_KW) {
    if (g.includes(kw)) {
      return { outputType: 'workflow', mode: 'team', confidence: 0.88, complexity: 3, matched: kw };
    }
  }
  for (const kw of REVIEW_KW) {
    if (g.includes(kw)) {
      return { outputType: 'review', mode: 'team', confidence: 0.9, complexity: 2, matched: kw };
    }
  }
  for (const kw of REPORT_KW) {
    if (g.includes(kw)) {
      return { outputType: 'report', mode: 'team', confidence: 0.87, complexity: 4, matched: kw };
    }
  }
  for (const kw of ANSWER_KW) {
    if (g.includes(kw)) {
      return { outputType: 'answer', mode: 'single', confidence: 0.85, complexity: 1, matched: kw };
    }
  }
  return null;
}

/**
 * Full Python-equivalent classify(). Honours `outputHint` override
 * (single for "answer", team otherwise), otherwise keyword-classifies,
 * otherwise falls back to the Python default of (report, team, 0.75, 3).
 *
 * Sync because the Python version's `async` is purely cosmetic — there
 * are no awaits inside it; TS callers don't need the Promise overhead.
 */
export function classifyIntent(
  goal: string,
  outputHint?: string | null,
): IntentResult {
  if (outputHint && (['answer', 'report', 'review', 'workflow'] as const).includes(outputHint as OutputType)) {
    const hint = outputHint as OutputType;
    const mode: AssemblyMode = hint === 'answer' ? 'single' : 'team';
    return { outputType: hint, mode, confidence: 1.0, complexity: 2 };
  }

  const hit = keywordClassify(goal);
  if (hit) {
    return {
      outputType: hit.outputType,
      mode: hit.mode,
      confidence: hit.confidence,
      complexity: hit.complexity,
    };
  }

  // Python default fallback
  return { outputType: 'report', mode: 'team', confidence: 0.75, complexity: 3 };
}

// ─── Explicit single-agent intent (2026-05-27) ─────────────────────────────
//
// Bug: typing "帮我创建一个开发工程师agent" into the StartPage composer produced
// a 3-agent team (架构师 / 全栈开发 / 测试工程师). Root cause — the default
// `agent-team-blueprint` skill runs the agent-first prompt, whose only sizing
// rule is "凭 goal 推导需要哪些 agent 角色". The LLM read "开发工程师" as a
// complex job and expanded it into a software squad, ignoring the user's
// literal "一个 agent".
//
// This detector is a DETERMINISTIC guard (so it's unit-testable, unlike an
// LLM prompt change). When the goal explicitly asks for a single agent AND
// does not mention a team, the caller appends `SINGLE_AGENT_DIRECTIVE` to the
// system prompt to hard-cap the roster at one node.

// Matches "一个 / 单个 / 1个 / 一名 / 一位 …… agent/智能体/员工/助手" with up to a
// short job-title gap in between ("一个开发工程师agent"), plus the English
// "a/one/single agent" forms.
const SINGLE_AGENT_PATTERNS: readonly RegExp[] = [
  /(?:一个|单个|1\s*个|一名|一位|就一个|只要一个|只.{0,3}一个)[^。！？\n]{0,10}?(?:agent|智能体|员工|助手)/i,
  /\b(?:a|one|single)\s+agent\b/i,
  /\bjust\s+one\s+agent\b/i,
];

// Negative guard — if the user also says "团队 / 小队 / team / 多个 / 几个 …",
// they want a team; do NOT force single even when a singular phrase matched.
const TEAM_INTENT_PATTERN = /团队|小队|一组|一队|多个|几个|多名|\bteam\b|team of/i;

export interface SingleAgentIntent {
  single: boolean;
  matched?: string;
}

// 前端孪生：src/lib/assemblyRules.ts（deriveRosterRule）。改正则两边同步。
/**
 * 2026-06-18 — 只读的"显式团队意图"判定。个人态默认按单 agent 跑（mode='single'）
 * 时，用它做逃生门：goal 里出现 团队/小队/多个/几个/team 等词 → 仍升级为团队。
 * **不改 TEAM_INTENT_PATTERN 本身**（孪生正则零改动），只暴露一个读取器，因此
 * 不触发 assembly-rules-sync 的孪生同步约束。
 */
export function hasExplicitTeamIntent(goal: string): boolean {
  return TEAM_INTENT_PATTERN.test((goal ?? '').trim());
}

/**
 * 2026-06-23 — 普通对话直通路判定（investigate 根因修复）。个人态默认 mode='single'
 * 且本轮**不是组装意图**时，应由一个普通对话/编码 agent 直接回答，而不是套
 * 「团队组装指挥官」人格走组装三相位。纯函数、可单测；不改孪生正则
 * （SINGLE_AGENT_PATTERNS / TEAM_INTENT_PATTERN），故不触发 assembly-rules-sync。
 *
 * 返回 true（走普通对话）当且仅当：
 *   - mode === 'single'（个人 start composer 的 Auto 默认 / 用户显式选「单」）
 *   - 非显式 @skill / skill_name（那是复现/组装意图）
 *   - 非显式「建 1 个 agent」（detectExplicitSingleAgent —— 那仍是组装意图，组装器锁 1 节点）
 *   - goal 无团队词（hasExplicitTeamIntent —— 有就该组队）
 */
export function shouldUsePlainConversation(args: {
  mode?: string | null;
  explicit_skill?: boolean;
  goal: string;
}): boolean {
  if (args.mode !== 'single') return false;
  if (args.explicit_skill) return false;
  if (detectExplicitSingleAgent(args.goal).single) return false;
  if (hasExplicitTeamIntent(args.goal)) return false;
  return true;
}

/**
 * Detect an explicit "create exactly one agent" request in the goal.
 * Returns `{ single: true, matched }` only when a singular-agent phrase is
 * present AND no team phrase is present.
 */
export function detectExplicitSingleAgent(goal: string): SingleAgentIntent {
  const g = (goal ?? '').trim();
  if (!g) return { single: false };
  if (TEAM_INTENT_PATTERN.test(g)) return { single: false };
  for (const re of SINGLE_AGENT_PATTERNS) {
    const m = g.match(re);
    if (m) return { single: true, matched: m[0] };
  }
  return { single: false };
}

/**
 * System-prompt directive appended when `detectExplicitSingleAgent` fires.
 * Written in Chinese to match the surrounding multi-turn prompt language.
 */
export const SINGLE_AGENT_DIRECTIVE = `

# ⚠️ 用户明确要求：只建 1 个 Agent

用户在 goal 里明确说了要「一个 / 单个 agent」。**严格只产出 1 个 agent 节点**，
它同时充当 coordinator 和执行者。**禁止**因为任务看起来复杂就把它扩写成多角色
团队（不要自动补架构师 / 测试 / 评审等额外角色）。

- 只 emit 1 个 \`<sf:node type="agent">\`（node_id 用 coordinator）
- 不要 emit 第二个 agent 节点，也不要 emit agent 之间的 edge
- 如果你判断任务确实需要更多角色，在 \`<sf:thinking>\` 里说明即可，但**仍然只建
  1 个**，把"是否扩成团队"留给用户后续决定
`;

// ─── Design-doc-shaped wrapper: classifyTS() ───────────────────────────────

/**
 * Map Python output_type → design-doc IntentKind:
 *   - answer        → chat     (single-turn Q&A, no team)
 *   - report/review/workflow → task  (needs a team / multi-step plan)
 *
 * "clarify" is reserved for the case where the goal is too vague to
 * classify with any keyword AND too short to be a meaningful task —
 * the classifier needs to come back to the user before assembling.
 * We treat <= 3 chars (after trim) or empty input as needing clarify.
 */
export function classifyTS(goal: string): ClassifyResult {
  const trimmed = (goal ?? '').trim();
  const reasons: string[] = [];

  if (trimmed.length === 0) {
    return { kind: 'clarify', confidence: 1.0, reasons: ['empty_goal'] };
  }
  if (trimmed.length <= 3) {
    return { kind: 'clarify', confidence: 0.8, reasons: ['too_short'] };
  }

  const g = trimmed.toLowerCase();
  const hit = keywordClassify(g);

  if (hit) {
    reasons.push(`keyword:${hit.matched}`);
    reasons.push(`output_type:${hit.outputType}`);

    if (hit.outputType === 'answer') {
      return { kind: 'chat', confidence: hit.confidence, reasons };
    }
    // report / review / workflow → multi-step team task
    return { kind: 'task', confidence: hit.confidence, reasons };
  }

  // Python default fallback was (report, team, 0.75, 3) — treat as task,
  // but mark the rule that fired so callers can tell it was a guess.
  reasons.push('fallback:default_report_team');
  return { kind: 'task', confidence: 0.75, reasons };
}
