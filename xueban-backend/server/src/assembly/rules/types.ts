// Rule = 设底线的可执行硬约束（文章：Rule 强制原则约束）。与 Skill(recipe) 分目录。
export type AssemblyRule =
  | { kind: 'roster_max'; max: number; reason: string }
  | { kind: 'require_coordinator'; reason: string }
  | { kind: 'role_whitelist'; allowed: string[]; reason: string };

/** 装配出口被校验的最小节点形状（解耦 RunSessionNode / TeamDef）。 */
export interface RosterNode {
  role_id: string;
  type: 'coordinator' | 'agent';
  title: string;
}
