/**
 * tool-approval.ts — pending-approval registry for the Node tool pipeline.
 *
 * ROADMAP-agentinfra 阶段 1 / GAP-dsh-alignment G1: bridges
 * `PermissionPolicyV2`'s `'prompt'` mode (which v1 downgraded to a soft deny)
 * into a real human-in-the-loop round trip. Semantics mirror the Python side
 * (`xueban/runtime/permissions.py` auto|prompt|block, prompt reuses the
 * approval machinery + `runtime/approval/service.py` pending/approve/reject):
 *
 *   ToolRunner sees mode === 'prompt'
 *     → `request(sessionId, toolUse)` — emits a `permission_check` frame with
 *       decision:'pending' and parks on a promise
 *     → user POSTs /api/run-sessions/:id/permissions/:requestId
 *       → `decide()` settles the promise — emits a `permission_check` frame
 *         carrying the decision
 *     → 'approve' → the executor runs; 'deny' / timeout / abort → the runtime
 *       packs an is_error tool_result (fail-closed, like Python's prompt
 *       timeout → reject).
 *
 * Frames go out through an injected `emit(sessionId, event, frame)` callback so
 * this module stays lib-pure (no routes import, no bus import — the same
 * boundary session-store keeps). `routes/run-sessions.ts` wires the emitter to
 * its private runBus, so both frames land in the run's monotonic event log and
 * replay on reconnect — that is the "决策为命名事件，可回放" acceptance.
 *
 * Naming note: one wire event name (`permission_check`) for request and
 * decision, distinguished by `data.decision`. Python's durable counterparts are
 * `permission.checked` / `permission.blocked` (run_event_log.py); the unified
 * naming table (ROADMAP 阶段 2) will register the mapping.
 */

import { randomUUID } from 'crypto';

export type PermissionDecision = 'approve' | 'deny';

/** Wire payload for the `permission_check` event (request frame + decision frame). */
export interface PermissionCheckFrame {
  request_id: string;
  session_id: string;
  tool_name: string;
  tool_input?: unknown;
  mode: 'prompt';
  reason: string;
  /** 'pending' on the request frame; final outcome on the decision frame. */
  decision: 'pending' | 'approve' | 'deny';
  /** Who settled the decision. Absent on the pending frame. */
  decided_by?: 'user' | 'timeout' | 'abort';
  /** Optional user-supplied reason (deny) — surfaced to the LLM and timeline. */
  decision_reason?: string;
  requested_at: number;
  decided_at?: number;
}

export type PermissionEmit = (
  sessionId: string,
  event: 'permission_check',
  frame: PermissionCheckFrame,
) => void;

/** Read-only view for `GET /api/run-sessions/:id/permissions`. */
export interface PendingPermissionRequest {
  request_id: string;
  session_id: string;
  tool_name: string;
  requested_at: number;
}

interface PendingEntry {
  frame: PermissionCheckFrame;
  settled: boolean;
  timer: NodeJS.Timeout | null;
  settle: (
    decision: PermissionDecision,
    decidedBy: 'user' | 'timeout' | 'abort',
    decisionReason?: string,
  ) => void;
}

export interface ToolApprovalRegistry {
  /** Wire the frame emitter (routes layer, once, to its runBus). */
  setEmitter(emit: PermissionEmit): void;
  /**
   * Park one tool call for approval. Emits the pending frame immediately
   * (even while the caller awaits), then resolves when the user decides, the
   * timeout fires, or the abort signal trips. Timeout/abort settle as 'deny'.
   */
  request(
    sessionId: string,
    toolUse: { name: string; input?: unknown },
    reason: string,
    signal?: AbortSignal,
  ): Promise<PermissionDecision>;
  /**
   * Settle a pending request from the REST endpoint. Returns false when the
   * id is unknown, already settled, or belongs to a different session
   * (`expectedSession` provided and mismatched).
   */
  decide(
    requestId: string,
    decision: PermissionDecision,
    reason?: string,
    expectedSession?: string,
  ): boolean;
  /** Pending (unresolved) requests, optionally filtered by session. */
  listPending(sessionId?: string): PendingPermissionRequest[];
  /** Test/introspection helper. */
  _clearForTests(): void;
}

export interface ToolApprovalOptions {
  emit?: PermissionEmit;
  /** Default 120_000ms; env override SF_TOOL_APPROVAL_TIMEOUT_MS wins if set. */
  timeoutMs?: number;
}

