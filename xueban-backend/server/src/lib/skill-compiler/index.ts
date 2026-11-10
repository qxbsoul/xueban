/**
 * skill-compiler/index.ts — Round 4 PR-C entry point.
 *
 * Reads a `SkillReadOutput` (PR-A verbatim file collection), asks an LLM to
 * decide single agent vs multi-agent team, validates the LLM's JSON output,
 * and emits a `CompiledSkill` that the assembler consumes at run time. The
 * compile pass is **one-time per skill content** — results are cached under
 * `.xueban/cache/skill-compile/<source_content_hash>.json` and shared
 * across every subsequent run of the same skill.
 *
 * Goal text is intentionally NOT an input. Per user spec:
 *   > "不是基于 goal。@skill 了就根据 skill 去生成 agent、team"
 * Goal participates only at run time as the user message; it does not
 * influence the agent/team decision or the team topology.
 *
 * Flow:
 *   compile(skill)
 *     1. cache lookup by content_hash → hit returns cached entry verbatim
 *     2. pick a provider/key via BYOK settings (anthropic > openai > zhipu …)
 *     3. build versioned prompt → call provider via `callProvider`
 *     4. accumulate text → parse JSON → schema-validate
 *     5. on any failure: fallbackCompile(skill, reason)
 *     6. write cache → return
 *
 * Public surface:
 *   - `compile(skill)`              full pipeline (cache → LLM → fallback)
 *   - `getCompiledSkill(skill_id)`  cache-only lookup (used by assembler)
 *
 * Both functions are best-effort and never throw on transient infra failure;
 * the worst case is `fallbackCompile()` runs and produces a degraded but
 * runnable config.
 */

import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import yaml from 'js-yaml';
import { callProvider, isProviderId, type ProviderId } from '../../transport/api-clients';
import { getSetting } from '../../storage/settings';
import type { SkillReadOutput, SkillFileEntry } from '../../skill-reader/types';
import { buildCompilePrompt } from './prompt';
import { fallbackCompile, memberIdFromFile } from './fallback';
import { readCompileCache, writeCompileCache } from './cache';
import type {
  CompiledSkill,
  CompiledAgentConfig,
  CompiledTeamConfig,
} from './types';

export type {
  CompiledSkill,
  CompiledAgentConfig,
  CompiledTeamConfig,
} from './types';
export { buildCompilePrompt } from './prompt';
export { fallbackCompile } from './fallback';
export {
  readCompileCache,
  writeCompileCache,
  _setCacheRootForTests as _setCompileCacheRootForTests,
} from './cache';

/**
 * BYOK provider priority for compile calls. Picks the first provider whose
 * key resolves successfully (either from saved settings or env var). Tied
 * to the providers we actually have ApiClient implementations for.
 *
 * `anthropic` first because the user spec says "Claude does best at code-y
 * structured output"; `zhipu` is the dev's default BYOK so it's the
 * realistic mainstream fallback on this machine; openai is the canonical
 * compat target. Others trail.
 */
const COMPILE_PROVIDER_ORDER: ReadonlyArray<ProviderId> = [
  'anthropic',
  'openai',
  'zhipu',
  'deepseek',
  'google',
  'qwen',
  'moonshot',
  'openrouter',
  'mimo',
];

/**
 * Per-provider default model for compile calls. We override the provider's
 * shipping default because compile needs a "smart enough to write good JSON"
 * tier — for Anthropic we pick Sonnet, for Zhipu we pick glm-4.6 etc.
 *
 * Override globally via `XUEBAN_COMPILE_MODEL` env var (e.g. for CI).
 */
