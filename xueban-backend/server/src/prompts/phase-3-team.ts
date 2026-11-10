/**
 * phase-3-team.ts — Phase 3 of the multi-turn skill-assembler prompt.
 *
 * Phase 2 (2026-05-22): switched from tool_use orchestration to daemon-led
 * artifact handoff. Per orchestration-transport.md §"Phase 2 Eng Review"
 * decisions A2/A3/A4/CL6, the DAG (nodes + edges) is built by the daemon
 * from team.yaml ahead of the LLM ever running; workflow/scheduler.ts runs
 * the graph (topological parallel + conditional + per-node retry) and emits
 * <sf:edge> frames itself. The LLM no longer calls register_edge.
 *
 * What the LLM still does in Phase 3:
 *   - Emits two "thinking-only" steps ("设置工具集" / "Policy 协作规则") so the
 *     UI step list shows the wrap-up phase, with <sf:thinking> commentary.
 *   - Emits the wrap-up <sf:step> for "配置 Team Workflow" so the user sees
 *     the workflow-config step finish (daemon will have already emitted the
 *     edges by this point).
 *   - emits <sf:complete redirect="/editor"/> to terminate the turn.
 *
 * Round 3 P0 fix (2026-05-24): the <sf:step name="配置 Team Workflow"> tag's
 * `output_kind` was changed from "edges" → "none" for the same reason as
 * phase-2-agent.ts's "配置 Agent 角色" — daemon emits <sf:edge> frames, the
 * LLM doesn't, so the parser's output_kind gate was incorrectly firing
 * STEP_NO_OUTPUT against BMAD live BYOK runs.
 */

export const PHASE_3_TEAM = `# Phase 3 · 收尾：工具集 / Policy / Workflow 帧 / complete

## 背景：DAG 已经由 daemon 建好

team.yaml 里的 edge（sequential / parallel / conditional + max_retries +
condition）由 daemon 在 phase 1 之前就解析完成，并由 workflow/scheduler.ts
按拓扑顺序执行。daemon 自己 emit \`<sf:edge>\` 帧到 SSE，前端按 node_id 路由。
**phase 3 不需要、也不应该再尝试创建边** —— 没有 register_edge 工具可用。

你在 phase 3 的职责只有三件事：发两个 thinking-only 帧、发一个 workflow 总结
帧、然后 complete。

## 3.1 设置工具集（thinking-only step）

emit \`<sf:step name="设置工具集" output_kind="none" status="running"/>\`
emit \`<sf:thinking step="设置工具集">\` 块：用一段中文说明
 - 每个 agent 的 tools 是否合理（基于 agent.yaml#tools 的 picked 列表）
 - 有没有重复 / 缺失（比如团队里没人能写文件）
emit \`<sf:step name="设置工具集" output_kind="none" status="done"/>\`

这一步纯思考，不调任何工具、不输出 artifact。

## 3.2 Policy 协作规则（thinking-only step）

emit \`<sf:step name="Policy 协作规则" output_kind="none" status="running"/>\`
emit \`<sf:thinking step="Policy 协作规则">\` 块：用一段中文说明
 - 失败如何 retry（team.yaml 里哪条 edge 配了 conditional + max_retries）
 - 升级路径（哪个 agent 是兜底）
emit \`<sf:step name="Policy 协作规则" output_kind="none" status="done"/>\`

## 3.3 配置 Team Workflow（总结帧）

emit \`<sf:step name="配置 Team Workflow" output_kind="none" status="running"/>\`

此时 daemon 已经按 team.yaml 把所有 \`<sf:edge>\` 帧发完了。你**不要**再尝试
emit edge 或调用任何 register_edge 工具 —— 只需用一两句中文总结："已按 team.yaml
连好 N 条 sequential / M 条 conditional 边"，作为 UI 步骤列表里这一步的注脚。

emit \`<sf:step name="配置 Team Workflow" output_kind="none" status="done"/>\`

> **output_kind 说明**：本步 \`output_kind="none"\`，不是 "edges"。
> daemon 负责 emit \`<sf:edge>\` 帧；LLM 这一步只做总结性 thinking 输出，
> 没有"建图"产出，标 none 让 parser 的 step-gating 不会误报 STEP_NO_OUTPUT。

## 3.4 完成

emit \`<sf:complete redirect="/editor"/>\`

之后什么都不要再 emit。runtime 会把 \`<sf:complete>\` 翻译成 SSE \`event:
'complete'\` 终止该 turn。

## 强制规则

- **不要**在 phase 3 里 emit \`<sf:node>\` / \`<sf:edge>\` / \`<sf:agent-substep>\`
  —— node/edge 是 daemon 的事，substep 是 phase 2 的事。
- **不要** 调用任何 register_agent / register_edge / list_team_agents /
  get_skill_anchor 工具 —— 它们不存在于 phase 3 的可用工具集。
- "设置工具集" 和 "Policy 协作规则" 都是 thinking-only 步骤，绝对不允许 ToolUse。
- 单 agent team（team.yaml 只有 1 个 node）会被 daemon 安排零 edge，phase 3 流程
  完全不变：照样发 3.1 / 3.2 / 3.3 / 3.4 四块。
`;
