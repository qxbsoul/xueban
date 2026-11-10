/**
 * anthropic-block-adapter.ts — wire-shape ↔ internal-shape converter for
 * ContentBlock (S1) and ConversationMessage (S1).
 *
 * Why this module exists
 * ──────────────────────
 * Our internal `ContentBlock` (conversation-types.ts) uses `kind` as the
 * discriminant. The Anthropic Messages API uses `type` on the wire. Without a
 * single dedicated translation layer, every call site that talks to the LLM
 * (S5 ConversationRuntime, S6 assembler re-wiring, future provider adapters)
 * would do its own ad-hoc `kind ↔ type` mapping and we'd drift.
 *
 * S1-S3 Checker P1 review (design §5 line 1083) explicitly calls out that S5
 * needs this mapping and that it must be **centralized in one file**, not
 * scattered through the Runtime. So we ship the adapter as its own S5-companion
 * module ahead of ConversationRuntime.
 *
 * Scope
 * ─────
 * Pure data conversion. NO I/O, NO SSE emission, NO business logic. Importing
 * this module is side-effect free. It only knows about two shapes:
 *
 *   - Internal:  ContentBlock  (kind: 'text' | 'tool_use' | 'tool_result')
 *   - Wire:      AnthropicBlock (type: 'text' | 'tool_use' | 'tool_result')
 *
 * Asymmetry: ToolResult.tool_name
 * ───────────────────────────────
 * Our internal ToolResult carries `tool_name` for debug / logging. The
 * Anthropic wire format does NOT — it only references the prior ToolUse by id
 * (`tool_use_id`). When converting wire → internal we therefore leave
 * `tool_name` as empty string; the caller (ConversationRuntime) knows the
 * pending ToolUse's name and is responsible for enriching the block. Round-
 * trip tests must skip this field (or pre-fill it on the internal side before
 * comparing).
 */

import type { ContentBlock, ConversationMessage } from './conversation-types';

/**
 * AnthropicBlock — exactly the shape that goes on the wire to the Anthropic
 * Messages API in the `messages[].content[]` array. Mirrors @anthropic-ai/sdk
 * ContentBlock types but kept local so this module has zero external imports.
 *
 * Notes:
 *  - `tool_use.input` is `unknown` (Anthropic types it as `Record<string,
 *    unknown>` but in practice provider models can emit any JSON value).
 *  - `tool_result.content` is a string here (the JSON-stringified output).
 *    Anthropic supports a richer multi-part content shape but we only ever
 *    send/receive plain strings, matching the Rust port's behavior.
 */
export type AnthropicBlock =
  | { type: 'text'; text: string }
  | { type: 'tool_use'; id: string; name: string; input: unknown }
  | { type: 'tool_result'; tool_use_id: string; content: string; is_error?: boolean };

/**
 * Internal ContentBlock → wire-format AnthropicBlock. Total — every variant
 * is mapped explicitly. The switch is exhaustive on `kind`; TS will complain
 * if a new variant is added without updating this function.
 */
export function toAnthropicBlock(b: ContentBlock): AnthropicBlock {
  switch (b.kind) {
    case 'text':
      return { type: 'text', text: b.text };
    case 'image':
    case 'video':
      throw new Error('当前 Anthropic 适配器不接受 MiMo 多模态附件');
    case 'tool_use':
      return { type: 'tool_use', id: b.id, name: b.name, input: b.input };
    case 'tool_result':
      return {
        type: 'tool_result',
        tool_use_id: b.tool_use_id,
        content: b.output,
        is_error: b.is_error,
      };
  }
}

/**
 * Wire-format AnthropicBlock → internal ContentBlock.
 *
 * ToolResult.tool_name is filled with empty string here — the Anthropic wire
 * format does not carry the original tool name, only `tool_use_id`. The
 * caller (ConversationRuntime) is expected to enrich `tool_name` from the
 * pending ToolUse before storing the block in session.messages.
 *
 * For ToolResult.is_error: wire field is optional (`undefined` means "not
 * error"). Internally we keep it as a required boolean, so undefined → false.
 *
 * Note (S5 P0 #2 — Checker review): in the S5/S6 flow this `tool_result`
 * branch is never reached — wire never delivers tool_result to us.
 * ContentRuntime constructs tool_result blocks directly (with `tool_name`
 * enriched from the matching pending tool_use). This branch exists for
 * protocol completeness only (round-trip tests, future provider adapters
 * that might echo tool_result blocks back in a different shape).
 */