const COMPILE_DEFAULT_MODEL: Record<ProviderId, string> = {
  anthropic: 'claude-sonnet-4-6',
  openai: 'gpt-4o-mini',
  zhipu: 'glm-4.6',
  deepseek: 'deepseek-chat',
  google: 'gemini-1.5-flash',
  qwen: 'qwen-max',
  moonshot: 'moonshot-v1-8k',
  mimo: 'mimo-v2.6-pro',
  mistral: 'mistral-large-latest',
  groq: 'llama-3.1-70b-versatile',
  openrouter: 'anthropic/claude-3.5-sonnet',
  ollama: 'llama3',
  lmstudio: 'local-model',
  azure: 'gpt-4o-mini',
};

/**
 * Allow tests to inject a fake `callProvider`. The injected fn obeys the
 * same `AsyncGenerator<ProviderChunk>` contract so the rest of the pipeline
 * is exercised end-to-end.
 */
type CallProviderFn = typeof callProvider;
let callProviderImpl: CallProviderFn = callProvider;
export function _setCallProviderForTests(fn: CallProviderFn | null): void {
  callProviderImpl = fn ?? callProvider;
}

interface ResolvedKey {
  providerId: ProviderId;
  apiKey: string;
  model: string;
  baseUrl?: string;
}

/**
 * Walk `COMPILE_PROVIDER_ORDER` and return the first provider with a usable
 * key (BYOK saved settings > per-provider env var > nothing). Returns null
 * when no provider is configured — caller falls back to rule-based compile.
 */
function resolveCompileProvider(): ResolvedKey | null {
  let byok:
    | { providers?: Record<string, { apiKey?: string; baseUrl?: string; enabled?: boolean }> }
    | undefined;
  try {
    byok = getSetting('byok') as typeof byok;
  } catch {
    byok = undefined;
  }

  const envModel = process.env.XUEBAN_COMPILE_MODEL;

  for (const providerId of COMPILE_PROVIDER_ORDER) {
    const saved = byok?.providers?.[providerId];
    // 2026-06-01 — respect the BYOK `enabled` toggle. A provider the user has
    // switched OFF in Settings must be skipped even if a (stale / wrong) key
    // still sits in its slot. Root cause of a real bug: an `anthropic` slot
    // that was disabled but held a GLM-format key got picked first (it's first
    // in COMPILE_PROVIDER_ORDER), so the compiler sent `claude-sonnet-4-6` + a
    // non-Anthropic key to api.anthropic.com → PROVIDER_ERROR → every prose
    // skill fell back to a garbage rule-based team. Skipping disabled providers
    // lets resolution fall through to the actually-enabled one (e.g. zhipu).
    if (saved && saved.enabled === false) continue;
    const savedKey = typeof saved?.apiKey === 'string' ? saved.apiKey.trim() : '';
    const envKey = process.env[providerEnvVarFor(providerId)] ?? '';
    const apiKey = savedKey || envKey;
    if (!apiKey) continue;
    return {
      providerId,
      apiKey,
      model: envModel ?? COMPILE_DEFAULT_MODEL[providerId],
      baseUrl:
        typeof saved?.baseUrl === 'string' && saved.baseUrl.trim().length > 0
          ? saved.baseUrl.trim()
          : undefined,
    };
  }
  return null;
}

function providerEnvVarFor(p: ProviderId): string {
  // Inline mapping to avoid an import cycle on PROVIDER_ENV_VAR (it's defined
  // in transport/api-clients/types.ts; importing it here would pull the full
  // ProviderInput type machinery into the compiler).
  const upper = p.toUpperCase();
  if (p === 'anthropic') return 'ANTHROPIC_API_KEY';
  return `${upper}_API_KEY`;
}

/**
 * Cache-only lookup. Used by `assembler.ts` to decide which run-time branch
 * to take. Never triggers LLM work — that happens through `compile()`,
 * which the ingest hot path runs eagerly.
 *
 * @param skill_id  skill id (matches `SkillReadOutput.skill_id`)
 * @returns The compiled skill if the cache has any entry whose skill_id
 *          matches; null otherwise. We scan because the cache is keyed by
 *          content_hash not skill_id, but skills only get ≤1 cache entry
 *          at a time so the scan is O(N) with N = installed skill count.
 */
