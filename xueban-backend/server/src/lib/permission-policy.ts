/**
 * permission-policy.ts — per-tool allow/deny/prompt gate for ConversationRuntime.
 *
 * S3 (skill-team-conversion-design-v1.md §5; D6 in §6) — TypeScript port of
 * `claw-code-reference rust/crates/runtime/src/permissions.rs`.
 *
 * **PR-D update (Round 4 Lane 2, 2026-05-26)**: extended PermissionMode with
 * `'prompt'`. The Rust upstream has always had this third mode; we deferred
 * it during S3 because the interactive SSE plumbing wasn't ready. Now that
 * builtin write-tier tools (write_file / edit_file) want prompt semantics,
 * the union is widened here at the base type so callers (and Lane 1's
 * permission-policy-v2.ts) can rely on it.
 *
 * The synchronous `authorize()` here treats `'prompt'` as **deny by default**
 * — it has no callback, no async, no prompter. permission-policy-v2.ts
 * (Lane 1) extends this class with the interactive prompter for the runtime.
 * Keeping the base class deny-on-prompt is the safe choice: any caller that
 * hasn't been upgraded to v2 will fail closed.
 *
 * Static factory `fromAllowedTools(['bash', ...])` builds the canonical
 * "deny-by-default + listed tools allowed" policy that maps directly to
 * Anthropic SKILL.md `allowed-tools: [...]` frontmatter.
 */

export type PermissionMode = 'allow' | 'deny' | 'prompt';

/**
 * authorize() return shape.
 *   - { allow: true }            tool may run immediately
 *   - { deny: reason }           tool refused; reason echoes back to the LLM
 *   - { prompt: reason }         tool requires human approval before running.
 *                                The synchronous base class never emits this
 *                                — permission-policy-v2.ts (Lane 1) does the
 *                                actual prompt handling. Kept in the union so
 *                                downstream types stay consistent.
 */
export type PermissionOutcome =
  | { allow: true }
  | { deny: string }
  | { prompt: string };

export class PermissionPolicy {
  /**
   * @param defaultMode  fallback applied when toolModes has no entry for the
   *                     requested tool name.
   * @param toolModes    per-tool override map. Stored by reference; callers
   *                     should treat it as owned by the policy after construction.
   */
  constructor(
    private readonly defaultMode: PermissionMode,
    private readonly toolModes: Map<string, PermissionMode> = new Map(),
  ) {}

  /**
   * Resolve the effective mode for one tool. Override beats default; missing
   * override falls through to defaultMode.
   */
  modeFor(toolName: string): PermissionMode {
    const m = this.toolModes.get(toolName);
    return m ?? this.defaultMode;
  }

  /**
   * Decide whether the runtime may invoke `toolName`. Synchronous: D6 means
   * we never block on an external prompter, so the answer is always immediate.
   * Deny reason is suitable for echoing back into the LLM's `tool_result`
   * block with `is_error: true`.
   *
   * @param _input  serialized tool input (currently unused; v3 placeholder).
   *                Prefixed with underscore to mark it as reserved for the
   *                future `'prompt'` mode where the prompter needs to see
   *                concrete input before approving. Keeping the slot in the
   *                signature now means call sites won't churn when v3 lands.
   */
  authorize(toolName: string, _input?: string): PermissionOutcome {
    const mode = this.modeFor(toolName);
    if (mode === 'allow') return { allow: true };
    if (mode === 'prompt') {
      // Synchronous base class fails closed on prompt. permission-policy-v2.ts
      // (Lane 1) overrides this to invoke an interactive prompter and may
      // return { prompt: ... } or resolve through user approval.
      return { deny: `tool '${toolName}' requires approval (prompt mode); no prompter wired` };
    }
    return { deny: `tool '${toolName}' denied by permission policy` };
  }

  /**
   * Canonical MVP factory: deny everything except the explicitly listed tools.
   * Maps directly to Anthropic SKILL.md `allowed-tools: [Bash, Read, Edit]`
   * frontmatter — pass the parsed list straight in.
   *
   * Empty list → deny-everything policy (useful for "no tools" skills).
   *
   * **Case sensitivity contract**: matching is exact. Callers are responsible
   * for passing tool names byte-identical to whatever `authorize()` will see
   * at runtime (which is the literal frontmatter `allowed-tools` entry, e.g.
   * `Bash` not `bash`). We deliberately do not normalize case here — SKILL.md
   * frontmatter is the source of truth and silent case-folding would mask
   * authoring typos.
   */
  static fromAllowedTools(allowedTools: readonly string[]): PermissionPolicy {
    return new PermissionPolicy(
      'deny',
      new Map(allowedTools.map((t) => [t, 'allow' as const])),
    );
  }
}
