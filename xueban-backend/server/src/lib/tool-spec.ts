/**
 * tool-spec.ts — ToolSpec + ToolRegistry for the multi-turn ConversationRuntime.
 *
 * S2 (skill-team-conversion-design-v1.md §5) — TypeScript port of
 * `claw-code-reference rust/crates/tools/src/lib.rs`. Holds the *catalog* of
 * tool definitions (name + description + JSON Schema) that we hand to the LLM
 * via the Anthropic Messages API `tools` field on every turn.
 *
 * Design notes:
 *   - `source` distinguishes 'base' (always-on, registered at boot) from
 *     'conditional' (per-skill / per-team tools that get layered on after a
 *     skill activates — e.g. SkillAnchorTool family in S4).
 *   - Order is preserved: tools registered first appear first in toAnthropicTools().
 *     This matters because LLMs sometimes anchor on tool-list position.
 *   - Re-registering the same name overwrites in place (no duplicates). Last
 *     write wins, position unchanged. Lets a conditional tool with the same
 *     name shadow a base one without leaking both into the manifest.
 *   - Pure data structure: no I/O, no SSE, no execution. ToolExecutor (S4/S5)
 *     and PermissionPolicy (S3) consume specs from this registry.
 */

/**
 * Where a tool came from. Influences when we offer it to the LLM:
 *   - `base`        — always present in the prompt's tool list.
 *   - `conditional` — only present after a triggering condition (skill
 *                     activation, team mode, etc).
 */
export type ToolSource = 'base' | 'conditional';

/**
 * One tool definition. `input_schema` is JSON Schema — must round-trip cleanly
 * into the Anthropic Messages API `tools[].input_schema` field. Keep it a
 * plain `object` (not a typed schema lib): the LLM cares only about the JSON.
 */
export interface ToolSpec {
  name: string;
  description: string;
  input_schema: object;
  source: ToolSource;
}

/**
 * Shape Anthropic Messages API expects for each entry in the request `tools`
 * array. Subset of ToolSpec — `source` is internal-only.
 */
export interface AnthropicToolEntry {
  name: string;
  description: string;
  input_schema: object;
}

/**
 * Ordered registry of ToolSpec. Backed by a Map keyed by name so register()
 * is O(1) and replaces in place. We track insertion order via a parallel
 * array so iteration is deterministic (matching the Rust Vec semantics).
 */
export class ToolRegistry {
  private specs = new Map<string, ToolSpec>();
  private order: string[] = [];

  constructor(initial: ToolSpec[] = []) {
    for (const s of initial) this.register(s);
  }

  /**
   * Add or overwrite a spec. Re-registering the same name keeps its original
   * position in the iteration order — useful for "conditional shadows base"
   * without reshuffling the LLM-visible tool list.
   *
   * **Last-write-wins semantics**: when an existing name is re-registered,
   * every field of the new spec replaces the old one in place — including
   * `description`, `input_schema`, AND `source`. So a conditional spec
   * registered after a base spec with the same name will end up with
   * `source: 'conditional'` (and the old base description is lost). This is
   * intentional: it lets a per-skill tool fully shadow a base tool without
   * leaking dual entries into `toAnthropicTools()`. Callers that need to
   * preserve the original should query `get(name)` before re-registering.
   */
  register(spec: ToolSpec): void {
    if (!this.specs.has(spec.name)) this.order.push(spec.name);
    this.specs.set(spec.name, spec);
  }

  /** All specs in insertion order. Returns a fresh array; safe to mutate. */
  list(): ToolSpec[] {
    return this.order
      .map((n) => this.specs.get(n))
      .filter((s): s is ToolSpec => s !== undefined);
  }

  get(name: string): ToolSpec | undefined {
    return this.specs.get(name);
  }

  has(name: string): boolean {
    return this.specs.has(name);
  }

  /**
   * Anthropic Messages API shape. Drops `source` (internal). Returns the same
   * insertion order as list() so prompt position is stable across turns.
   */
  toAnthropicTools(): AnthropicToolEntry[] {
    return this.list().map(({ name, description, input_schema }) => ({
      name,
      description,
      input_schema,
    }));
  }
}