export async function getCompiledSkill(
  skill_id: string,
): Promise<CompiledSkill | null> {
  // Direct path: when caller knows the content_hash they can hit cache
  // directly via readCompileCache. The skill_id flow needs a small index.
  const root = path.join(process.cwd(), '.xueban', 'cache', 'skill-compile');
  if (!fs.existsSync(root)) return null;
  let files: string[];
  try {
    files = await fs.promises.readdir(root);
  } catch {
    return null;
  }
  for (const f of files) {
    if (!f.endsWith('.json')) continue;
    const hash = f.slice(0, -5);
    const entry = await readCompileCache(hash);
    if (entry && entry.skill_id === skill_id) {
      return entry;
    }
  }
  return null;
}

/**
 * Extract a phase rank from an agent file path: the first integer prefixing
 * any path segment (e.g. `src/bmm-skills/2-plan-workflows/bmad-agent-pm/SKILL.md`
 * → 2). Lets a structured skill's own directory layout order the lifecycle DAG.
 * No numeric phase → sorts last (Infinity), original order preserved.
 */
function phaseRankFromPath(p: string): number {
  for (const seg of p.split('/')) {
    const m = seg.match(/^(\d+)[-_]/);
    if (m) return parseInt(m[1], 10);
  }
  return Number.POSITIVE_INFINITY;
}

const STRUCTURED_PERSONA_MAX = 4000;

/**
 * Path A 构造配方(SPEC),由组装 skill 的 yaml `path_a_structured` 块声明。
 * 这是 "怎么造"(Skill 维度);"不许越的线"(roster 上限等 Rule)不在这,留在
 * assemblyRules + enforceRules。改 yaml 即改建队行为,无需改本文件。
 */
interface StructuredSpec {
  members_source: string; // 'agent_files'
  edge_strategy: string;  // 'phase-dir-order' | 'sequential'
  raci_rules: string;     // 声明式(矩阵由前端 deriveRaci 派生);引擎不计算 RACI
  persona: string;        // 'verbatim'
}

const DEFAULT_STRUCTURED_SPEC: StructuredSpec = {
  members_source: 'agent_files',
  edge_strategy: 'phase-dir-order',
  raci_rules: 'role-table',
  persona: 'verbatim',
};

let _structuredSpecCache: StructuredSpec | null = null;

/**
 * 读组装 skill 的 `assembly_workflow.yaml` → `path_a_structured` 块,作为构造配方真源。
 * yaml 缺失 / 块缺失 / 解析失败 → 用内置默认(行为与重构前一致,非破坏)。缓存一次。
 */
function loadStructuredSpec(): StructuredSpec {
  if (_structuredSpecCache) return _structuredSpecCache;
  const candidates = [
    path.join(process.cwd(), '..', '.xueban', 'skills', 'agent-team-assembly', 'assembly_workflow.yaml'),
    path.join(process.cwd(), '.xueban', 'skills', 'agent-team-assembly', 'assembly_workflow.yaml'),
  ];
  let spec: StructuredSpec = { ...DEFAULT_STRUCTURED_SPEC };
  for (const p of candidates) {
    try {
      if (!fs.existsSync(p)) continue;
      const doc = yaml.load(fs.readFileSync(p, 'utf-8')) as { path_a_structured?: Partial<StructuredSpec> } | null;
      const block = doc?.path_a_structured;
      if (block && typeof block === 'object') {
        spec = {
          members_source: typeof block.members_source === 'string' ? block.members_source : DEFAULT_STRUCTURED_SPEC.members_source,
          edge_strategy: typeof block.edge_strategy === 'string' ? block.edge_strategy : DEFAULT_STRUCTURED_SPEC.edge_strategy,
          raci_rules: typeof block.raci_rules === 'string' ? block.raci_rules : DEFAULT_STRUCTURED_SPEC.raci_rules,
          persona: typeof block.persona === 'string' ? block.persona : DEFAULT_STRUCTURED_SPEC.persona,
        };
      }
      break;
    } catch {
      /* best-effort → defaults */
    }
  }
  _structuredSpecCache = spec;
  return spec;
}

