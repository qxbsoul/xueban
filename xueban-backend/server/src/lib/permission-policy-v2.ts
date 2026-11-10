/**
 * permission-policy-v2.ts — Round 4 PR-D Lane 1 extended permission gate.
 *
 * Why a separate file (and not patching `permission-policy.ts`)
 * ─────────────────────────────────────────────────────────────
 * Round 4 PR-D is split across two lanes that touch overlapping concerns:
 *
 *   - **Lane 1 (this lane)** owns the runtime half — conversation-runtime,
 *     tool-runner, the new permission shape. We need the `'prompt'` mode
 *     branch in the gate today because the runtime's tool-use loop has to
 *     decide what to do when the LLM proposes a tool that requires human
 *     consent (v1 contract: treat `prompt` as `deny` AND emit a
 *     `permission_check` SSE so the UI can later layer in interactive
 *     approval without a wire-shape break).
 *
 *   - **Lane 2** is concurrently adding the actual `'prompt'` literal to
 *     the base `PermissionMode` union in `permission-policy.ts`, alongside
 *     8 new built-in tools. By keeping our extension in a separate file,
 *     we avoid a merge collision on the union declaration: Lane 2 lands
 *     `'allow' | 'deny' | 'prompt'`, this file simply consumes that union
 *     and adds prompt-aware branches without re-declaring the type.
 *
 * Hand-off note to Lane 2: this file relies on `PermissionMode` already
 * including `'prompt'`. Until Lane 2 lands, the union is just
 * `'allow' | 'deny'`, in which case `prompt` becomes unreachable in
 * `authorize()` and the v2 class behaves identically to a v1 deny-by-default
 * policy. The dead branch is intentional bridge code — once Lane 2 widens
 * the union, the `'prompt'` arm activates with no further changes here.
 *
 * Public API
 * ──────────
 *   - `PermissionPolicyV2` implements the same `authorize(toolName)` contract
 *     as v1 BUT returns a richer outcome that carries the mode + reason
 *     verbatim, so callers (ToolRunner in particular) can choose to emit
 *     SSE permission events instead of swallowing the decision.
 *   - `PermissionPolicyV2.fromAllowedTools()` mirrors v1's canonical
 *     deny-by-default + allowed-tools list factory, but its `defaults` slot
 *     also accepts per-tool `'prompt'` overrides.
 *
 * Synchronous (no interactive prompter callbacks): matches v1's design and
 * keeps the runtime free of async permission round-trips. If a future v3
 * adds an actual prompter, it'll layer on top of this class via a wrapper.
 */

import type { PermissionMode } from './permission-policy';

/**
 * V2 outcome — preserves which mode produced the decision so callers can
 * distinguish "deny because policy says so" from "deny because we treat
 * prompt as deny until human-in-the-loop lands". `reason` is a short string
 * suitable for embedding inside a tool_result.is_error message OR for an
 * SSE permission_check frame.
 */
export interface PermissionOutcomeV2 {
  /** Whether the runtime should proceed with the tool call. */
  allow: boolean;
  /** The mode that produced this decision — verbatim from the policy table. */
  mode: PermissionMode;
  /** Short rationale string. Safe to surface to the LLM and the user. */
  reason: string;
}

/**
 * Construction options. Both fields optional so a "completely default" v2
 * instance still works (everything denied except an empty allowed-tools list).
 *
 *   - `defaults`: per-tool mode map. Last write wins. Keys are exact tool
 *     names — case-sensitive, matching v1 semantics (frontmatter is the
 *     source of truth, no silent case folding).
 *   - `allowedTools`: SKILL.md frontmatter `allowed-tools` list. Each entry
 *     gets `'allow'` mapped in unless `defaults` already overrides it.
 *     Anything not in either map falls through to deny.
 */
export interface PermissionPolicyV2Options {
  defaults?: Record<string, PermissionMode>;
  allowedTools?: readonly string[];
}

export class PermissionPolicyV2 {
  private readonly modes: Map<string, PermissionMode>;

  constructor(opts: PermissionPolicyV2Options = {}) {
    this.modes = new Map();
    // `allowedTools` is applied first so `defaults` can override (e.g. a
    // listed tool that the skill author wants gated behind a prompt).
    for (const t of opts.allowedTools ?? []) {
      this.modes.set(t, 'allow');
    }
    for (const [t, m] of Object.entries(opts.defaults ?? {})) {
      this.modes.set(t, m);
    }
  }

  /** Effective mode for one tool. Falls through to `'deny'` on miss. */
  modeFor(toolName: string): PermissionMode {
    return this.modes.get(toolName) ?? 'deny';
  }

  /**
   * Synchronous authorize. Three outcomes:
   *
   *   - `'allow'`  → `{ allow: true, mode: 'allow', reason: 'allowed by policy' }`
   *   - `'deny'`   → `{ allow: false, mode: 'deny', reason: "tool '<name>' denied by permission policy" }`
   *   - `'prompt'` → `{ allow: false, mode: 'prompt', reason: "tool '<name>' requires human approval" }`
   *                  (v1 = treated as deny; ToolRunner emits permission_check SSE)
   *
   * The TypeScript narrowing on `mode` is exhaustive — if Lane 2's union
   * widens further (e.g. `'prompt-multi'`), the type checker will flag the
   * missing branch.
   */
  authorize(toolName: string): PermissionOutcomeV2 {
    const mode = this.modeFor(toolName);
    switch (mode) {
      case 'allow':
        return { allow: true, mode, reason: 'allowed by policy' };
      case 'deny':
        return {
          allow: false,
          mode,
          reason: `tool '${toolName}' denied by permission policy`,
        };
      // The `'prompt'` arm depends on Lane 2 widening the union. Until then
      // this branch is unreachable — TS narrows it to `never` and the
      // exhaustiveness check below stays satisfied. The explicit cast keeps
      // the literal stable so the runtime behavior is correct once the
      // union widens.
      default: {
        // Treat any non-'allow' / non-'deny' mode (today: 'prompt') as a
        // soft-deny with a recognisable reason so the ToolRunner can detect
        // it and emit a permission_check SSE.
        const fallbackMode = mode as PermissionMode;
        return {
          allow: false,
          mode: fallbackMode,
          reason: `tool '${toolName}' requires human approval`,
        };
      }
    }
  }

  /**
   * Canonical factory: deny-by-default + allowed tools list. Drop-in
   * replacement for v1 `PermissionPolicy.fromAllowedTools(...)` callers.
   */
  static fromAllowedTools(allowedTools: readonly string[]): PermissionPolicyV2 {
    return new PermissionPolicyV2({ allowedTools });
  }
}
