/**
 * skill-runners/types.ts — Shared types for the executor dispatcher (Story 15.19 v2)
 *
 * Story 15.23 (ACP) and 15.18 (LLM providers abstraction) will reuse these
 * exact shapes — that's the point of this Story: build the dispatcher seam
 * once so future executors plug in without touching `assembler.ts`.
 *
 * `RunnerChunk` is intentionally identical to `parser.ts` `SseEvent` — we
 * pass the same SSE shape through to `routes/run-sessions.ts` so the front-end
 * sees no difference between executors.
 */

import type { SseEvent } from '../../parser';

/**
 * Inputs every runner accepts. The runner decides what to do with each field;
 * `cli:*` runners pass `prompt` to stdin, `anthropic-direct` passes it as
 * `messages[0].content`.
 */
export interface RunnerInput {
  /** Final composed system prompt (15.13 layer-assembly output). */
  system_prompt: string;
  /** User goal (becomes the user-turn message / spawn stdin payload). */
  prompt: string;
  /** Session id used by parser callbacks for artifact paths. */
  session_id: string;
  /** Where artifacts should be written (per-session dir). */
  cwd: string;
  /** Extra env vars merged on top of process.env when spawning. */
  env?: Record<string, string>;
  /** AbortSignal — runner is responsible for cleaning up children on abort. */
  signal?: AbortSignal;
  /** BYOK key forwarded by route handler. Used by `anthropic-direct`. */
  anthropic_key?: string;
  /**
   * Story 15.18 — provider id selected by the front-end / route handler.
   * The `anthropic-direct` runner uses this to dispatch into the right
   * llm-providers entry (anthropic / openai / deepseek / zhipu).
   * Falls back to 'anthropic' for back-compat.
   */
  provider?: string;
  /**
   * Story 15.18 — generic BYOK key forwarded by route handler. Specific to
   * the `provider` chosen above; supersedes `anthropic_key` when set. Route
   * handler picks the right header (`X-${Provider}-Key`) into this field.
   */
  api_key?: string;
  /** Generation knobs (Story 15.9). Runners that ignore these are free to. */
  model?: string;
  max_tokens?: number;
  temperature?: number;
}

/** What runners yield. Identical to `parser.ts SseEvent` for forward compat. */
export type RunnerChunk = SseEvent;

/** Skill-side fields the dispatcher cares about. */
export interface SkillForDispatch {
  name: string;
  /**
   * Story 15.19 v2 — declarative executor selector. Examples:
   *   undefined           → 'anthropic-direct' (default, back-compat)
   *   'anthropic-direct'  → existing Claude SDK path
   *   'cli:claude'        → spawn `claude` CLI
   *   'cli:auto'          → first detected & env-ready CLI; fallback to anthropic-direct
   *   'acp:<id>'          → reserved for Story 15.23
   *   'mcp:<id>'          → reserved for Story 15.23
   */
  executor?: string;
}