/** 测试用:重置 spec 缓存(改 yaml 后重读)。 */
export function _resetStructuredSpecForTests(): void {
  _structuredSpecCache = null;
}

// 待决2 — SPEC 指纹纳入结构化编译的 compiler_version,让"改 SPEC"使旧的 structured
// 缓存失效(SPEC yaml 不在目标 skill content_hash 里,否则改了 SPEC 旧缓存仍命中)。
function structuredSpecFingerprint(): string {
  return crypto.createHash('sha256').update(JSON.stringify(loadStructuredSpec())).digest('hex').slice(0, 8);
}
function structuredCompilerVersion(): string {
  return `v1+spec:${structuredSpecFingerprint()}`;
}

// 待决3 — persona 模式真接:verbatim(逐字,上限截断)| summary(取首段、短)。
function applyPersonaMode(raw: string, mode: string): string {
  const trimmed = raw.trim();
  if (mode === 'summary') {
    const firstPara = trimmed.split(/\n\s*\n/).find((p) => p.trim().length > 0)?.trim() ?? trimmed;
    return firstPara.length > 400 ? firstPara.slice(0, 400) + '\n[…]' : firstPara;
  }
  // verbatim(默认)
  return trimmed.length > STRUCTURED_PERSONA_MAX ? trimmed.slice(0, STRUCTURED_PERSONA_MAX) + '\n[…truncated]' : trimmed;
}

// 待决3 — raci_rules 真接:引擎读取并校验(目前仅 role-table)。RACI 的实际派生在
// 前端 deriveRaci 实现该规则;此处校验让该字段"活"起来(非法值告警+回落)。
const SUPPORTED_RACI_RULES = new Set(['role-table']);

/**
 * 确定性结构化编译 — 直接从 skill **声明的** agent 建队,不调 LLM。规则由组装 skill 的
 * yaml `path_a_structured` 块(SPEC)驱动,**不再硬编**:成员来源 / DAG 排法均读 yaml。
 *
 * 返回 null 当 skill 不是可识别的多 agent 结构(真 agent 文件 ≤1)→ 调用方落到 LLM 路。
 * `derivedFrom:'structured'` = 一等结果(UI 显示「已编译」而非「降级」)。全程零 token、可复现。
 */