export function fromAnthropicBlock(b: AnthropicBlock): ContentBlock {
  switch (b.type) {
    case 'text':
      return { kind: 'text', text: b.text };
    case 'tool_use':
      return { kind: 'tool_use', id: b.id, name: b.name, input: b.input };
    case 'tool_result':
      // See JSDoc: dead branch in S5/S6 production flow. Kept for protocol
      // completeness only — do NOT throw, callers may exercise it from tests.
      return {
        kind: 'tool_result',
        tool_use_id: b.tool_use_id,
        tool_name: '',
        output: b.content,
        is_error: b.is_error ?? false,
      };
  }
}

/**
 * One Anthropic messages[].content[] envelope. Note: Anthropic's API does not
 * accept `role: 'tool'` directly — tool_result blocks live inside a `user`
 * message. We collapse our internal `role: 'tool'` into a `user` role on the
 * wire to match. The internal session keeps the distinction for debugging /
 * UI display.
 */
export interface AnthropicMessage {
  role: 'user' | 'assistant' | 'system';
  content: AnthropicBlock[];
}

/**
 * Convert a slice of internal ConversationMessage[] to the Anthropic
 * `messages` field shape.
 *
 *   - `role: 'tool'` is folded into `role: 'user'` per Anthropic convention.
 *   - **Consecutive `role: 'tool'` messages are MERGED** into a single wire
 *     `role: 'user'` envelope (Anthropic Messages API §contract requires
 *     strict user/assistant alternation; two adjacent `role: 'user'`
 *     messages → HTTP 422). ConversationRuntime emits one tool message per
 *     tool_use block (for fine-grained debug / replay), so this fold is what
 *     keeps the wire shape legal when an assistant turn dispatched multiple
 *     tool_uses in one shot. Boundary protection: a "real" `role: 'user'`
 *     message followed by `role: 'tool'` does NOT merge — those are two
 *     genuinely distinct turns and the API accepts them as text-user then
 *     tool-result-user only if separated by an assistant turn. (Today the
 *     runtime never produces user→tool adjacent without an assistant
 *     between, but we preserve them as separate wire entries so the bug
 *     surfaces as a clean 422 instead of silently corrupting history.)
 *   - `role: 'system'` passes through; callers are responsible for ensuring
 *     system messages live in the top-level `system` field instead (Anthropic
 *     API requires that) — we don't filter here, that's policy not data.
 *   - `usage` is dropped (request-side messages don't carry usage; usage only
 *     comes back on the assistant turn's stream).
 *
 * S5 P0 #1 fix (Checker review): previously this function used `.map`, which
 * produced two adjacent `role: 'user'` wire envelopes when the runtime
 * dispatched ≥2 tools in one turn (e.g. 4-agent skill C scenario echo + add).
 * The reduce-based fold below merges only **tool→tool runs**, never
 * user→tool, to keep the boundary safe.
 */
export function toAnthropicMessages(messages: ConversationMessage[]): AnthropicMessage[] {
  const out: AnthropicMessage[] = [];
  // Track whether the LAST pushed entry came from a `role: 'tool'` internal
  // message — that is the only case where we're allowed to extend it in place
  // with subsequent tool_result blocks. A user-text message that happens to
  // map to `role: 'user'` must NOT be merged with a following tool message.
  let lastWasFoldedTool = false;

  for (const m of messages) {
    const wireBlocks = m.blocks.map(toAnthropicBlock);

    if (m.role === 'tool') {
      if (lastWasFoldedTool && out.length > 0) {
        // Extend the previous folded-tool wire envelope in place.
        out[out.length - 1].content.push(...wireBlocks);
      } else {
        out.push({ role: 'user', content: wireBlocks });
        lastWasFoldedTool = true;
      }
    } else {
      out.push({ role: m.role, content: wireBlocks });
      lastWasFoldedTool = false;
    }
  }

  return out;
}
