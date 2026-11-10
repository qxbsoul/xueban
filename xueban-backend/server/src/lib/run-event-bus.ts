/**
 * run-event-bus.ts — T3-1 (docs/architecture/opendesign-streaming-architecture-study.md §5).
 *
 * The "missing backend" piece modelled on OpenDesign's `apps/daemon/src/runs.ts`:
 * a run is a PERSISTENT entity with a monotonically-numbered, buffered event log
 * and a set of attached SSE views. The agent/pipeline runs ONCE and `emit()`s
 * into the log; SSE connections are detachable/re-attachable VIEWS that resume
 * from a cursor (`?after` / Last-Event-ID).
 *
 * This decouples *execution* from the *SSE connection* and fixes the two
 * architectural symptoms the previous design had (audit 2026-05-27):
 *   - P3 (user message shown twice): reconnect used to re-run the whole pipeline
 *     through a fresh projector with new random ids. With a single run + stable
 *     monotonic event ids, a reconnect replays the SAME buffered events — no
 *     duplication, no re-run.
 *   - Wasted tokens / inconsistent output: N reconnects = N LLM executions
 *     previously. Now N views share ONE execution.
 *
 * Express-agnostic by design: `attach()` takes a plain `RunEventSink` so this
 * module stays unit-testable without HTTP. The route layer owns res/heartbeat/
 * req-close and adapts them to a sink.
 */

/** One buffered event. `id` is monotonic within a run (1-based). */
export interface RunEventRecord {
  id: number;
  event: string;
  data: unknown;
  ts: number;
}

export type RunStatus = 'running' | 'succeeded' | 'failed' | 'canceled';

/**
 * O2 / T3-5 — the serializable subset of a Run, written to disk by an injected
 * RunPersistence so the timeline survives a Node restart. Excludes the live-only
 * fields (clients, abort, waiters) which are reconstructed empty on hydrate.
 */
export interface RunSnapshot {
  id: string;
  status: RunStatus;
  events: RunEventRecord[];
  nextEventId: number;
  exitCode: number | null;
  signal: string | null;
  createdAt: number;
  updatedAt: number;
}

/**
 * Injected disk-persistence adapter (impl: run-event-store.ts). Kept as an
 * interface so run-event-bus stays Express/fs-agnostic and unit-testable with a
 * fake. All methods are best-effort: the bus never awaits or depends on them
 * for correctness — disk is a recovery log, memory is the runtime truth.
 */
export interface RunPersistence {
  /** Persist (typically debounced) a run snapshot. */
  save(snap: RunSnapshot): void;
  /** Remove a run's persisted snapshot. */
  remove(id: string): void;
  /** Synchronously load all persisted snapshots (called once at construction). */
  loadAllSync(): RunSnapshot[];
  /**
   * O4 — synchronously load a SINGLE persisted snapshot by id, or undefined if
   * absent. Optional for back-compat with existing fakes; when omitted, the
   * bus's lazy `hydrateOne()` can only serve runs already loaded at startup.
   * Used to recover a run whose snapshot is on disk but was never hydrated
   * (e.g. created after startup, then the in-memory run was GC'd by TTL while
   * its disk snapshot lingered — a narrow window, but it lets /stream replay
   * read-only instead of hard-404ing).
   */
  loadOneSync?(id: string): RunSnapshot | undefined;
}

const TERMINAL: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'succeeded',
  'failed',
  'canceled',
]);

/**
 * hop 卡终态集合 — contracts.ts hop_update patch.status 里除 'running' 外皆终态
 * （done/failed/timeout/empty/denied/aborted）。清扫用它判「已有结算、勿再补」。
 */
const HOP_TERMINAL: ReadonlySet<string> = new Set([
  'done',
  'failed',
  'timeout',
  'empty',
  'denied',
  'aborted',
]);

/** A connected SSE view. The route layer implements these against `res`. */
export interface RunEventSink {
  /** Write one event frame. Called for replayed AND live events. */
  send(rec: RunEventRecord): void;
  /** Stream finished (run reached terminal). Close the HTTP response. */
  end(): void;
}

