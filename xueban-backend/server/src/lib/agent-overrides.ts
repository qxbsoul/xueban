/**
 * agent-overrides.ts — yaml 模板 agent 的运行时字段覆盖（2026-11-04）。
 *
 * 背景：`.xueban/agents/<id>.agent.yaml` 是设计时模板（re-seed 会还原，
 * Node 侧视其为只读），但它们经 GET /api/agents 的合并语义进入前端——
 * 灵魂页里这些 agent 同样能配 Claw 绑定 / DiceBear 头像。此前 PATCH
 * /api/agents/:id 直落 proxyFallback 转 Python，Python 只认 agent-*.json，
 * 语义 id（pm / dev / reader…）一律 404 → 前端「保存头像」报 Agent API error 404。
 *
 * 方案：PATCH yaml 模板时，把可安全覆盖的字段写入同目录 sidecar
 * `<id>.overrides.json`（与 DELETE 的 yaml 回退对称）；GET /:id 与 GET /
 * 合并时再贴回 record。只允许白名单字段（头像 / Claw 绑定），persona /
 * soul 等设计时字段仍走 yaml 本体编辑，不在这里放行。
 *
 * 存储格式：{ "patch": {...}, "updated_at": iso }；patch 里的 "" 值与
 * Python PATCH 同语义（= 显式清空）。
 */

import fs from 'fs';
import path from 'path';

// 与 agent-yaml.ts 相同的双目录解析（root 优先，server-local 兜底）。
const ROOT_AGENTS_DIR = path.join(process.cwd(), '..', '.xueban', 'agents');
const LOCAL_AGENTS_DIR = path.join(process.cwd(), '.xueban', 'agents');

const VALID_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

/** 允许通过 sidecar 覆盖的字段白名单（展示层配置，不碰设计时语义）。 */
const OVERRIDABLE_FIELDS = [
  'claw_id',
  'avatar_color',
  'avatar_style',
  'avatar_seed',
  'avatar_image',
] as const;

export type AgentOverrideField = (typeof OVERRIDABLE_FIELDS)[number];

export interface AgentOverrides {
  patch: Partial<Record<AgentOverrideField, string | null>>;
  updated_at?: string;
}

function resolveOverrideFile(agentId: string): string | null {
  if (!VALID_ID_RE.test(agentId)) return null;
  for (const dir of [ROOT_AGENTS_DIR, LOCAL_AGENTS_DIR]) {
    const candidate = path.join(dir, `${agentId}.overrides.json`);
    if (fs.existsSync(candidate)) return candidate;
  }
  // 不存在时落在哪个目录？跟 yaml 本体的解析顺序一致：root 有 yaml 落 root，
  // 否则落 local（resolveAgentFile 同语义）。这里简化：root 目录存在即用 root。
  if (fs.existsSync(path.join(ROOT_AGENTS_DIR, `${agentId}.agent.yaml`))) {
    return path.join(ROOT_AGENTS_DIR, `${agentId}.overrides.json`);
  }
  return path.join(LOCAL_AGENTS_DIR, `${agentId}.overrides.json`);
}

/** 读取某 agent 的 overrides；无文件/解析失败 → null。 */
export function loadAgentOverrides(agentId: string): AgentOverrides | null {
  if (!VALID_ID_RE.test(agentId)) return null;
  for (const dir of [ROOT_AGENTS_DIR, LOCAL_AGENTS_DIR]) {
    const candidate = path.join(dir, `${agentId}.overrides.json`);
    if (!fs.existsSync(candidate)) continue;
    try {
      const parsed = JSON.parse(fs.readFileSync(candidate, 'utf-8')) as AgentOverrides;
      if (!parsed || typeof parsed !== 'object' || typeof parsed.patch !== 'object') return null;
      return parsed;
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * 把 overrides 贴到一个 record 上（GET 合并语义）。mutates 并返回 record。
 * 只贴白名单字段；undefined 表示该字段无覆盖（保持 record 原值）。
 */
export function applyAgentOverrides<T extends Record<string, unknown>>(
  record: T,
  overrides: AgentOverrides | null,
): T {
  if (!overrides) return record;
  const mutable = record as Record<string, unknown>;
  for (const field of OVERRIDABLE_FIELDS) {
    const v = overrides.patch[field];
    if (v === undefined) continue;
    mutable[field] = v;
  }
  return record;
}

/**
 * 写入 overrides（PATCH yaml 模板时的持久化端点）。
 * 返回写入后的完整 overrides；agentId 非法 → null。
 */
export function saveAgentOverrides(
  agentId: string,
  patch: Partial<Record<AgentOverrideField, string | null>>,
): AgentOverrides | null {
  if (!VALID_ID_RE.test(agentId)) return null;
  const clean: Partial<Record<AgentOverrideField, string | null>> = {};
  for (const field of OVERRIDABLE_FIELDS) {
    const v = patch[field];
    if (v === undefined) continue;
    // 与 Python PATCH 同语义："" → null（显式清空）
    clean[field] = typeof v === 'string' && v.trim() === '' ? null : v;
  }
  const target = resolveOverrideFile(agentId);
  if (!target) return null;
  const next: AgentOverrides = { ...loadAgentOverrides(agentId) ?? { patch: {} }, patch: { ...(loadAgentOverrides(agentId)?.patch ?? {}), ...clean }, updated_at: new Date().toISOString() };
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, JSON.stringify(next, null, 2), 'utf-8');
  return next;
}
