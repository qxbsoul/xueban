/**
 * gateway-run.ts — B3 网关过程事件 (unified-conversation-core-plan-2026-06-11 §B3).
 *
 * "聊着聊着 agent 开始干活，过程卡就地点亮"的后端地基：聊天网关
 * (groups-chat.ts runFanout / runTeamDagFanout) 每轮 fan-out 在这里开一条
 * "gateway run"，把执行期过程（thinking 占位、流式片段、错误、msg_foot 状态行）
 * 投影成 TimelineMessage，经 run 体系管线（run-event-bus 'message' /
 * 'message-patch' 事件）写进 run 事件日志。
 *
 * 架构决策（已定）：过程消息 **不** 写进 Python 群消息（MessageItem 只有
 * user/agent/system 三种 kind，不动它）。复用 run 管线后，B2 的
 * GET /api/groups/:id/timeline 已会归并互链 conversation 下所有 run 的事件，
 * 前端灰度 timeline 零改动即可回放这些过程卡（实时推送是 F5 的活）。
 *
 * id 规范：
 *   - TimelineMessage.id = `gw_<sessionId>_<n>`（nextId() 单调，本轮内唯一）
 *   - turn_id = `gwturn_<sessionId>`（同一轮 fan-out 共享一个 turn）
 *
 * 失败语义：startGatewayRun 任何一步失败（sqlite 不可用等）→ 返回 null，
 * 聊天回复主链路照常跑——过程事件是 additive 增强，绝不反向破坏聊天。
 */

import { createConversation, getConversationByGroup } from '../storage/conversations';
import { getOrCreateProject } from '../storage/projects';
import { registerGatewayRun } from '../routes/run-sessions';
import type { MessagePatch, TimelineMessage } from './contracts';

/** goal 预览长度：'[chat] ' + 首条用户消息前 40 字。 */
const GOAL_PREVIEW_LEN = 40;

/** One chat fan-out round's process-event channel into the run pipeline. */
export interface GatewayRun {
  sessionId: string;
  conversationId: string;
  /** 本轮 fan-out 的统一 turn id（所有过程消息共享）。 */
  turnId: string;
  /** 单调消息 id 工厂：gw_<sessionId>_1, gw_<sessionId>_2, … */
  nextId(): string;
  /** Emit 一条完整 TimelineMessage（'message' 事件）。 */
  emitMessage(msg: TimelineMessage): void;
  /** Emit 一条增量 patch（'message-patch' 事件）。 */
  emitPatch(patch: MessagePatch): void;
  /** 终结本轮 run（幂等）。默认 'succeeded'；用户插话取消上一轮 → 'canceled'。 */
  finish(status?: 'succeeded' | 'failed' | 'canceled'): void;
}

/**
 * 为一个群的一轮 fan-out 开 gateway run：
 *   1. 解析该群的互链 conversation（B1 getConversationByGroup）；没有则在
 *      default project 下创建并写入 group_id（与 POST /api/run-sessions 的
 *      group_id 分支同款语义——群聊里发任务天然归属同一条会话）。
 *   2. 在 run-sessions sessionStore 注册轻量 session
 *      （goal='[chat] <首条用户消息前40字>'，skill_name='chat-gateway'）。
 *   3. 返回 emit/finish 包装 + id 工厂。
 *
 * 返回 null = 注册失败（聊天主链路不受影响，调用方所有 gw 操作需 `gw?.`）。
 */
export function startGatewayRun(groupId: string, userText?: string): GatewayRun | null {
  try {
    let conv = getConversationByGroup(groupId);
    if (!conv) {
      const project = getOrCreateProject('default', 'Default Project');
      conv = createConversation(project.project_id, undefined, groupId);
    }
    const preview = (userText ?? '').replace(/\s+/g, ' ').trim().slice(0, GOAL_PREVIEW_LEN);
    const handle = registerGatewayRun({
      goal: `[chat] ${preview}`,
      conversation_id: conv.conversation_id,
    });
    const sessionId = handle.session_id;
    let n = 0;
    return {
      sessionId,
      conversationId: conv.conversation_id,
      turnId: `gwturn_${sessionId}`,
      nextId: () => `gw_${sessionId}_${++n}`,
      emitMessage: (msg) => handle.emit('message', msg),
      emitPatch: (patch) => handle.emit('message-patch', patch),
      finish: (status = 'succeeded') => handle.finish(status),
    };
  } catch (e) {
    // 过程事件失败绝不阻断聊天回复主链路。
    // eslint-disable-next-line no-console
    console.warn('[gateway-run] startGatewayRun failed (chat continues without process events):', e);
    return null;
  }
}
