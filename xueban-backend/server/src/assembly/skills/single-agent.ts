import type { AssemblyRecipe } from './types';

/** 用户明确要"一个 agent" → 单节点（coordinator 兼执行者），无边。 */
export const SINGLE_AGENT_RECIPE: AssemblyRecipe = {
  id: 'single-agent',
  description: '单个 agent：用户明确要一个 agent 时，只建一个、兼协调与执行',
  match: { singleAgent: true },
  // Post-assembly UX: after the single agent's team auto-saves into the
  // current workspace, ask the user whether to move it to a fresh workspace.
  // Single-agent only — multi-agent recipes omit this flag and never prompt.
  askWorkspaceOnCreate: true,
  roles: [
    {
      role_id: 'coordinator',
      type: 'coordinator',
      title: '助手',
      // hint 会被 recipeToTeamDef 烤进 persona（system prompt 的全部内容；具体
      // 任务由 runDag 的 goal 串进 user-turn）。骨架要薄但不能空——给清晰的
      // 单兵作业守则：直接交付、独立完成、不要演多角色协作。
      hint: [
        '你是一个独立工作的单个 agent，没有团队、没有可交接的下游同事。',
        '用户的具体任务通过对话消息给你——直接、完整地完成它并交付最终结果，',
        '不要假装在和别的 agent 协作、不要等待审批或交接，也不要把工作拆给不存在的角色。',
      ].join(''),
    },
  ],
  edges: [],
};
