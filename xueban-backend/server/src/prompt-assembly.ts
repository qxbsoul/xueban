/**
 * prompt-assembly.ts — Story 15.13 / 15.29 / S8 — Multi-layer system prompt composer.
 *
 * Implements the OpenDesign-aligned 8-layer "onion" with a cache boundary
 * marker (S8 / skill-team-conversion-design-v1.md §3.1.b + §5 S8) for
 * Anthropic's 5-minute prompt cache:
 *
 *   ┌─── STATIC SEGMENT (cache_control: ephemeral; cross-turn stable) ───┐
 *   1. DISCOVERY directives    (force intent restatement + plan)
 *   2. Identity charter        (anti-AI-slop hard rules)
 *   3. DS injection_prompt     (from design-systems registry / 15.11)
 *   4. Skill system_prompt     (from skills registry / 15.10)
 *   5. Framework directive     (mode-keyed, e.g. deck → PPTX rules)
 *   └────────────────────────────────────────────────────────────────────┘
 *
 *       __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__   ← S8 marker (literal string)
 *
 *   ┌─── DYNAMIC SEGMENT (NOT cached; changes every turn) ───────────────┐
 *   6. Project meta block      (kind / fidelity / animations / ...)
 *   7. Conversation history    (multi-turn chat history; 15.29)
 *   8. Side files              (reference snippets / user goal artifacts)
 *   └────────────────────────────────────────────────────────────────────┘
 *
 * Layer order is fixed. Each layer is independently togglable via
 * `layer_toggles.<key> = false`. Empty layers (toggled off OR empty content)
 * are dropped completely — no separator, no blank line — so the resulting
 * prompt is dense regardless of which layers are active.
 *
 * Layers are joined by `\n\n---\n\n` (markdown horizontal rule) for visual
 * separation when the prompt is dumped to logs / inspector UI.
 *
 * S8 boundary placement rule (skill-team-conversion-design-v1.md §3.1.b):
 *   - Layers `discovery / identity / ds / skill / framework` go STATIC
 *     (cross-turn stable; skill_mode is bound at session start).
 *   - Layers `project_meta / conversation_history / side_files` go DYNAMIC
 *     (user goal + history + per-turn artifacts).
 *   - The boundary marker is emitted between the last non-empty static layer
 *     and the first non-empty dynamic layer. If either side is empty after
 *     the empty-drop pass, the marker is still emitted so downstream
 *     (anthropic-api-client.ts) can split the system field; consumers ignore
 *     a trailing/leading marker gracefully.
 */

import { DISCOVERY_CHARTER } from './discovery-charter';
import { IDENTITY_CHARTER } from './identity-charter';
import { getFrameworkDirective } from './framework-directives';

export interface LayerToggles {
  discovery?: boolean;
  identity?: boolean;
  ds?: boolean;
  skill?: boolean;
  framework?: boolean;
  project?: boolean;
  /** Story 15.29 — conversation history layer; default enabled. */
  conversation_history?: boolean;
  sides?: boolean;
}

export interface ComposeInput {
  /** From design-systems registry (15.11). May be empty. */
  ds_injection?: string;
  /** From skills registry (15.10). The skill body. */
  skill_system_prompt?: string;
  /** Skill mode — drives framework directive selection ('deck' → PPTX). */
  skill_mode?: string;
  /** Optional user-supplied project metadata (kind / fidelity / etc.). */
  project_meta?: Record<string, unknown> | null;
  /**
   * Story 15.29 — pre-rendered conversation history block. When present and
   * non-empty, it is injected between project_meta and side_files. Empty /
   * missing → drop. assembler.ts / run-sessions.ts owns the rendering.
   */
  conversation_history?: string;
  /**
   * Side-file reference block. Story 15.12 owns construction; here we just
   * accept a pre-rendered string and inject it as the final dynamic layer.
   * Empty → skip.
   */
  side_files?: string;
  /** Per-layer overrides; missing keys default to enabled. */
  layer_toggles?: LayerToggles;
}

export interface ComposeResult {
  prompt: string;
  layers_included: string[];
  layers_skipped: string[];
  total_chars: number;
  /** Concrete framework name when a directive was injected, else null. */
  framework: string | null;
  /**
   * S8 — char count of the static segment (everything before the
   * boundary marker, excluding the marker itself). Useful for
   * cache-hit telemetry. 0 when no static layers rendered.
   */
  layers_static_chars: number;
  /**
   * S8 — char count of the dynamic segment (everything after the
   * boundary marker, excluding the marker itself). 0 when no dynamic
   * layers rendered.
   */
  layers_dynamic_chars: number;
}

