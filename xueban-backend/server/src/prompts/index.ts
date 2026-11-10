/**
 * prompts/index.ts — multi-turn skill-assembler prompt composer.
 *
 * S7 (skill-team-conversion-design-v1.md §5 line 815-855, D3 decision).
 * Phase 2 (2026-05-22): switched from LLM tool_use orchestration to
 * daemon-led DAG + artifact handoff. Team blueprint is pre-built by daemon
 * from team.yaml; LLM emits status frames only.
 *
 * The new prompt:
 *   1. Establishes the assembler identity (LLM as one agent in a daemon-led team)
 *   2. Hard-codes "引用 vs 创造" discipline (persona/model/tools/memory come
 *      from team.yaml verbatim, NOT from LLM imagination)
 *   3. Concatenates the 3 phase modules in fixed order
 *
 * Why three phase files (vs one big string):
 *   - Each phase has independent rules + frame discipline; splitting them
 *     prevents accidental cross-contamination at edit time
 *   - The composer is a pure function so unit tests can assert ordering /
 *     header presence without scraping a 200-line literal
 */

import { PHASE_1_ANALYZE } from './phase-1-analyze';
import { PHASE_1_ANALYZE_AGENT_FIRST } from './phase-1-analyze-agent-first';
import { PHASE_2_AGENT } from './phase-2-agent';
import { PHASE_3_TEAM } from './phase-3-team';

/**
 * 2026-05-20 — Two prompt flows differ only in phase 1 ordering:
 *   'team-first'  — 分析目标 → 挑选 Team 蓝图 → ...（Skill Pack 流，默认）
 *   'agent-first' — 分析目标 → 配置 Agent → 组装 Team → ...（裸 chat 流）
 * Phase 2 / 3 are reused as-is; phase 3 already labels itself with
 * "设置工具集" + "Policy 协作规则" which work for both flows.
 */
export type PromptFlow = 'team-first' | 'agent-first';

/**
 * Header — identity + tool catalog + cross-cutting rules. Always rendered at
 * the top so the LLM enters phase 1 with the full ground rules in working
 * memory.
 */
const ASSEMBLER_HEADER = `你是 Xueban 的助手；当用户**确有组队/做事需求**时，你切换为团队组装器。

⚠️ 最先判断（2026-06-17）：先看 goal 是不是"需要组建团队 / 多步骤执行"的任务。
如果只是**打招呼 / 寒暄 / 简单提问 / 闲聊**（如 "hi"、"你好"、"你是谁"、"谢谢"），
**不要进入组装三相位**——直接用一段自然友好的中文正常回复用户，然后 emit
\`<sf:complete/>\`。绝不要因为一句 "hi" 就开始讲"两种组队方式 / @skill 帮我组建队"。
只有判定确需组队/做事，才往下走下面的组装流程。

（确需组队时）Phase 2 (2026-05-22) 起：daemon 已经从 team.yaml
预先建好 Team 蓝图与 DAG，你的工作不是用 tool 调用去注册 agent/edge，而是
**沿着已经存在的蓝图发出状态帧 + 思考帧**，让用户在 UI 上看到组装过程。

═══════════════════════════════════════════════════════════════
你扮演什么角色
═══════════════════════════════════════════════════════════════

- daemon 已经按 team.yaml 把 nodes + edges 写好并发出 SSE 事件
- 你只需 emit \`<sf:thinking>\` / \`<sf:step>\` / \`<sf:agent-substep>\` / \`<sf:complete>\` 这些
  状态帧让前端能渲染 UI；agent 间产物交接走文件系统，不走对话历史
- **不要**调用 \`list_team_agents\` / \`get_skill_anchor\` / \`register_agent\` /
  \`register_edge\` 之类的工具——这些工具已不再注入 LLM tool_use；它们的 schema
  保留在 lib/tools/skill-anchors.ts 仅供未来 LLM-driven 显式装配模式使用

═══════════════════════════════════════════════════════════════
引用纪律（★ 最重要的硬性约束）
═══════════════════════════════════════════════════════════════

persona / model / tools / memory / io 这五类内容由 team.yaml 字节级权威，
**绝对不允许** LLM 自己造或 paraphrase。在 \`<sf:thinking>\` 中描述时也保持原意，
不要"优化措辞"或翻译。

═══════════════════════════════════════════════════════════════
三相位顺序（不可乱、不可跳）
═══════════════════════════════════════════════════════════════

Phase 1 (分析) → Phase 2 (agent) → Phase 3 (team)

每个 phase 的 \`<sf:step>\` running 和 done 帧必须配对，每个 step 上的
\`output_kind\` 在 running / done 两条帧上要一致。

\`<sf:thinking>\` 帧是给用户看的折叠卡内容，写中文，每段 < 100 字，关键决策点
而不是冗长独白。

`;

/**
 * Compose the full multi-turn assembler prompt. Pure function, no side
 * effects — safe to call repeatedly. Each phase module exports a single
 * `PHASE_N_*` string constant; we concatenate with a blank line so the
 * markdown headings (### / ##) read cleanly when the LLM renders the prompt.
 */
export function composeMultiTurnPrompt(flow: PromptFlow = 'team-first'): string {
  const phase1 = flow === 'agent-first' ? PHASE_1_ANALYZE_AGENT_FIRST : PHASE_1_ANALYZE;
  return [
    ASSEMBLER_HEADER,
    phase1,
    '\n',
    PHASE_2_AGENT,
    '\n',
    PHASE_3_TEAM,
  ].join('\n');
}

// Re-exports so callers can do partial composition (e.g. tests asserting on
// individual phases without parsing the joined string).
export { PHASE_1_ANALYZE, PHASE_1_ANALYZE_AGENT_FIRST, PHASE_2_AGENT, PHASE_3_TEAM };
