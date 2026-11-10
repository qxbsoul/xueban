/**
 * hop-broadcast.ts — hop 生命周期实时播报（hop_card 帧 + hop_update patch），
 * 2026-11-07 从 groups-chat.ts 内联包装器抽出（可直测的纯逻辑 + 注入 IO）。
 *
 * 为什么抽出（三个真实缺陷一起修）
 * ---------------------------------
 *   1. 「交接中」中间态不上屏：start 回调此前只落库不播报，聊天流 hop 卡
 *      pending 直跳终态（P0 盘点「pending→终态跳变」）。现在 start → 发
 *      hop_update {status:'running'}，NeoHopCard 显示「交接中」。
 *   2. 帧/补丁顺序竞态：open 的帧要异步解析 agent 名字才发出，settle 的
 *      patch 若先到会打在未知消息 id 上（前端 reducer 静默丢弃、回放同样
 *      丢失）。现在按 hopId 缓存帧状态：帧未发先把 patch 入队，帧发出后
 *      按序 flush —— hop_update 永远晚于其 hop_card 帧。
 *   3. 按边 key 索引串卡：conditional rework 对同一条边每轮新开 hop，旧
 *      包装器按 "from→to" 索引帧 id，后一轮 open 会覆盖前一轮，settle 打到
 *      别的卡上。现在按 hopId 索引，每轮 rework 各自成卡。
 *
 * 契约边界（additive，不改 contracts.ts）
 * ---------------------------------------
 *   - 帧 = hop_card status:'pending'（open 时刻全量帧，名字快照）；
 *   - patch = hop_update，status:'running'（交接中）或终态（settle）；
 *   - 播报 best-effort：emit 抛错静默吞掉，绝不阻塞 SQLite 落库主链路
 *     （base 的 open/start/settle 总是先调）。
 */
import type { MessagePatch, TimelineMessage } from '../contracts';
import type { HopRecorder } from './run-team-dag';

/** hop_update 的 patch 载荷（与 contracts.ts MessagePatch 同源收窄）。 */
export type HopUpdatePatch = Extract<MessagePatch, { op: 'hop_update' }>['patch'];

export interface HopBroadcasterDeps {
  /** 帧 id 生成（生产 = GatewayRun.nextId）。 */
  nextId: () => string;
  /** 回合 id，写进帧 turn_id。 */
  turnId: string;
  emitMessage: (msg: TimelineMessage) => void;
  emitPatch: (patch: MessagePatch) => void;
  /** agent 展示名解析（open 时刻快照进帧；拒绝回落 agentId）。 */
  resolveName: (agentId: string) => Promise<string>;
}

interface FrameState {
  cardId: string;
  openedAt: number;
  ready: boolean;
  queue: HopUpdatePatch[];
}

/**
 * 包一层 HopRecorder：SQLite 落库（base）照旧，播报按下列不变量收口：
 *   - 每个 hopId 一张卡；patch 只打自己的卡；
 *   - 先帧后补丁（帧未发 patch 入队，帧发出后按序 flush）；
 *   - open/start/settle 的 base 调用先于一切播报，且播报异常不冒泡。
 */
export function createHopBroadcaster(base: HopRecorder, deps: HopBroadcasterDeps): HopRecorder {
  const frames = new Map<string, FrameState>();

  const emitSafe = (fn: () => void): void => {
    try {
      fn();
    } catch {
      /* best-effort — 播报失败不影响落库主链路 */
    }
  };

  const pushPatch = (hopId: string, patch: HopUpdatePatch): void => {
    const st = frames.get(hopId);
    if (!st) return; // 非本广播器 open 的 hop（理论上不存在）→ 静默降级
    if (st.ready) {
      emitSafe(() => deps.emitPatch({ id: st.cardId, op: 'hop_update', patch }));
    } else {
      st.queue.push(patch);
    }
  };

  return {
    open: (edge) => {
      const hopId = base.open(edge);
      const st: FrameState = {
        cardId: deps.nextId(),
        openedAt: Date.now(),
        ready: false,
        queue: [],
      };
      frames.set(hopId, st);
      // 名字解析异步；期间到达的 patch 入队，帧发出后按序 flush。
      void (async () => {
        const [fromName, toName] = await Promise.all([
          deps.resolveName(edge.from).catch(() => edge.from),
          deps.resolveName(edge.to).catch(() => edge.to),
        ]);
        if (frames.get(hopId) !== st) return; // 防御：状态被替换则不再发帧
        emitSafe(() =>
          deps.emitMessage({
            id: st.cardId,
            kind: 'hop_card',
            turn_id: deps.turnId,
            ts: Date.now(),
            from_agent: edge.from,
            to_agent: edge.to,
            from_name: fromName,
            to_name: toName,
            status: 'pending',
          }),
        );
        st.ready = true;
        for (const queued of st.queue.splice(0)) {
          emitSafe(() => deps.emitPatch({ id: st.cardId, op: 'hop_update', patch: queued }));
        }
      })();
      return hopId;
    },
    start: (id) => {
      base.start(id);
      // 「交接中」中间态 — 上游开始产出交接内容（此前 pending 直跳终态）。
      pushPatch(id, { status: 'running' });
    },
    settle: (id, status, opts) => {
      base.settle(id, status, opts);
      const st = frames.get(id);
      pushPatch(id, {
        status,
        ...(opts?.outputChars !== undefined ? { output_chars: opts.outputChars } : {}),
        ...(opts?.detail ? { detail: opts.detail } : {}),
        ...(st ? { duration_ms: Date.now() - st.openedAt } : {}),
      });
    },
  };
}