type LayerKey =
  | 'discovery'
  | 'identity'
  | 'ds'
  | 'skill'
  | 'framework'
  | 'project'
  | 'conversation_history'
  | 'sides';

const LAYER_ORDER: readonly LayerKey[] = [
  // ── Static segment (cacheable; precedes boundary marker) ────────────
  'discovery',
  'identity',
  'ds',
  'skill',
  'framework',
  // ── Dynamic segment (changes per turn; follows boundary marker) ─────
  'project',
  'conversation_history',
  'sides',
] as const;

/**
 * S8 — partition LAYER_ORDER into static vs dynamic. Used by composeSystemPrompt
 * to decide where the boundary marker lands.
 *
 * IMPORTANT: this set must stay in sync with the §3.1.b "SystemPromptBuilder
 * 分层" contract in skill-team-conversion-design-v1.md. Adding a new layer
 * means classifying it explicitly here; failure to do so is a design-doc
 * violation, not a refactor.
 */
const STATIC_LAYER_KEYS: ReadonlySet<LayerKey> = new Set<LayerKey>([
  'discovery',
  'identity',
  'ds',
  'skill',
  'framework',
]);

const LAYER_SEPARATOR = '\n\n---\n\n';

/**
 * S8 — the literal marker token. Mirrors `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` in
 * the Rust reference (rust/crates/runtime/src/prompt.rs). Exported so
 * AnthropicApiClient can split the system prompt on this exact string when
 * constructing the SDK `system` field with cache_control.
 *
 * Do NOT change the literal — Anthropic SDK consumers depend on it being
 * recognizable. If it ever needs to evolve, add a v2 marker and detect both.
 */
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY = '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__';

/**
 * Render the project_meta object as a bullet list. Strings / numbers /
 * booleans are stringified directly; complex values are JSON.stringify'd so
 * nothing leaks raw [object Object] into the prompt. Object key insertion
 * order is preserved (no sorting / re-keying).
 */
function renderProjectMeta(meta: Record<string, unknown>): string {
  const lines = Object.entries(meta).map(([k, v]) => {
    const val =
      typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'
        ? String(v)
        : JSON.stringify(v);
    return `- ${k}: ${val}`;
  });
  return '## PROJECT META\n\n' + lines.join('\n');
}

/**
 * Compose the system prompt by stacking 8 ordered layers (5 static + 3
 * dynamic). Empty / disabled layers are dropped. A boundary marker is
 * injected between the static and dynamic segments so downstream
 * (anthropic-api-client.ts) can apply Anthropic prompt-cache cache_control
 * to the static half. Returns the final prompt plus observability metadata
 * for the `compose` SSE event (layer names only — never the prompt itself).
 */