function tryStructuredCompile(skill: SkillReadOutput): CompiledSkill | null {
  const spec = loadStructuredSpec();
  // members_source:目前只支持 agent_files;未知来源 → 不接管,交给 LLM 路。
  if (spec.members_source !== 'agent_files') return null;
  // raci_rules 校验(待决3):非支持值告警,RACI 仍按 role-table(前端 deriveRaci)派生。
  if (!SUPPORTED_RACI_RULES.has(spec.raci_rules)) {
    console.warn(
      `[skill-compiler] path_a_structured.raci_rules='${spec.raci_rules}' 未支持,回落 role-table(RACI 由前端 deriveRaci 派生)`,
    );
  }

  const files: SkillFileEntry[] = skill.agent_files ?? [];
  if (files.length < 2) return null;

  // 成员排序由 edge_strategy 决定:
  //   phase-dir-order — 按阶段目录前缀(1-→2-→3-)排,同阶段保持原序;
  //   sequential      — 保持磁盘发现顺序。
  // 排好后一律串成顺序链(a→b→c)。
  const indexed = files.map((f, i) => ({ f, i }));
  const ordered =
    spec.edge_strategy === 'sequential'
      ? indexed
      : indexed
          .map((x) => ({ ...x, phase: phaseRankFromPath(x.f.path) }))
          .sort((a, b) => a.phase - b.phase || a.i - b.i);

  const members_ids: string[] = [];
  const members_personas: Record<string, string> = {};
  for (const { f } of ordered) {
    const id = memberIdFromFile(f);
    if (!id || members_ids.includes(id)) continue;
    members_ids.push(id);
    members_personas[id] = applyPersonaMode(f.raw, spec.persona);  // 待决3:按 persona 模式取
  }
  if (members_ids.length < 2) return null;

  const edges_v1 = members_ids.slice(1).map((to, i) => ({
    from: members_ids[i],
    to,
    kind: 'sequential' as const,
  }));

  const teamConfig: CompiledTeamConfig = {
    team_id: skill.skill_id,
    version: 1,
    name: skill.skill_id,
    description: `Structured team — ${members_ids.length} declared agent(s), edge_strategy=${spec.edge_strategy}, persona=${spec.persona}.`,
    members_ids,
    members_personas,
    edges_v1,
    policy_obj: { retry: 3, timeout_per_step_ms: 60_000 },
    derivedFrom: 'structured',
  };
  return {
    skill_id: skill.skill_id,
    source_content_hash: skill.content_hash,
    compiled_at: new Date().toISOString(),
    compiler_version: structuredCompilerVersion(),  // 待决2:含 SPEC 指纹 → 改 SPEC 旧缓存失效
    mode: 'team',
    teamConfig,
    llm_call_meta: { model: 'structured', tokens_in: 0, tokens_out: 0, duration_ms: 0 },
  };
}

/**
 * Full compile pipeline. Cache hit → instant return; cache miss → LLM call
 * (with fallback on any failure). Always returns a runnable CompiledSkill.
 *
 * Caller responsibility: ensure `skill` came from a fresh `readSkill()`
 * walk (cached or not) — if you hand-craft a SkillReadOutput in tests, do
 * compute a real content_hash via `computeContentHash([…])` so the cache
 * round-trip works.
 */
