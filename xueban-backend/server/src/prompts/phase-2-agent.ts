/**
 * phase-2-agent.ts — Phase 2 of the multi-turn skill-assembler prompt.
 *
 * Phase 2 (2026-05-22): switched from tool_use orchestration to daemon-led
 * artifact handoff. Per orchestration-transport.md §"Phase 2 Eng Review"
 * decisions A2/A3/CL6, the daemon now drives team execution: it pre-loads
 * team.yaml + agent.yaml from disk, builds the DAG with workflow/scheduler.ts,
 * and emits node/edge SSE events itself. The LLM is no longer asked to call
 * list_team_agents / get_skill_anchor / register_agent / register_edge.
 *
 * What the LLM still does in Phase 2:
 *   - Acts as the agent the daemon has scheduled (system prompt = that agent's
 *     persona, from agent.yaml#persona, fed verbatim by the daemon).
 *   - Emits <sf:agent-substep> tags to express running/done state for the
 *     5 substeps (identity / persona / model / tools / memory). These are
 *     pure event tags — they no longer carry "look at what tool I just
 *     called" semantics.
 *   - Reads its upstream artifact (e.g. docs/brief.md) and writes its own
 *     artifact (e.g. docs/architecture.md) for the next agent in the DAG.
 *
 * What the daemon does ahead of Phase 2:
 *   - Emits the <sf:node>/<sf:edge> blueprint frames for every agent in the
 *     selected subset, so the UI already shows the team graph before the
 *     LLM produces a single token.
 *
 * Round 3 P0 fix (2026-05-24): the <sf:step name="配置 Agent 角色"> tag's
 * `output_kind` was changed from "nodes" → "none" because the LLM no longer
 * emits <sf:node> tags itself (per A3, daemon-led). The parser's S2.2
 * output_kind gate (parser.ts:200) was firing STEP_NO_OUTPUT errors against
 * BMAD live runs because LLM correctly followed the "don't emit node" rule
 * yet the step still declared `output_kind="nodes"`. The agent-substep frames
 * remain the LLM's actual observable output for this step.
 */

export const PHASE_2_AGENT = `# Phase 2 · 为每个选中的 Agent 推进 5 个 substep

## 背景：daemon 已经建好图

到 phase 2 时，daemon 已经从 team.yaml + agent.yaml 解析出全部启用的 agent，
并预先 emit 了 \`<sf:node>\` / \`<sf:edge>\` 蓝图帧。前端左侧节点列表已经渲染。
**你不需要、也不应该再尝试注册 agent 或边** —— 那是 daemon 的事。

你在 phase 2 的职责只有一件事：作为 daemon 调度到你的那个 agent，按 5 个
substep 顺序 emit 状态帧，并按 system prompt 完成产出。

## 总框架

emit \`<sf:step name="配置 Agent 角色" output_kind="none" status="running"/>\`

然后**对你被调度执行的那个 agent**，按顺序 emit 5 个 substep。
未被调度到的 agent 由 daemon 自己 emit pending 状态，无需你处理。

最后 emit \`<sf:step name="配置 Agent 角色" output_kind="none" status="done"/>\`.

> **output_kind 说明**：这一步的 \`output_kind="none"\`，不是 "nodes"。
> daemon 负责 emit \`<sf:node>\` 蓝图帧；本步 LLM 只产 \`<sf:agent-substep>\` 状态帧，
> 所以从 parser 角度看本步没有"建图"产出，标 none 让 step-gating 不会误报
> STEP_NO_OUTPUT。

## 单个 agent 的 5 substep 推进顺序

按顺序、不可跳：identity → persona → model → tools → memory.

每个 substep 的形态：

\`\`\`
<sf:agent-substep node_id="<your_agent_id>" substep="<slot>" status="running"/>
... (你的内容输出 / 思考过程)
<sf:agent-substep node_id="<your_agent_id>" substep="<slot>" status="done"
   source="<agent>.agent.yaml#<slot>" tokens="<n>" cached="true"/>
\`\`\`

\`source\` / \`tokens\` / \`cached\` 字段的值由 daemon 在你的 system prompt 上下文里
告诉你（agent.yaml 解析后的元数据），原样回填即可。

### identity substep
identity 只是占位 — 标记"我开始作为这个 agent 工作"。emit running, 然后立刻
emit done（无 source/tokens/cached）。

### persona / model / tools / memory substep
这 4 个 substep 对应你身份的 4 个切面。emit running → 简短表达"我读到了我的
persona / model / tools / memory 配置" → emit done，把 daemon 在 context 里
提供的 \`ref\` / \`tokens\` 元数据原样填进 done 帧的属性。

## artifact handoff（这才是 phase 2 的核心产出）

你的 system prompt 会告诉你：
- 你的上游 agent 把成果写在哪个文件（例：\`docs/brief.md\`）
- 你需要把自己的产出写到哪个文件（例：\`docs/architecture.md\`）

读上游文件 → 按 persona 工作 → 写下游文件。下一层节点的 LLM 会在新一轮 turn
里读你刚写的文件，daemon 用 DAG scheduler 串起整条流水线。

## 强制规则

- **不要** emit \`<sf:node>\` / \`<sf:edge>\` / \`<sf:agent-persona>\` 这些建图帧。
  daemon 已经发完了，重复 emit 会导致前端节点重复渲染。
- **不要** 尝试调用任何 register_agent / register_edge / list_team_agents /
  get_skill_anchor 工具 —— 它们不存在于 phase 2 的可用工具集。
- 5 个 substep 顺序固定（identity → persona → model → tools → memory），
  每个 substep 都要单独 emit running + done 一对帧。
- substep done 帧的 \`source\` / \`tokens\` / \`cached\` 必须与 daemon 在你 context
  里提供的 anchor 元数据一致（例: \`source="reader.agent.yaml#persona"\`），
  不要瞎编。
- 产出物（artifact 文件）必须严格写到 system prompt 指定的路径；DAG scheduler
  靠这个路径把成果传给下游节点。
`;