function envTimeoutMs(): number | undefined {
  const raw = Number(process.env.SF_TOOL_APPROVAL_TIMEOUT_MS);
  return Number.isFinite(raw) && raw > 0 ? raw : undefined;
}

export function createToolApprovalRegistry(
  opts: ToolApprovalOptions = {},
): ToolApprovalRegistry {
  const timeoutMs = opts.timeoutMs ?? envTimeoutMs() ?? 120_000;
  const pending = new Map<string, PendingEntry>();
  let emit: PermissionEmit | undefined = opts.emit;

  function settleEntry(
    entry: PendingEntry,
    requestId: string,
    decision: PermissionDecision,
    decidedBy: 'user' | 'timeout' | 'abort',
    decisionReason?: string,
  ): void {
    if (entry.settled) return;
    entry.settled = true;
    if (entry.timer) clearTimeout(entry.timer);
    pending.delete(requestId);
    const decidedFrame: PermissionCheckFrame = {
      ...entry.frame,
      decision,
      decided_by: decidedBy,
      decision_reason: decisionReason,
      decided_at: Date.now(),
    };
    try {
      emit?.(decidedFrame.session_id, 'permission_check', decidedFrame);
    } catch {
      /* a dead sink must not break the decision path */
    }
    entry.settle(decision, decidedBy, decisionReason);
  }

  return {
    setEmitter(fn: PermissionEmit): void {
      emit = fn;
    },

    request(sessionId, toolUse, reason, signal) {
      const requestId = randomUUID();
      const frame: PermissionCheckFrame = {
        request_id: requestId,
        session_id: sessionId,
        tool_name: toolUse.name,
        tool_input: toolUse.input,
        mode: 'prompt',
        reason,
        decision: 'pending',
        requested_at: Date.now(),
      };

      let resolveFn!: (d: PermissionDecision) => void;
      const promise = new Promise<PermissionDecision>((resolve) => {
        resolveFn = resolve;
      });
      const entry: PendingEntry = {
        frame,
        settled: false,
        timer: null,
        settle: (decision) => resolveFn(decision),
      };
      pending.set(requestId, entry);

      // Pending frame first — the UI must see the prompt while the caller
      // is parked on the promise, not after the decision lands.
      try {
        emit?.(sessionId, 'permission_check', frame);
      } catch {
        /* ignore sink errors; the wait itself still works */
      }

      entry.timer = setTimeout(
        () =>
          settleEntry(entry, requestId, 'deny', 'timeout', 'approval timed out'),
        timeoutMs,
      );
      entry.timer.unref?.();

      if (signal) {
        if (signal.aborted) {
          settleEntry(entry, requestId, 'deny', 'abort', 'aborted before approval');
        } else {
          signal.addEventListener(
            'abort',
            () =>
              settleEntry(
                entry,
                requestId,
                'deny',
                'abort',
                'aborted by caller signal',
              ),
            { once: true },
          );
        }
      }

      return promise;
    },

    decide(requestId, decision, reason, expectedSession) {
      const entry = pending.get(requestId);
      if (!entry || entry.settled) return false;
      if (
        expectedSession !== undefined &&
        entry.frame.session_id !== expectedSession
      ) {
        return false;
      }
      settleEntry(entry, requestId, decision, 'user', reason);
      return true;
    },

    listPending(sessionId) {
      const out: PendingPermissionRequest[] = [];
      for (const entry of pending.values()) {
        if (sessionId !== undefined && entry.frame.session_id !== sessionId) {
          continue;
        }
        out.push({
          request_id: entry.frame.request_id,
          session_id: entry.frame.session_id,
          tool_name: entry.frame.tool_name,
          requested_at: entry.frame.requested_at,
        });
      }
      return out;
    },

    _clearForTests() {
      for (const entry of pending.values()) {
        if (entry.timer) clearTimeout(entry.timer);
      }
      pending.clear();
    },
  };
}

/**
 * Process-singleton shared by the lib wiring (assembler / workflow executor
 * construct prompters against it) and the routes layer (decision endpoint).
 * Same pattern as tool-runner's module-level executor registry: tool approval
 * has no per-instance state worth injecting by hand at every construction.
 */
export const toolApprovals = createToolApprovalRegistry();
