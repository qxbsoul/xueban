# .xueban/skills/

Skill 数据层。每个子目录一个 skill。文件 source-tracked（per `.gitignore` 例外条款）。

## 当前 skills

| ID | team_ref | mode | 用途 |
|---|---|---|---|
| `paper-review` | paper-review | blueprint | 4-agent 学术论文评审（coord/reader/critic/writer） |
| `bmad` | bmad | blueprint | BMAD 四角研发团队（pm/arch/dev/qa） |
| `web-prototype` | — | prototype | 单文件 HTML 原型生成（无 team，走 legacy prompt） |
| `report` | — | report | Markdown 研究报告（无 team） |
| `agent-team-blueprint` | — | blueprint | 裸 LLM 自由组团（无 team_ref，走 agent-first 流） |
| `editor-export-demo`, `test-15-28-*` | — | various | Storied 测试 skill |

## 结构

```
.xueban/skills/<id>/
  SKILL.md                ← 必需。frontmatter (name/description/mode/team_ref/allowed-tools) + body
  assets/ (optional)      ← side-files，loadSkillSideFiles 自动注入 prompt
  references/ (optional)
```

## SKILL.md frontmatter 关键字段

```yaml
---
name: 用户可读名
description: 一句话能力描述
mode: blueprint | prototype | report
preview_type: yaml | html | markdown
team_ref: <team-id>       # 关键：指向 .xueban/teams/<id>.team.yaml（S0.5）
allowed-tools:            # 关键：S6 PermissionPolicy 派生源
  - list_team_agents
  - get_skill_anchor
  - register_agent
  - register_edge
example_prompt: 触发例
---

# body — 给 LLM 看的工作流指导（S9）
```

## ALWAYS / NEVER 规则（S9 各 SKILL.md body）

- ALWAYS：verbatim persona 来自 `get_skill_anchor` / model.id 与 yaml 一致 / 5 substep 全 emit / 三相位顺序
- NEVER：造 yaml 缺的 agent / paraphrase persona / hard-code 全启 4 agent / 跳 substep

## 关联目录

- `.xueban/agents/*.agent.yaml` — agent 模板（S0.5 全局库），skill 通过 team_ref 引用
- `.xueban/teams/*.team.yaml` — team 蓝图（S0.5）+ DAG layout 持久化（S0.7）

## 增加新 skill

1. 新建 `.xueban/skills/<new-id>/SKILL.md`
2. （可选）`.xueban/teams/<new-id>.team.yaml` + `agents/*.agent.yaml` 关联 agent
3. `POST /api/skills/reload` 或重启 backend
4. 前端 picker `/` 或 `@` 立即可见

## 跨边界规则

- yaml 是 **source of truth**。sqlite agents 表只为 UI Quick Hire 创建的实例（S0.6 merge view 双向呈现）
- 编辑 yaml 后通过 `clearTeamCache()` / mtime 自然失效，runtime 无需重启
- 不要在 SKILL.md frontmatter 写敏感信息（被 logger / API JSON 暴露）