export async function compile(skill: SkillReadOutput): Promise<CompiledSkill> {
  // 1) cache lookup —— 待决2:structured 缓存是 SPEC-依赖的。若缓存是 structured 但
  // compiler_version 与当前 SPEC 指纹不符(= SPEC yaml 改过)→ 视为 miss,重编译。
  const cached = await readCompileCache(skill.content_hash);
  if (cached) {
    const staleStructured =
      cached.teamConfig?.derivedFrom === 'structured' &&
      cached.compiler_version !== structuredCompilerVersion();
    if (!staleStructured) {
      return { ...cached, skill_id: skill.skill_id };
    }
    // SPEC 改过 → 落到下面重编译(structured 仅 ~5ms,代价可忽略)。
  }

  // 2026-06-01 — 确定性优先:skill 若声明了多 agent 结构(≥2 个真 agent 文件),
  // 直接结构化建队(读人家声明的 roster + 按阶段目录排 DAG),**跳过 LLM 编译**
  // ——快(秒出,免 ~140s)、忠实(verbatim roster,不让 LLM 猜/漏)、可复现、
  // 不依赖 key。只有"无结构/散文型"skill(<2 真 agent)才落到下面的 LLM 兜底。
  const structured = tryStructuredCompile(skill);
  if (structured) {
    await writeCompileCache(structured).catch(() => {
      /* cache failure non-fatal */
    });
    return structured;
  }

  // 2) provider resolution — fallback when nothing usable
  const resolved = resolveCompileProvider();
  if (!resolved) {
    const out = fallbackCompile(skill, 'no-provider-configured');
    await writeCompileCache(out).catch(() => {
      /* cache failure non-fatal */
    });
    return out;
  }

  // 3) build prompt + call LLM
  const prompt = buildCompilePrompt(skill);
  const t0 = Date.now();
  let text = '';
  try {
    const stream = callProviderImpl(resolved.providerId, {
      systemPrompt: prompt.system,
      userMessage: prompt.user,
      api_key: resolved.apiKey,
      model: resolved.model,
      max_tokens: 4096,
      temperature: 0,
      base_url: resolved.baseUrl,
    });
    for await (const chunk of stream) {
      if (chunk.type === 'text-delta') {
        text += chunk.text;
      } else if (chunk.type === 'error') {
        const out = fallbackCompile(skill, `llm-error:${chunk.code ?? 'UNKNOWN'}`);
        await writeCompileCache(out).catch(() => {});
        return out;
      }
    }
  } catch (err) {
    const out = fallbackCompile(skill, `llm-throw:${(err as Error).message ?? 'unknown'}`);
    await writeCompileCache(out).catch(() => {});
    return out;
  }
  const duration_ms = Date.now() - t0;

  // 4) parse + validate JSON
  const parsed = tryParseCompilerJson(text);
  if (!parsed) {
    const out = fallbackCompile(skill, 'json-parse-failed');
    await writeCompileCache(out).catch(() => {});
    return out;
  }
  const validated = validateAndNormalize(parsed, skill);
  if (!validated) {
    const out = fallbackCompile(skill, 'schema-invalid');
    await writeCompileCache(out).catch(() => {});
    return out;
  }

  const out: CompiledSkill = {
    skill_id: skill.skill_id,
    source_content_hash: skill.content_hash,
    compiled_at: new Date().toISOString(),
    compiler_version: 'v1',
    mode: validated.mode,
    agentConfig: validated.agentConfig,
    teamConfig: validated.teamConfig,
    llm_call_meta: {
      model: `${resolved.providerId}:${resolved.model}`,
      // Token counts unknown for callProvider path (it drops usage events on
      // the single-turn back-compat shim). Approximate via char/4.
      tokens_in: Math.ceil(prompt.estimated_chars / 4),
      tokens_out: Math.ceil(text.length / 4),
      duration_ms,
    },
  };

  await writeCompileCache(out).catch(() => {});
  return out;
}

// ─── parse / validate ────────────────────────────────────────────────────────

interface ParsedCompilerJson {
  mode?: unknown;
  agentConfig?: unknown;
  teamConfig?: unknown;
}

/**
 * Extract the JSON object the LLM emitted. Tolerates:
 *   - leading / trailing whitespace
 *   - ```json fence wrappers (the system prompt forbids fences but LLMs
 *     don't always listen)
 *   - prose preamble before the `{` (very rare; we take the longest
 *     balanced `{…}` substring)
 *
 * Returns null on any failure — caller treats that as fallback trigger.
 */
function tryParseCompilerJson(text: string): ParsedCompilerJson | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  // strip ```json … ``` if present
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  const candidate = fenced ? fenced[1] : trimmed;
  // try direct parse first
  try {
    const parsed = JSON.parse(candidate);
    if (parsed && typeof parsed === 'object') return parsed as ParsedCompilerJson;
  } catch {
    /* fall through to substring extraction */
  }
  // longest balanced { … } substring
  const start = candidate.indexOf('{');
  const end = candidate.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) return null;
  try {
    const parsed = JSON.parse(candidate.slice(start, end + 1));
    if (parsed && typeof parsed === 'object') return parsed as ParsedCompilerJson;
  } catch {
    return null;
  }
  return null;
}

interface ValidatedConfig {
  mode: 'agent' | 'team';
  agentConfig?: CompiledAgentConfig;
  teamConfig?: CompiledTeamConfig;
}

const MEMBER_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

/**
 * Normalize + validate the LLM output. Returns null on any structural
 * problem so caller fires fallbackCompile.
 *
 * Mutations performed:
 *   - clamp max_iterations into [1, 200]
 *   - default policy_obj.retry → 3, timeout_per_step_ms → 60000
 *   - synthesize members_personas entry from member id when LLM omitted it
 *   - mark teamConfig.derivedFrom = 'prose-llm'
 */
