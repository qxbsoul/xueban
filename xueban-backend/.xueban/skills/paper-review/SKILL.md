---
name: 论文评审团队
description: 4-agent 学术论文评审流水线 (reader → critic → writer)，基于 BMAD 编排模式
mode: blueprint
preview_type: yaml
platform: web
scenario: research
fidelity: high
team_ref: paper-review
example_prompt: "审一下这篇 arXiv 论文，重点看 methodology 部分"
allowed-tools: []
---

<!--
Phase 2 (2026-05-22): switched from tool_use orchestration to daemon-led
artifact handoff. Per orchestration-transport.md §"Phase 2 Eng Review"
decisions A2/A3/CL6, the daemon now drives team execution: it parses
team.yaml + agent.yaml, builds the DAG with workflow/scheduler.ts, and
emits <sf:node>/<sf:edge> SSE frames itself. The 4 SkillAnchorTool
references (list_team_agents / get_skill_anchor / register_agent /
register_edge) have been removed — they are no longer LLM-callable here.
The LLM acts as the agent the daemon scheduled and exchanges work via
artifact files (docs/extracted-sections.md → docs/critique.md → ...).
-->

# 论文评审团队 Skill

本 skill 是一个 **Team Blueprint**——通过 `team_ref: paper-review` 引用全局 team 库（`.xueban/teams/paper-review.team.yaml`），agent 内容来自全局 agent 库（`.xueban/agents/{coord,reader,critic,writer}.agent.yaml`）。

daemon 负责把 yaml 解析、建图、发节点蓝图帧；agent LLM 只在自己被调度的那一步发 substep 帧、读上游 artifact、写下游 artifact。前端 AgentDetail v3 stacked 视图显示 `from <agent>.agent.yaml#<slot>` 的溯源标，让用户知道 persona / model / tools 是从 yaml cache 取出来的、不是 LLM 现造的。

## 用户场景

何时启用本 skill：

- "审一下这篇 arXiv 论文"
- "评审这篇 NeurIPS 论文，重点 methodology"
- "给我读这篇 paper 的 contribution"
- "帮我看下这篇论文的 related work 站得住吗"
- 关键词：论文 / paper / arxiv / review / 评审 / 审稿 / NeurIPS / ICML / ICLR

不适用（应拒绝并改建议其他 skill）：

- 写综述 / literature review（应用 literature-survey skill）
- 翻译论文（应用 translator skill）
- 写自己的论文（应用 paper-writer skill）
- 通用阅读笔记（无需 4-agent 编排，单 agent 即可）

## 编排模型（daemon-led + artifact handoff）

daemon 在 LLM 跑之前完成：

1. 读 `.xueban/teams/paper-review.team.yaml`，按 user goal 决定启用哪几个 agent（全 4 个 vs 只 reader+critic 等）。
2. 从 `.xueban/agents/{coord,reader,critic,writer}.agent.yaml` 加载 persona / model / tools / memory 文本，作为各 agent LLM turn 的 system prompt 上下文。
3. 用 `workflow/scheduler.ts` 跑 DAG，按拓扑顺序调度每个 agent。
4. 每个节点开始时 emit `<sf:node>`，每条边 emit `<sf:edge>`，未启用的 agent emit pending `<sf:agent-substep>`。前端 AgentDetail 据此渲染。

agent LLM 在被调度的 turn 内只做三件事：

1. emit 自己的 5 个 substep 帧（identity → persona → model → tools → memory，每个一对 running/done）。
2. 读 system prompt 指定的上游 artifact 文件（例：`docs/extracted-sections.md`）。
3. 按 persona 工作，把产出写到 system prompt 指定的下游 artifact 文件（例：`docs/critique.md`）。

典型 paper-review 串：`docs/paper-meta.md` (reader 从 PDF 提取) → `docs/critique.md` (critic 评 methodology / baseline / related work) → `docs/review.md` (writer 整合成 conf-style review)。coord（如启用）负责跨 agent 的工单分派与冲突仲裁。

