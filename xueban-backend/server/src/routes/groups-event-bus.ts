/**
 * groups-event-bus.ts — 后端合一·阶段1b（2026-06-12）：Node 侧群 SSE 内存总线。
 *
 * 对齐 Python xueban/api/_group_events.py 的 per-group in-process pub/sub。
 * SF_GROUPS_BACKEND==='node' 时，groups-node.ts 的 GET /:id/events 订阅本总线；
 * groups-chat.ts 网关在 node 模式下把 agent 回复 / system 通知 publish 到这里，
 * 浏览器即可实时看到（取代 Python SSE bus）。
 *
 * 事件形状：{ id, type: 'agent.message' | 'system.notice' | 'agent.typing', data: {...} }。
 * SSE 端把 id → `id:` 行、type → `event:` 行、data → `data:` 行。
 *
 * 设计同 Python：
 *   - 仅进程内（多 worker 不跨进程；单进程 Node 网关无此问题）。
 *   - best-effort：listener 抛错被吞，不阻塞 publisher。
 *   - 自清理：最后一个 listener 退订后删除该 group 的 **订阅** bucket。
 *
 * 2026-11-07 — 断线补帧兜底（对齐 Python _group_events 同日改动）：
 *   - publish 给每条事件发**组内单调 id**（从 0 起，进程重启归零），并落入
 *     有界环形缓冲（默认 100 条）；**无订阅者不再丢帧**（断线窗口的事件可补）。
 *   - `agent.typing` 等瞬态提示帧不入缓冲（补发陈旧 typing 无意义），但仍占 id，
 *     保证 Last-Event-ID 游标单调。
 *   - getGroupEventsAfter(groupId, cursor) 返回应补发的帧：
 *     cursor 之后的全部缓冲帧；cursor 缺省（null）→ 全量补发（客户端按消息
 *     id 幂等去重，重复无害）；cursor 大于当前水位（进程重启后旧游标）→ 同全量。
 *   - 缓冲在退订后**保留**（这正是断线重连的恢复源），只按容量驱逐。
 */

export interface GroupEvent {
  /** 组内单调事件序号（publish 时分配；客户端 Last-Event-ID 游标用）。 */
  id: number;
  type: string;
  data: unknown;
}

type Listener = (event: GroupEvent) => void;

const subscribers = new Map<string, Set<Listener>>();

// ── 断线补帧：组内 seq + 环形缓冲（2026-11-07）────────────────────────────────
/** 每组保留最近多少条可回放事件（超出驱逐最旧）。 */
export const REPLAY_BUFFER_MAX = 100;
/** 瞬态帧：占 id 但不入缓冲（补发陈旧 typing 无意义）。 */
export const REPLAY_SKIP_TYPES = new Set(['agent.typing']);
/** 兜底：极端群数上限，超出按「最久未发布」整组驱逐，防内存无界。 */
const MAX_TRACKED_GROUPS = 200;

const seqCounters = new Map<string, number>();
const replayBuffers = new Map<string, GroupEvent[]>();

function nextSeq(groupId: string): number {
  const seq = seqCounters.get(groupId) ?? 0;
  seqCounters.set(groupId, seq + 1);
  return seq;
}

function evictOldestGroupIfNeeded(): void {
  if (replayBuffers.size <= MAX_TRACKED_GROUPS) return;
  // Map 迭代序 = 插入序；新组 append 到尾部，头部即最久未见。
  const oldest = replayBuffers.keys().next();
  if (!oldest.done) replayBuffers.delete(oldest.value);
}

/** 订阅某群事件。 */
export function subscribe(groupId: string, fn: Listener): void {
  let set = subscribers.get(groupId);
  if (!set) {
    set = new Set();
    subscribers.set(groupId, set);
  }
  set.add(fn);
}

/** 退订；订阅 bucket 空时删除（防止无界增长）。**缓冲不删**——断线重连要靠它补帧。 */
export function unsubscribe(groupId: string, fn: Listener): void {
  const set = subscribers.get(groupId);
  if (!set) return;
  set.delete(fn);
  if (set.size === 0) subscribers.delete(groupId);
}

/**
 * 向某群所有订阅者推送一个事件。listener 抛错被吞。
 * 返回分配到的组内单调 id。事件以副本发出（附 id），不改调用方对象。
 *
 * 2026-11-07 起：即使无订阅者也分配 id 并入缓冲（瞬态帧除外），供断线补帧。
 */
export function publishGroupEvent(groupId: string, event: Omit<GroupEvent, 'id'>): number {
  const id = nextSeq(groupId);
  const payload: GroupEvent = { ...event, id };

  if (!REPLAY_SKIP_TYPES.has(event.type)) {
    let buf = replayBuffers.get(groupId);
    if (!buf) {
      evictOldestGroupIfNeeded();
      buf = [];
      replayBuffers.set(groupId, buf);
    }
    buf.push(payload);
    if (buf.length > REPLAY_BUFFER_MAX) buf.splice(0, buf.length - REPLAY_BUFFER_MAX);
  }

  const set = subscribers.get(groupId);
  if (set) {
    for (const fn of [...set]) {
      try {
        fn(payload);
      } catch {
        /* best-effort — 一个坏 listener 不能阻塞其他订阅者 */
      }
    }
  }
  return id;
}

/**
 * 断线重连补帧集：缓冲中 id > cursor 的全部事件（按 id 升序）。
 * - cursor = null（未带游标的新连接）→ 全量补发；客户端按消息 id 幂等去重。
 * - cursor > 当前水位（进程重启后 seq 归零，旧游标越界）→ 视同无游标全量补发。
 * - 缓冲已驱逐的部分补不回（返回仍存在的帧；宁可缺口不重复）。
 */
export function getGroupEventsAfter(groupId: string, cursor: number | null): GroupEvent[] {
  const buf = replayBuffers.get(groupId) ?? [];
  const latest = (seqCounters.get(groupId) ?? 0) - 1;
  const effective = cursor !== null && cursor <= latest ? cursor : -1;
  return buf.filter((e) => e.id > effective);
}

/** 当前水位（最后分配的 id；无事件为 -1）。测试用。 */
export function latestSeq(groupId: string): number {
  return (seqCounters.get(groupId) ?? 0) - 1;
}

/** 订阅者数（测试用）。 */
export function subscriberCount(groupId: string): number {
  return subscribers.get(groupId)?.size ?? 0;
}

/** 清空全部总线状态（测试隔离用）。 */
export function resetGroupEventBusForTests(): void {
  subscribers.clear();
  seqCounters.clear();
  replayBuffers.clear();
}