export function composeSystemPrompt(input: ComposeInput): ComposeResult {
  const t = input.layer_toggles ?? {};

  // Build each layer as { key, content }. Empty content === drop later.
  const layers: Array<{ key: LayerKey; content: string }> = [];

  // 1. DISCOVERY (static)
  layers.push({
    key: 'discovery',
    content: t.discovery !== false ? DISCOVERY_CHARTER : '',
  });

  // 2. Identity (static)
  layers.push({
    key: 'identity',
    content: t.identity !== false ? IDENTITY_CHARTER : '',
  });

  // 3. DS injection (static)
  layers.push({
    key: 'ds',
    content: t.ds !== false && input.ds_injection ? input.ds_injection : '',
  });

  // 4. Skill system_prompt (static)
  layers.push({
    key: 'skill',
    content:
      t.skill !== false && input.skill_system_prompt ? input.skill_system_prompt : '',
  });

  // 5. Framework directive (static; mode-driven). S8: moved up from the
  // tail position into the static segment — skill_mode is bound at session
  // start so the directive body is stable across turns and benefits from
  // prompt-cache like the rest of the static block.
  let frameworkContent = '';
  let framework: string | null = null;
  if (t.framework !== false) {
    frameworkContent = getFrameworkDirective(input.skill_mode);
    if (frameworkContent && input.skill_mode) {
      framework = input.skill_mode;
    }
  }
  layers.push({ key: 'framework', content: frameworkContent });

  // 6. Project meta (dynamic)
  let projectContent = '';
  if (
    t.project !== false &&
    input.project_meta &&
    typeof input.project_meta === 'object' &&
    Object.keys(input.project_meta).length > 0
  ) {
    projectContent = renderProjectMeta(input.project_meta);
  }
  layers.push({ key: 'project', content: projectContent });

  // 7. Conversation history (dynamic; Story 15.29) — assembler.ts pre-renders
  // the markdown block from `getRecentMessages(conversation_id, 20)`. When
  // the session has no conversation_id or no messages, the input field is
  // empty / undefined and the layer is dropped together with all other
  // empty layers.
  layers.push({
    key: 'conversation_history',
    content:
      t.conversation_history !== false && input.conversation_history
        ? input.conversation_history
        : '',
  });

  // 8. Side files (dynamic; 15.12 interface — empty by default)
  layers.push({
    key: 'sides',
    content: t.sides !== false && input.side_files ? input.side_files : '',
  });

  // Drop empty layers; preserve order. Using trim() so a layer that's pure
  // whitespace doesn't sneak through as "non-empty".
  const included = layers.filter((l) => l.content.trim().length > 0);
  const skipped = layers
    .filter((l) => l.content.trim().length === 0)
    .map((l) => l.key);

  // Partition included into static vs dynamic per STATIC_LAYER_KEYS. The
  // boundary marker sits BETWEEN these two arrays. Empty side is fine —
  // we still emit the marker so downstream split logic is uniform.
  const staticIncluded = included.filter((l) => STATIC_LAYER_KEYS.has(l.key));
  const dynamicIncluded = included.filter((l) => !STATIC_LAYER_KEYS.has(l.key));

  const staticPrompt = staticIncluded.map((l) => l.content).join(LAYER_SEPARATOR);
  const dynamicPrompt = dynamicIncluded.map((l) => l.content).join(LAYER_SEPARATOR);

  // Assemble final prompt. Marker is wrapped by LAYER_SEPARATOR on both
  // sides when both halves are non-empty so the boundary visually matches
  // every other layer break. When one side is empty, we still emit the
  // marker but with only one separator (or none) to avoid leading /
  // trailing whitespace in the prompt body.
  let prompt: string;
  if (staticPrompt && dynamicPrompt) {
    prompt =
      staticPrompt + LAYER_SEPARATOR + SYSTEM_PROMPT_DYNAMIC_BOUNDARY + LAYER_SEPARATOR + dynamicPrompt;
  } else if (staticPrompt) {
    // Static-only: marker at the tail signals "no dynamic segment". The
    // splitter in anthropic-api-client.ts treats an empty after-marker
    // half as no-op (no second cache_control block).
    prompt = staticPrompt + LAYER_SEPARATOR + SYSTEM_PROMPT_DYNAMIC_BOUNDARY;
  } else if (dynamicPrompt) {
    // Dynamic-only: marker at the head. Splitter sees empty before-marker
    // half → no cache_control block, just a single plain system text.
    prompt = SYSTEM_PROMPT_DYNAMIC_BOUNDARY + LAYER_SEPARATOR + dynamicPrompt;
  } else {
    // Both empty (e.g. all toggles off) — emit nothing, NOT the bare
    // marker. A lone marker would be a meaningless system prompt.
    prompt = '';
  }

  // Sanity guard — LAYER_ORDER must mirror the layers we just built. Catches
  // divergence between push order and the canonical list.
  const builtKeys = layers.map((l) => l.key);
  if (
    builtKeys.length !== LAYER_ORDER.length ||
    builtKeys.some((k, i) => k !== LAYER_ORDER[i])
  ) {
    throw new Error(
      `[prompt-assembly] layer order drift: built ${builtKeys.join(',')} vs canonical ${LAYER_ORDER.join(',')}`,
    );
  }

  return {
    prompt,
    layers_included: included.map((l) => l.key),
    layers_skipped: skipped,
    total_chars: prompt.length,
    framework,
    layers_static_chars: staticPrompt.length,
    layers_dynamic_chars: dynamicPrompt.length,
  };
}

/** Exported for tests / inspector UI. */
export { LAYER_ORDER, LAYER_SEPARATOR, STATIC_LAYER_KEYS };
