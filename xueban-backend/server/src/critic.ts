/**
 * critic.ts — Story 15.14 — Xueban auto-critique pass.
 *
 * After artifact generation, runCritique:
 *   1. Reads the artifact from .xueban/projects/<session_id>/<filename>
 *   2. Runs lint to get hard-evidence findings + sf:step _meta
 *   3. Composes a 5-dimension critique prompt (goal_achievement / skill_completeness /
 *      structural_integrity / reference_grounding / anti_pattern_free)
 *   4. Calls Anthropic SDK (single non-streaming round; critique is short)
 *   5. Parses <sf:critique>{...}</sf:critique> JSON-in-XML
 *   6. Returns CritiqueOutput; failure modes return scores=null + error fields
 *
 * Failure modes (all caught at runCritique boundary, never thrown to caller):
 *   - No API key  → CritiqueOutput.scores = null, error_code='CRITIQUE_NO_API_KEY'
 *   - HTTP 401/403/429/5xx → scores=null, error_code='CRITIQUE_API_ERROR'
 *   - Parse failure → scores=null, error_code='CRITIQUE_PARSE_FAILED'
 *
 * Network call uses `fetch` (Node 20+ native) so we can mock it in tests via
 * dependency injection (callApi parameter) without a global stub.
 */

import fs from 'fs';
import { runLint, projectArtifactPath, type LintResult } from './lint';

export const CRITIQUE_DIMENSIONS = [
  'goal_achievement',
  'skill_completeness',
  'structural_integrity',
  'reference_grounding',
  'anti_pattern_free',
] as const;

export type CritiqueDim = typeof CRITIQUE_DIMENSIONS[number];

export interface DimensionScore {
  score: number;
  rationale: string;
  improvement?: string;
}

export interface CritiqueScores {
  goal_achievement: DimensionScore | null;
  skill_completeness: DimensionScore | null;
  structural_integrity: DimensionScore | null;
  reference_grounding: DimensionScore | null;
  anti_pattern_free: DimensionScore | null;
  /** Reserved 6th dimension; this Story keeps it null. */
  policy_compliance: DimensionScore | null;
}

export interface CritiqueOutput {
  scores: CritiqueScores | null;
  overall_summary: string;
  lint_summary: LintResult['summary'];
  duration_ms: number;
  /** Present only on failure. */
  error_code?: 'CRITIQUE_NO_API_KEY' | 'CRITIQUE_API_ERROR' | 'CRITIQUE_PARSE_FAILED' | 'CRITIQUE_FAILED';
  error_message?: string;
}

export interface CritiqueInput {
  session_id: string;
  filename: string;
  /** User goal (first user message text); injected into prompt for goal_achievement. */
  user_goal: string;
  /** SKILL.md declared steps (e.g. ['discover', 'draft', 'review']). */
  expected_steps: string[];
  /** BYOK / env override; falls back to process.env.ANTHROPIC_API_KEY. */
  anthropic_key?: string;
  /** Critique model — defaults to a fast Haiku-class for cost. */
  model?: string;
  /** Override fetch for tests (mock). Defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Override lint for tests (mock). Defaults to runLint. */
  lintImpl?: (sessionId: string, filename: string) => LintResult;
  /** Override artifact reader for tests. Defaults to fs.readFileSync. */
  readArtifact?: (sessionId: string, filename: string) => string;
}

export interface CritiqueProgressEmitter {
  (stage: 'lint' | 'prompting' | 'streaming' | 'parsing' | 'done', message?: string): void;
}