## 协议 / 事件（XML tag）

LLM 在自己 turn 里可 emit 的协议帧（这些是事件协议，不是工具调用）：

- `<sf:thinking step="...">...</sf:thinking>` — 思考块
- `<sf:agent-substep node_id="..." substep="..." status="running|done" source="..." tokens="..." cached="true"/>` — 5 substep 推进帧
- `<sf:step name="..." output_kind="..." status="..."/>` — 步骤起止帧
- `<artifact path="...">...</artifact>` — 写文件（runtime 负责落盘到 workspace）

LLM **不可** emit 的（由 daemon 负责）：

- `<sf:node>` / `<sf:edge>` — daemon 建图时已经发完
- `<sf:agent-persona>` — daemon 用 agent.yaml 直接喂 system prompt

## ALWAYS

- 5 个 substep 顺序固定：identity → persona → model → tools → memory；每个 substep 必须一对 running + done 帧。
- substep done 帧的 `source` / `tokens` / `cached` 必须填 daemon 在 system prompt context 里提供的 anchor 元数据（例：`source="reader.agent.yaml#persona"`），不要瞎编。
- 上游 artifact 文件路径以 system prompt 提供的为准，读到后才开始正式产出。
- 下游 artifact 文件路径以 system prompt 提供的为准，必须严格写到那个路径，DAG scheduler 靠它把成果传给下一节点。
- `model.id` 必须与 yaml 一致（reader → `claude-sonnet-4`，critic → `claude-sonnet-4`，writer → `claude-haiku-4` 等，以 daemon context 提供的为准）。

## NEVER

- 不要尝试 emit `<sf:node>` / `<sf:edge>` —— daemon 已经发完，重复 emit 会让前端节点列表重复渲染。
- 不要尝试调用 `list_team_agents` / `get_skill_anchor` / `register_agent` / `register_edge` 工具 —— 它们不存在于本 skill 的可用工具集（`allowed-tools: []`）。
- 不要 paraphrase persona / model / tools / memory 文本 —— daemon 已经把 yaml verbatim 喂给你的 system prompt，你只需"按这个身份工作"，不要复读或改写。
- 不要造 yaml 里没有的 agent（如"测试主管"、"论文翻译官"——本 team 只有 coord / reader / critic / writer 4 个）。
- 不要把 artifact 写到 system prompt 指定路径之外的地方，scheduler 找不到就断流水线。

## 示例 · "审一篇 NeurIPS 论文 methodology" goal

daemon 决策（基于 goal 和 team.yaml）：只启用 reader + critic，DAG 为 `reader → critic`，writer / coord pending。

执行顺序：

1. **reader turn**：读 user 给的 PDF，产出 `docs/extracted-sections.md`（重点抽 methodology 段落）。emit 5 substep 帧。
2. **critic turn**：读 `docs/extracted-sections.md`，产出 `docs/critique.md`（评 methodology / baseline / metric 合理性）。
3. writer / coord 节点 daemon emit pending 灰色状态，未实际触发 LLM turn。

最终 team 输出：2 个 active agent + 2 个 pending（writer / coord 灰色）+ 1 个 artifact 链（extracted → critique）。

## 示例 · "完整审稿，要 conf-style review 文档" goal

daemon 决策：启用全部 4 个 agent，DAG 为 `coord → reader → critic → writer`。

执行顺序：

1. **coord turn**：拆 review 任务（结构 / methodology / 实验 / related work），产出 `docs/review-plan.md`。
2. **reader turn**：按 plan 从 PDF 抽内容，产出 `docs/extracted-sections.md`。
3. **critic turn**：评 reader 抽出的各段，产出 `docs/critique.md`。
4. **writer turn**：把 critique 整合成 conf-style review 文档，产出 `docs/review.md`。

最终 team 输出：4 个 active agent + 0 个 pending + 完整 artifact 链。