export interface Run {
  id: string;
  status: RunStatus;
  events: RunEventRecord[];
  nextEventId: number;
  clients: Set<RunEventSink>;
  /** Aborts the underlying execution (LLM stream / child process). */
  abort: AbortController;
  /** True once a pipeline has been claimed to start (single-start guard). */
  started: boolean;
  /**
   * Pending `wait()` resolvers. Each is called with the run's terminal status
   * exactly once when the run reaches a terminal state (finish / cancel /
   * shutdownActive) or is dropped (reset), then the set is cleared so no
   * waiter ever hangs forever. Mirrors OpenDesign `runs.ts` `run.waiters`.
   */
  waiters: Set<(status: RunStatus) => void>;
  /**
   * Exit code of the underlying CLI child process, once known (A10). `null`
   * means "not exited via code" (still running, killed by signal, or never
   * spawned a child). Lets callers distinguish a crash (non-zero) from a clean
   * exit (0) after the run reaches a terminal status.
   */
  exitCode: number | null;
  /**
   * Terminating signal of the underlying CLI child process, once known (A10).
   * e.g. `'SIGTERM'` / `'SIGKILL'`. `null` when the child exited via a code or
   * never spawned.
   */
  signal: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface RunBusOptions {
  /** Ring-buffer cap per run (drop oldest beyond this). */
  maxEvents?: number;
  /** How long after terminal a run stays queryable, ms. */
  ttlMs?: number;
  /** Injectable clock + cleanup scheduler (tests). */
  now?: () => number;
  schedule?: (fn: () => void, ms: number) => void;
  /**
   * O2 / T3-5 — optional disk persistence. When provided, runs are hydrated
   * from it at construction and snapshots are saved on emit/finish/setExit and
   * removed on reset/cleanup. Omit (the default) for pure in-memory behavior —
   * existing callers and the 28 unit tests are unaffected.
   */
  persist?: RunPersistence;
}

export interface RunBus {
  /** Get an existing run, or create one in `running` state. */
  ensure(id: string): Run;
  get(id: string): Run | undefined;
  /**
   * O4 — lazily recover a single run from disk if it isn't in memory. Returns
   * the run if it's already in memory OR was successfully loaded from the
   * injected persistence's `loadOneSync`; undefined if there's nothing to
   * recover (no persistence, no `loadOneSync`, or no snapshot on disk). A
   * recovered run is hydrated with the SAME semantics as startup hydration:
   * non-terminal (crash-interrupted) snapshots get a synthetic terminal error
   * frame + `canceled` status and `started:true` so it is served READ-ONLY
   * (never re-run). Idempotent — a no-op for an already-present run.
   */
  hydrateOne(id: string): Run | undefined;
  /**
   * Claim the right to start this run's pipeline. Returns true exactly once per
   * run (the FIRST caller); subsequent callers (reconnects) get false and must
   * NOT re-run — they only attach. Creates the run if absent.
   */
  claimStart(id: string): boolean;
  /** Append an event: assign monotonic id, buffer (capped), fan out to clients. */
  emit(id: string, event: string, data: unknown): RunEventRecord | undefined;
  /**
   * Attach an SSE view. Replays buffered events with `id > after`, then (unless
   * already terminal) registers the sink for live events. Returns a detach fn.
   * If terminal after replay, calls `sink.end()` and returns a no-op detach.
   */
  attach(id: string, after: number, sink: RunEventSink): () => void;
  /** Mark terminal, flush `end` to clients, schedule cleanup. Idempotent. */
  finish(id: string, status: Exclude<RunStatus, 'running'>): void;
  /**
   * Resolve when the run reaches a terminal status (OpenDesign `runs.ts:198`).
   * If the run is absent (never created / already cleaned up) or already
   * terminal, resolves immediately with the current/terminal status; otherwise
   * registers a waiter that fires on the next finish/cancel/reset transition.
   * Never rejects, never hangs past a terminal/reset transition.
   */
  wait(id: string): Promise<RunStatus>;
  /** Request cancellation: abort execution; finish as `canceled`. */
  cancel(id: string): void;
  /**
   * Record the underlying CLI child process's exit outcome (A10). `code` is the
   * numeric exit code (or null if killed by signal); `signal` is the killing
   * signal (or null if exited via code). No-op for unknown runs. Does NOT change
   * status — finish/cancel own that; this is pure metadata for crash diagnosis.
   */
  setExit(id: string, code: number | null, signal: string | null): void;
  /**
   * Graceful shutdown (A8): cancel every non-terminal run — abort its execution
   * signal (the cli spawner listens and SIGTERM→SIGKILLs its child) and finish
   * it as `canceled`. Optionally waits up to `graceMs` (default 3000) for those
   * downstream child kills to settle before resolving, so the process can exit
   * cleanly. The wait timer is unref'd so it never keeps the process alive.
   */
  shutdownActive(opts?: { graceMs?: number }): Promise<void>;
  /**
   * Drop a run entirely so the next `claimStart` re-runs from scratch. Used by
   * retry/resume ("full_rerun"): aborts any in-flight execution, ends attached
   * views, and removes the run + its buffered log. The next GET /stream then
   * `ensure`s a fresh run and `claimStart` returns true again.
   */
  reset(id: string): void;
  isTerminal(status: RunStatus): boolean;
  /** Test/introspection. */
  size(): number;
}

export function createRunBus(opts: RunBusOptions = {}): RunBus {
  const maxEvents = opts.maxEvents ?? 2000;
  const ttlMs = opts.ttlMs ?? 30 * 60 * 1000;
  const now = opts.now ?? Date.now;
  const schedule =
    opts.schedule ??
    ((fn, ms) => {
      const t = setTimeout(fn, ms);
      // Don't keep the process alive just for cleanup.
      (t as { unref?: () => void }).unref?.();
    });

  const persist = opts.persist;
  const runs = new Map<string, Run>();

  /** Serializable view of a run for disk persistence (O2). */
  const snapshot = (run: Run): RunSnapshot => ({
    id: run.id,
    status: run.status,
    events: run.events,
    nextEventId: run.nextEventId,
    exitCode: run.exitCode,
    signal: run.signal,
    createdAt: run.createdAt,
    updatedAt: run.updatedAt,
  });

  /**
   * 2026-11-06「消息结束了还在转圈」修复 — run 终态时事件日志里可能残留
   * 未收口的过程卡：thinking(streaming) / tool_call(running) / msg_foot(running)。
   * 正常路径生产者会先发 thinking_finalize / tool_finalize / msg_foot(done) 再
   * finish；但崩溃、重启、abort 中断等路径会跳过收口 —— 回放(replayTimeline)
   * 只认 message/message-patch 事件，这些卡就永远停在 streaming（前端转圈
   * 23 小时的根因）。这里扫一遍日志，给每张未终态卡就地补一条终态 patch
   * （'message-patch' 事件追加到日志尾部），使任何回放路径都干净落定。
   * 2026-11-07 hop 卡收口 — hop_card 帧（pending/running）同样在此补
   * hop_update {status:'aborted'} 终态：SQLite 侧有 sweepOrphanHops 兜底，
   * 但重放可见的时间线侧此前无人收口，崩溃/中断后 hop 卡永卡「交接中」。
   * 2026-11-07 plan 卡任务行收口 — plan_card 的行级 settle（plan_update_task）
   * 同样可能在中断时缺席，running 行在回放里永远转圈（Loader2）：按行归并
   * message 初值 + 补丁取最终状态，仍 'running' 的行补
   * plan_update_task {status:'failed'} 终态补丁。pending 行有意不动 ——
   * 「待派发」如实反映从未开始，且正常收尾可能存在有意不派发的行，不误标。
   * 幂等：已终态的卡直接跳过（含本清扫补过的 —— 合成 patch 也在日志里，
   * 行级同理：合成 plan_update_task 归并后不再是 'running'，二次清扫不命中）。
   */
  const sweepOpenProcessCards = (run: Run): number => {
    // 按消息 id 归并已见过的终态信号（append/中间帧不算）——后写的终态优先。
    const finalized = new Set<string>();
    // plan_card 行级最终状态：(卡 id, task id) → status（后写覆盖；无 status
    // 的补丁如 elapsed_ms 不改行状态）。
    const rowStatus = new Map<string, Map<string, string>>();
    for (const rec of run.events) {
      if (rec.event !== 'message-patch') continue;
      const p = rec.data as {
        id?: string;
        op?: string;
        task_id?: string;
        patch?: { status?: string };
      } | null;
      if (!p || typeof p.id !== 'string') continue;
      if (p.op === 'plan_update_task' && typeof p.task_id === 'string') {
        let byTask = rowStatus.get(p.id);
        if (!byTask) {
          byTask = new Map<string, string>();
          rowStatus.set(p.id, byTask);
        }
        if (typeof p.patch?.status === 'string') byTask.set(p.task_id, p.patch.status);
        continue;
      }
      if (
        p.op === 'thinking_finalize' ||
        p.op === 'tool_finalize' ||
        (p.op === 'msg_foot_update' && p.patch?.status === 'done') ||
        (p.op === 'hop_update' && HOP_TERMINAL.has(p.patch?.status ?? ''))
      ) {
        finalized.add(p.id);
      }
    }
    let swept = 0;
    for (const rec of run.events) {
      if (rec.event !== 'message') continue;
      const m = rec.data as {
        id?: string;
        kind?: string;
        status?: string;
        ts?: number;
        tasks?: Array<{ id?: string; status?: string }>;
      } | null;
      if (!m || typeof m.id !== 'string' || finalized.has(m.id)) continue;
      if (m.kind === 'thinking' && m.status === 'streaming') {
        run.events.push({
          id: run.nextEventId++,
          event: 'message-patch',
          data: {
            id: m.id,
            op: 'thinking_finalize',
            duration_ms: Math.max(0, now() - (m.ts ?? now())),
          },
          ts: now(),
        });
        finalized.add(m.id);
        swept += 1;
      } else if (m.kind === 'tool_call' && m.status === 'running') {
        run.events.push({
          id: run.nextEventId++,
          event: 'message-patch',
          data: { id: m.id, op: 'tool_finalize', status: 'error' },
          ts: now(),
        });
        finalized.add(m.id);
        swept += 1;
      } else if (m.kind === 'msg_foot' && m.status === 'running') {
        run.events.push({
          id: run.nextEventId++,
          event: 'message-patch',
          data: {
            id: m.id,
            op: 'msg_foot_update',
            patch: { status: 'done' },
          },
          ts: now(),
        });
        finalized.add(m.id);
        swept += 1;
      } else if (m.kind === 'hop_card' && (m.status === 'pending' || m.status === 'running')) {
        // hop 卡收口 — 中断/崩溃时 hop 的 settle 从未发生（SQLite 侧由
        // sweepOrphanHops 补 aborted），日志侧在此对齐：补 aborted 终态
        // patch，回放不再永卡「交接中」。已 settle 的卡在上面 finalized
        // 已收集（终态 hop_update），不会被改写。
        run.events.push({
          id: run.nextEventId++,
          event: 'message-patch',
          data: {
            id: m.id,
            op: 'hop_update',
            patch: {
              status: 'aborted',
              detail: '回合中断，交接未结算',
              duration_ms: Math.max(0, now() - (m.ts ?? now())),
            },
          },
          ts: now(),
        });
        finalized.add(m.id);
        swept += 1;
      } else if (m.kind === 'plan_card') {
        // plan 卡任务行收口 — 崩溃/中断时行级 settle（plan_update_task）从未
        // 发生，running 行回放永转圈（Loader2）。行最终状态 = message 初值 +
        // 补丁归并（rowStatus 后写覆盖）；仅收 'running' 行 → failed。已结算
        // 行（done/failed/blocked）与 pending 行不碰；合成补丁进日志后二次
        // 清扫归并出 'failed' 不再命中，幂等。
        const byTask = rowStatus.get(m.id);
        for (const t of Array.isArray(m.tasks) ? m.tasks : []) {
          if (!t || typeof t.id !== 'string') continue;
          const status = byTask?.get(t.id) ?? t.status;
          if (status !== 'running') continue;
          run.events.push({
            id: run.nextEventId++,
            event: 'message-patch',
            data: {
              id: m.id,
              op: 'plan_update_task',
              task_id: t.id,
              patch: { status: 'failed', note: '回合中断，任务未结算' },
            },
            ts: now(),
          });
          swept += 1;
        }
      }
    }
    return swept;
  };

  /** Best-effort persist; never throws into the hot path. */
  const save = (run: Run): void => {
    if (!persist) return;
    try {
      persist.save(snapshot(run));
    } catch {
      /* disk is a recovery log, not a correctness dependency */
    }
  };

  const create = (id: string): Run => {
    const ts = now();
    const run: Run = {
      id,
      status: 'running',
      events: [],
      nextEventId: 1,
      clients: new Set(),
      abort: new AbortController(),
      started: false,
      waiters: new Set(),
      exitCode: null,
      signal: null,
      createdAt: ts,
      updatedAt: ts,
    };
    runs.set(id, run);
    return run;
  };

  /**
   * Reconstruct a Run from a persisted snapshot, applying the crash-recovery
   * correction: a snapshot that was still `running` when the process died has no
   * live producer, so we append a synthetic run-level `error` terminal frame (so
   * a reconnecting view replays the full as-of-crash timeline then STOPS cleanly
   * instead of hanging / reconnect-looping) and mark it `canceled`.
   * `started: true` ensures claimStart() never re-runs it. Returns the run plus
   * whether it was interrupted. Shared by startup hydration AND lazy hydrateOne.
   */
  const reconstructFromSnapshot = (snap: RunSnapshot): { run: Run; interrupted: boolean } => {
    const run: Run = {
      id: snap.id,
      status: snap.status,
      events: Array.isArray(snap.events) ? snap.events.slice() : [],
      nextEventId: typeof snap.nextEventId === 'number' ? snap.nextEventId : (snap.events?.length ?? 0) + 1,
      clients: new Set(),
      abort: new AbortController(),
      started: true,
      waiters: new Set(),
      exitCode: snap.exitCode ?? null,
      signal: snap.signal ?? null,
      createdAt: snap.createdAt ?? now(),
      updatedAt: snap.updatedAt ?? now(),
    };
    let interrupted = false;
    if (!TERMINAL.has(run.status)) {
      // 崩溃恢复清扫 — 先把日志里 streaming/running 的过程卡收口（补终态
      // patch 进日志），再追加 run 级 error 终结帧 + canceled。这样重放
      // timeline 时过程卡先落定，底部再出现「会话进程已重启」错误帧。
      sweepOpenProcessCards(run);
      run.events.push({
        id: run.nextEventId++,
        event: 'error',
        data: {
          session_id: run.id,
          code: 'server',
          message: '会话进程已重启，本次运行已中断（历史已保留）',
          interrupted_by_restart: true,
        },
        ts: now(),
      });
      run.status = 'canceled';
      interrupted = true;
    }
    return { run, interrupted };
  };

  // O2 hydration — restore all persisted runs at startup.
  if (persist) {
    let hydrated = 0;
    let interrupted = 0;
    for (const snap of persist.loadAllSync()) {
      if (!snap || typeof snap.id !== 'string' || runs.has(snap.id)) continue;
      const { run, interrupted: wasInterrupted } = reconstructFromSnapshot(snap);
      if (wasInterrupted) interrupted += 1;
      runs.set(run.id, run);
      save(run); // re-persist the corrected (terminal) snapshot
      hydrated += 1;
    }
    if (hydrated > 0) {
      // eslint-disable-next-line no-console
      console.log(`[run-event-bus] hydrated ${hydrated} run(s) from disk (${interrupted} interrupted → canceled)`);
    }
  }

  // O4 — lazy single-run recovery from disk. Used by /stream when a session is
  // gone and the run isn't in memory: pull its snapshot back so we can replay
  // the timeline read-only instead of hard-404ing.
  const hydrateOne = (id: string): Run | undefined => {
    const existing = runs.get(id);
    if (existing) return existing;
    if (!persist || typeof persist.loadOneSync !== 'function') return undefined;
    let snap: RunSnapshot | undefined;
    try {
      snap = persist.loadOneSync(id);
    } catch {
      return undefined; // disk is best-effort; treat a read failure as "absent"
    }
    if (!snap || typeof snap.id !== 'string' || snap.id !== id) return undefined;
    const { run } = reconstructFromSnapshot(snap);
    runs.set(run.id, run);
    save(run); // persist the corrected (terminal) snapshot, matching startup
    return run;
  };

  const ensure = (id: string): Run => runs.get(id) ?? create(id);
  const get = (id: string): Run | undefined => runs.get(id);

  const claimStart = (id: string): boolean => {
    const run = ensure(id);
    if (run.started) return false;
    run.started = true;
    return true;
  };

  const emit = (
    id: string,
    event: string,
    data: unknown,
  ): RunEventRecord | undefined => {
    const run = runs.get(id);
    if (!run) return undefined;
    const rec: RunEventRecord = { id: run.nextEventId++, event, data, ts: now() };
    run.events.push(rec);
    if (run.events.length > maxEvents) {
      // 2026-08-15 刷新恢复回归修复 — 环形缓冲爆满时优先驱逐可再生的
      // status_line 刷屏帧，而不是无脑丢最旧事件。status_line 是 singleton
      // 语义（前端只渲染最新一条，见 timeline-projector bumpStatusLine），
      // 每个 text chunk 都会重发一条全量 message，长 run（1m25s/688 chunk）
      // 能占掉 1/3 缓冲，把最早的 user_turn / thinking / assistant_text
      // 内容帧挤出，导致刷新重放后时间线只剩 msg_foot 一条。
      // 驱逐时保留最新一条 status_line（终态底部栏重放要用它）。
      let excess = run.events.length - maxEvents;
      const isExpendable = (r: RunEventRecord): boolean =>
        r.event === 'message' &&
        (r.data as { kind?: string } | null | undefined)?.kind === 'status_line';
      const expendableIdx: number[] = [];
      for (let i = 0; i < run.events.length; i++) {
        if (isExpendable(run.events[i]!)) expendableIdx.push(i);
      }
      expendableIdx.pop(); // 最新一条不驱逐
      const drop = new Set(expendableIdx.slice(0, excess));
      if (drop.size > 0) {
        run.events = run.events.filter((_, i) => !drop.has(i));
        excess -= drop.size;
      }
      // 没有（或不够）可驱逐的刷屏帧 → 退回原策略：丢最旧。
      if (excess > 0) run.events.splice(0, excess);
    }
    run.updatedAt = rec.ts;
    for (const sink of run.clients) {
      try {
        sink.send(rec);
      } catch {
        // A dead sink shouldn't poison the fan-out; the route's req-close
        // handler will detach it shortly.
      }
    }
    save(run); // O2 — debounced disk snapshot so the timeline survives restart
    return rec;
  };

  const attach = (id: string, after: number, sink: RunEventSink): (() => void) => {
    const run = ensure(id);
    const cursor = Number.isFinite(after) ? after : 0;
    // Replay everything the client hasn't seen yet.
    for (const rec of run.events) {
      if (rec.id > cursor) {
        try {
          sink.send(rec);
        } catch {
          /* sink died mid-replay — give up on it */
          return () => {};
        }
      }
    }
    // Already finished: nothing live to wait for.
    if (TERMINAL.has(run.status)) {
      try {
        sink.end();
      } catch {
        /* ignore */
      }
      return () => {};
    }
    run.clients.add(sink);
    return () => {
      run.clients.delete(sink);
    };
  };

  const scheduleCleanup = (run: Run): void => {
    schedule(() => {
      const cur = runs.get(run.id);
      if (cur && TERMINAL.has(cur.status)) {
        runs.delete(run.id);
        // 后端合一·阶段0（2026-06-12）— 不再删除磁盘 snapshot。
        // 终态 run 的事件日志=会话历史的一部分（B2 群 timeline 经
        // run_index→getRunReplay→hydrateOne 重放过程卡）；O2 时代把它当
        // "中断恢复用的临时账"到期即焚，正是「重启后历史过程卡消失」的
        // 第二半根因（第一半=sessionStore TTL,已由 run_index 修复）。
        // 显式重置语义（retry/reset 的 persist.remove）不受影响。
      }
    }, ttlMs);
  };

  const finish = (id: string, status: Exclude<RunStatus, 'running'>): void => {
    const run = runs.get(id);
    if (!run || TERMINAL.has(run.status)) return;
    // 终态清扫 — 收口日志里残留的 streaming/running 过程卡（正常路径生产者
    // 已自行 finalize，本扫描为 no-op；中断/异常路径在此兜底）。
    sweepOpenProcessCards(run);
    run.status = status;
    run.updatedAt = now();
    for (const sink of run.clients) {
      try {
        sink.end();
      } catch {
        /* ignore */
      }
    }
    run.clients.clear();
    // Resolve everyone awaiting this run's terminal status (single fire each).
    for (const waiter of run.waiters) waiter(status);
    run.waiters.clear();
    save(run); // O2 — persist the terminal snapshot so a restart serves it read-only
    scheduleCleanup(run);
  };

  const cancel = (id: string): void => {
    const run = runs.get(id);
    if (!run || TERMINAL.has(run.status)) return;
    if (!run.abort.signal.aborted) run.abort.abort();
    finish(id, 'canceled');
  };

  const setExit = (
    id: string,
    code: number | null,
    signal: string | null,
  ): void => {
    const run = runs.get(id);
    if (!run) return;
    run.exitCode = code;
    run.signal = signal;
    run.updatedAt = now();
    save(run);
  };

  const shutdownActive = async (
    opts: { graceMs?: number } = {},
  ): Promise<void> => {
    const graceMs = opts.graceMs ?? 3000;
    const active = Array.from(runs.values()).filter(
      (run) => !TERMINAL.has(run.status),
    );
    for (const run of active) {
      // Reuse cancel(): aborts the signal (cli spawner does SIGTERM→SIGKILL)
      // and finishes as 'canceled' via the existing logic. No duplication.
      cancel(run.id);
    }
    if (active.length === 0 || graceMs <= 0) return;
    // Give downstream child kills a moment to settle before the process exits.
    await new Promise<void>((resolve) => {
      const t = setTimeout(resolve, graceMs);
      (t as { unref?: () => void }).unref?.();
    });
  };

  const wait = (id: string): Promise<RunStatus> => {
    const run = runs.get(id);
    // Absent (never created / already cleaned up): nothing to await. 'canceled'
    // is the safe terminal answer for "no such live run".
    if (!run) return Promise.resolve('canceled');
    if (TERMINAL.has(run.status)) return Promise.resolve(run.status);
    return new Promise<RunStatus>((resolve) => {
      run.waiters.add(resolve);
    });
  };

  const reset = (id: string): void => {
    const run = runs.get(id);
    if (!run) return;
    if (!run.abort.signal.aborted) run.abort.abort();
    for (const sink of run.clients) {
      try {
        sink.end();
      } catch {
        /* ignore */
      }
    }
    run.clients.clear();
    // Don't strand wait() callers when the run is dropped mid-flight. If it was
    // already terminal, resolve with that status; otherwise treat the drop as a
    // cancellation so awaiters unblock instead of hanging forever.
    const resolvedStatus: RunStatus = TERMINAL.has(run.status)
      ? run.status
      : 'canceled';
    for (const waiter of run.waiters) waiter(resolvedStatus);
    run.waiters.clear();
    runs.delete(id);
    persist?.remove(id); // O2 — drop the snapshot so the next claimStart re-runs clean
  };

  return {
    ensure,
    get,
    hydrateOne,
    claimStart,
    emit,
    attach,
    finish,
    wait,
    cancel,
    setExit,
    shutdownActive,
    reset,
    isTerminal: (status) => TERMINAL.has(status),
    size: () => runs.size,
  };
}