/** Compose the critique user-prompt. Exported for unit tests. */
export function composeCritiquePrompt(
  filename: string,
  content: string,
  lint: LintResult,
  user_goal: string,
  expected_steps: string[],
): string {
  const findingsText = lint.findings.length
    ? lint.findings
        .map(f => `- [${f.severity}] ${f.rule}${f.line ? ` (line ${f.line})` : ''}: ${f.message}`)
        .join('\n')
    : '(none)';
  const seen = lint._meta?.sf_steps_seen ?? [];
  const completed = lint._meta?.sf_steps_completed ?? [];
  const stepsText = seen.length
    ? seen.map(s => `- ${s}${completed.includes(s) ? ' (done)' : ' (running, no done!)'}`).join('\n')
    : '(none)';
  const expectedText = expected_steps.length
    ? expected_steps.map(s => `- ${s}`).join('\n')
    : '(none declared)';

  // Truncate artifact content to first ~4000 chars to keep critique cheap and
  // avoid exceeding the model's input budget for big artifacts.
  const preview = content.length > 4000 ? content.slice(0, 4000) + '\n... (truncated)' : content;

  return [
    '你是 Xueban 产物质检员。按 5 维评估（满分 10 分整数）：',
    '1. goal_achievement — 用户目标是否真的解决',
    '2. skill_completeness — Skill 声明的 step 是否全部 running→done',
    '3. structural_integrity — 产物结构是否合法（YAML/MD/HTML 解析+lint 硬证据）',
    '4. reference_grounding — 是否真的用上了侧边文件 / project meta，而非凭空捏造',
    '5. anti_pattern_free — 是否避免 identity charter 禁忌（套话、半成品 placeholder、套用空话开场）',
    '',
    '基于下方的 lint findings + skill manifest 作为硬证据；不要凭空捏造问题。',
    '',
    `### User goal\n${user_goal || '(no explicit goal recorded)'}`,
    '',
    `### Skill manifest (expected steps)\n${expectedText}`,
    '',
    `### Steps actually seen in artifact\n${stepsText}`,
    '',
    `### File (${filename}, ${content.length} chars)\n\`\`\`\n${preview}\n\`\`\``,
    '',
    `### Lint findings\n${findingsText}`,
    '',
    '### Output format',
    '把 JSON 包在 <sf:critique>...</sf:critique> 标签里，每维 50-150 字 rationale + 一行 improvement：',
    '',
    '<sf:critique>',
    '{',
    '  "goal_achievement": {"score": 8, "rationale": "...", "improvement": "..."},',
    '  "skill_completeness": {"score": 6, "rationale": "...", "improvement": "..."},',
    '  "structural_integrity": {"score": 7, "rationale": "...", "improvement": "..."},',
    '  "reference_grounding": {"score": 8, "rationale": "...", "improvement": "..."},',
    '  "anti_pattern_free": {"score": 9, "rationale": "...", "improvement": "..."},',
    '  "overall_summary": "1-2 句概览"',
    '}',
    '</sf:critique>',
  ].join('\n');
}

/** Parse Anthropic raw text for <sf:critique>...</sf:critique> JSON. Exported for tests. */
export function parseCritique(raw: string, lintSummary: LintResult['summary']): CritiqueOutput {
  const m = raw.match(/<sf:critique>([\s\S]+?)<\/sf:critique>/);
  if (!m) {
    return {
      scores: null,
      overall_summary: `[critique parse failed: no <sf:critique> tag in raw (${raw.slice(0, 500)})]`,
      lint_summary: lintSummary,
      duration_ms: 0,
      error_code: 'CRITIQUE_PARSE_FAILED',
      error_message: 'no <sf:critique> tag',
    };
  }
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(m[1].trim()) as Record<string, unknown>;
  } catch (err) {
    return {
      scores: null,
      overall_summary: `[critique parse failed: ${err instanceof Error ? err.message : String(err)}]`,
      lint_summary: lintSummary,
      duration_ms: 0,
      error_code: 'CRITIQUE_PARSE_FAILED',
      error_message: err instanceof Error ? err.message : String(err),
    };
  }

  const scores: CritiqueScores = {
    goal_achievement: null,
    skill_completeness: null,
    structural_integrity: null,
    reference_grounding: null,
    anti_pattern_free: null,
    policy_compliance: null,
  };
  for (const dim of CRITIQUE_DIMENSIONS) {
    const v = parsed[dim];
    if (v && typeof v === 'object' && typeof (v as { score?: unknown }).score === 'number') {
      const s = v as { score: number; rationale?: unknown; improvement?: unknown };
      const n = Math.max(1, Math.min(10, Math.round(s.score)));
      scores[dim] = {
        score: n,
        rationale: typeof s.rationale === 'string' ? s.rationale : '',
        improvement: typeof s.improvement === 'string' ? s.improvement : undefined,
      };
    }
  }

  return {
    scores,
    overall_summary:
      typeof parsed.overall_summary === 'string' ? (parsed.overall_summary as string) : '',
    lint_summary: lintSummary,
    duration_ms: 0,
  };
}

/**
 * Default API caller — uses the Anthropic Messages API directly via fetch
 * (avoids depending on the SDK class instance which we already use elsewhere
 * but inflates the test surface). Returns the assistant text or throws.
 */