function validateAndNormalize(
  raw: ParsedCompilerJson,
  skill: SkillReadOutput,
): ValidatedConfig | null {
  if (raw.mode !== 'agent' && raw.mode !== 'team') return null;

  if (raw.mode === 'agent') {
    const ac = raw.agentConfig;
    if (!ac || typeof ac !== 'object') return null;
    const a = ac as Record<string, unknown>;
    if (typeof a.persona !== 'string' || typeof a.system_prompt !== 'string') {
      return null;
    }
    const tools = Array.isArray(a.tools)
      ? a.tools.filter((t): t is string => typeof t === 'string')
      : [];
    const max_iter = typeof a.max_iterations === 'number' ? a.max_iterations : 50;
    return {
      mode: 'agent',
      agentConfig: {
        persona: a.persona,
        system_prompt: a.system_prompt,
        tools,
        model_hint: typeof a.model_hint === 'string' ? a.model_hint : undefined,
        max_iterations: Math.max(1, Math.min(200, Math.floor(max_iter))),
      },
    };
  }

  // team
  const tc = raw.teamConfig;
  if (!tc || typeof tc !== 'object') return null;
  const t = tc as Record<string, unknown>;
  if (typeof t.name !== 'string') return null;
  if (!Array.isArray(t.members_ids) || t.members_ids.length === 0) return null;
  const members_ids = (t.members_ids as unknown[]).filter(
    (m): m is string => typeof m === 'string' && MEMBER_ID_RE.test(m),
  );
  if (members_ids.length === 0) return null;

  const personasRaw =
    t.members_personas && typeof t.members_personas === 'object'
      ? (t.members_personas as Record<string, unknown>)
      : {};
  const members_personas: Record<string, string> = {};
  for (const id of members_ids) {
    const p = personasRaw[id];
    members_personas[id] =
      typeof p === 'string' && p.trim().length > 0
        ? p
        : `Agent "${id}" — execute your role per the skill description.`;
  }

  const edgesRaw = Array.isArray(t.edges_v1) ? (t.edges_v1 as unknown[]) : [];
  const memberSet = new Set(members_ids);
  const edges_v1 = edgesRaw.flatMap((e) => {
    if (!e || typeof e !== 'object') return [];
    const eo = e as Record<string, unknown>;
    if (typeof eo.from !== 'string' || typeof eo.to !== 'string') return [];
    if (!memberSet.has(eo.from) || !memberSet.has(eo.to)) return [];
    const kindRaw = typeof eo.kind === 'string' ? eo.kind : 'sequential';
    const kind: 'sequential' | 'parallel' | 'conditional' =
      kindRaw === 'parallel' || kindRaw === 'conditional' ? kindRaw : 'sequential';
    return [
      {
        from: eo.from,
        to: eo.to,
        kind,
        condition: typeof eo.condition === 'string' ? eo.condition : undefined,
        max_retries: typeof eo.max_retries === 'number' ? eo.max_retries : undefined,
      },
    ];
  });

  const policyRaw =
    t.policy_obj && typeof t.policy_obj === 'object'
      ? (t.policy_obj as Record<string, unknown>)
      : {};
  const policy_obj = {
    retry: typeof policyRaw.retry === 'number' ? policyRaw.retry : 3,
    escalation:
      typeof policyRaw.escalation === 'string' ? policyRaw.escalation : undefined,
    timeout_per_step_ms:
      typeof policyRaw.timeout_per_step_ms === 'number'
        ? policyRaw.timeout_per_step_ms
        : 60_000,
  };

  return {
    mode: 'team',
    teamConfig: {
      team_id: skill.skill_id,
      version: 1,
      name: t.name,
      description: typeof t.description === 'string' ? t.description : undefined,
      members_ids,
      members_personas,
      edges_v1,
      policy_obj,
      derivedFrom: 'prose-llm',
    },
  };
}