async function defaultCallAnthropic(
  apiKey: string,
  systemPrompt: string,
  userPrompt: string,
  model: string,
  fetchImpl: typeof fetch,
): Promise<string> {
  // 2026-05-11 review F2: 30s timeout 防 fetch 卡住 → SSE 整流挂死分钟级。
  // OpenDesign 模式：所有 LLM 出口都 wrap AbortController。
  const ctrl = new AbortController();
  const timeoutId = setTimeout(() => ctrl.abort(), 30_000);
  let res: Response;
  try {
    res = await fetchImpl('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model,
        max_tokens: 1024,
        system: systemPrompt,
        messages: [{ role: 'user', content: userPrompt }],
      }),
      signal: ctrl.signal,
    });
  } catch (err) {
    if ((err as Error).name === 'AbortError') {
      throw Object.assign(new Error('Anthropic API timeout (30s)'), {
        code: 'CRITIQUE_API_ERROR',
        status: 504,
      });
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw Object.assign(new Error(`Anthropic API ${res.status}: ${text.slice(0, 200)}`), {
      code: 'CRITIQUE_API_ERROR',
      status: res.status,
    });
  }
  const j = (await res.json()) as { content?: Array<{ type?: string; text?: string }> };
  const text = (j.content ?? [])
    .filter(b => b.type === 'text' && typeof b.text === 'string')
    .map(b => b.text as string)
    .join('');
  return text;
}

/**
 * Run the critique pass. Always resolves — never throws — to keep the caller's
 * SSE loop simple. On error, scores=null and error_* fields are populated.
 */
export async function runCritique(
  input: CritiqueInput,
  emit?: CritiqueProgressEmitter,
): Promise<CritiqueOutput> {
  const startedAt = Date.now();
  const fetchImpl = input.fetchImpl ?? fetch;
  const lintImpl = input.lintImpl ?? runLint;
  const readArtifact =
    input.readArtifact ??
    ((sid: string, fn: string) => fs.readFileSync(projectArtifactPath(sid, fn), 'utf-8'));

  let lint: LintResult;
  let content: string;
  try {
    emit?.('lint');
    lint = lintImpl(input.session_id, input.filename);
    content = readArtifact(input.session_id, input.filename);
  } catch (err) {
    return {
      scores: null,
      overall_summary: `[critique failed: ${err instanceof Error ? err.message : String(err)}]`,
      lint_summary: { errors: 0, warnings: 0, infos: 0 },
      duration_ms: Date.now() - startedAt,
      error_code: 'CRITIQUE_FAILED',
      error_message: err instanceof Error ? err.message : String(err),
    };
  }

  emit?.('prompting');
  const systemPrompt =
    'You are a strict Xueban quality reviewer. Always wrap final output ' +
    'in <sf:critique>...</sf:critique> with valid JSON. Avoid filler praise; ' +
    'cite the lint findings + skill manifest as concrete evidence.';
  const userPrompt = composeCritiquePrompt(
    input.filename,
    content,
    lint,
    input.user_goal,
    input.expected_steps,
  );

  const apiKey = input.anthropic_key ?? process.env.ANTHROPIC_API_KEY ?? '';
  if (!apiKey) {
    return {
      scores: null,
      overall_summary: '[critique skipped: no Anthropic API key]',
      lint_summary: lint.summary,
      duration_ms: Date.now() - startedAt,
      error_code: 'CRITIQUE_NO_API_KEY',
      error_message: 'no Anthropic API key configured',
    };
  }

  emit?.('streaming');
  let raw: string;
  try {
    raw = await defaultCallAnthropic(
      apiKey,
      systemPrompt,
      userPrompt,
      input.model ?? 'claude-haiku-4-5-20251001',
      fetchImpl,
    );
  } catch (err) {
    const code = (err as { code?: string }).code ?? 'CRITIQUE_API_ERROR';
    return {
      scores: null,
      overall_summary: `[critique API call failed: ${err instanceof Error ? err.message : String(err)}]`,
      lint_summary: lint.summary,
      duration_ms: Date.now() - startedAt,
      error_code: (code === 'CRITIQUE_API_ERROR' ? 'CRITIQUE_API_ERROR' : 'CRITIQUE_FAILED') as CritiqueOutput['error_code'],
      error_message: err instanceof Error ? err.message : String(err),
    };
  }

  emit?.('parsing');
  const parsedOut = parseCritique(raw, lint.summary);
  parsedOut.duration_ms = Date.now() - startedAt;
  emit?.('done');
  return parsedOut;
}
