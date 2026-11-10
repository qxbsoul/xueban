/**
 * run-team-dag.ts — chat DAG 执行器编排核心(批 2 Phase 2a + 2b 加固)。
 *
 * 按 computeDagLayers 的层序跑:层间有序、同层并发(mapLimit 有界,B5)。每个下游 agent 的
 * 输入 = 用户原始消息(调用方 bake 进 callAgent)+ 上游产出,上游按 gate 过滤(deny/warn/permit)。
 * 每节点经 runNodeWithPolicy 跑:超时(B1)+ 重试(B4)+ 外部取消(B6)。signal abort → 不再起
 * 新层/新节点,剩余标 'aborted'。产出经 deps.postMessage 落库。所有 I/O 经 deps 注入。
 *
 * 2026-11-05 交接记录 + 失败传播（设计移植自 CopilotKit/OpenBot 的 hop/work_items）：
 *   - deps.hops 可选注入 — 每条前向边（A→B 交接）open→settle 全生命周期落库；
 *     不注入则零开销（纯内存路径/旧测试不变）。
 *   - 上游 failed/timeout/empty/aborted 时，下游 upstream 注入一条系统失败通知
 *     （失败传播），下游知情自适应，不再带着残缺上下文瞎跑。
 *
 * 2026-11-05 第二轮 — conditional rework 闭环（评审表「异常处理」收尾）：
 *   - conditional 边（如 QA→Dev）不再是死配置：下游节点带 conditional 入边时，
 *     跑完先经 detectRejection 零 LLM 判定（rework-detect.ts 标记约定）；
 *   - 驳回 → 给评审者口中的被驳回方（edge.condition 指定，缺省=上游）注入
 *     驳回意见触发返工，预算 = edge.max_retries（缺省 1），用尽即放行并显式记；
 *   - 每次返工一个新 hop（rework 边），全程可审计。
 *
 * 不在本阶段:中断恢复/hydrate(B2)、用户插话完整续跑(B3)。
 */
import type { TeamRunShape } from '../team-source';
import { computeDagLayers } from './dag-layers';
import { gate } from './policy-gate';
import { mapLimit } from './map-limit';
import { runNodeWithPolicy } from './node-policy';
import { detectRejection } from './rework-detect';

export interface UpstreamMsg { from: string; text: string }

/**
 * 交接记录器 — 每条 DAG 前向边（from→to 交接）一个 hop。由调用方实现（生产环境
 * 落 SQLite storage/agent-hops.ts，测试用内存 Map），执行器只按生命周期回调。
 */
export interface HopRecorder {
  /** 边开始处理时开 hop，返回 id。 */
  open: (edge: { from: string; to: string }) => string;
  /** 开 hop 后、出结果前标记 running（可选步骤）。 */
  start: (id: string) => void;
  /** 结算为终态。幂等（已结算不覆盖）。 */
  settle: (
    id: string,
    status: 'done' | 'failed' | 'timeout' | 'empty' | 'denied' | 'aborted',
    opts?: { outputChars?: number; detail?: string },
  ) => void;
}

export interface RunTeamDagDeps {
  getAgent: (agentId: string) => Promise<{ name: string; soul: string } | null>;
  callAgent: (args: { agentId: string; name: string; soul: string; upstream: UpstreamMsg[]; signal: AbortSignal }) => Promise<{ text: string; error?: string }>;
  postMessage: (msg: { content: string; sender_name: string; sender_kind: 'agent' | 'system'; agent_id?: string }) => Promise<void>;
  /** 可选：交接记录（2026-11-05）。不注入 = 行为与旧版完全一致。 */
  hops?: HopRecorder;
  /**
   * 可选：approve 边人工审批门（2026-11-05，OpenBot 审批纪律对齐）。
   * 不注入 = approve 边按 direct 处理（旧版行为，零回归）。
   * request 返回 'rejected'（含超时/外部取消）→ 该节点标 'blocked' 不执行，
   * 其出边下游按失败传播知情；'approved' → 照常执行。
   */
  approval?: {
    request: (args: {
      nodeId: string;
      nodeName: string;
      /** 待审批内容预览（上游产出首段，供审批卡片/消息展示）。 */
      preview: string;
      signal: AbortSignal;
    }) => Promise<'approved' | 'rejected'>;
  };
  /** 可选：节点被审批拒绝后回调（群聊接线用它把 plan_card 任务行标 blocked）。 */
  onNodeBlocked?: (agentId: string) => void;
}

export type AgentOutcome = 'ok' | 'error' | 'empty' | 'timeout' | 'aborted' | 'blocked';

export interface RunTeamDagResult {
  ranLayers: number;
  perAgent: Record<string, AgentOutcome>;
  deniedEdges: number;
  warnedEdges: number;
  aborted: boolean;
  /** conditional rework 实际触发的返工次数（按节点累计，含每轮回执）。 */
  reworkRuns: number;
}

export interface RunTeamDagOpts {
  /** 外部取消信号(B6)。abort → 停止起新节点,剩余标 'aborted'。 */
  signal?: AbortSignal;
  /** 同层最大并发(B5)。默认 5。 */
  maxConcurrency?: number;
  /** 单节点超时 ms(B1)。默认 120000。 */
  nodeTimeoutMs?: number;
  /** 入边无 max_retries 时的默认重试预算(B4)。默认 1。 */
  defaultMaxRetries?: number;
  /** conditional rework 全局每节点返工上限(2026-11-05)。默认 2,防坏数据无限循环。 */
  maxReworkRounds?: number;
}

export async function runTeamDagFanout(
  team: TeamRunShape,
  deps: RunTeamDagDeps,
  userPrompt: string,
  opts: RunTeamDagOpts = {},
): Promise<RunTeamDagResult> {
  const signal = opts.signal ?? new AbortController().signal;
  const maxConcurrency = opts.maxConcurrency ?? 5;
  const nodeTimeoutMs = opts.nodeTimeoutMs ?? 120_000;
  const defaultMaxRetries = opts.defaultMaxRetries ?? 1;

  const { layers } = computeDagLayers(team.members, team.edges);

  // 前向边索引:入边来源(收集上游)+ 入边 max_retries(B4 预算)。conditional/幽灵/自环排除。
  // 2026-11-05 — 每条前向边同步记下 from→to（给 hop 记录器用；hop 在层循环里 open）。
  const incoming = new Map<string, string[]>();
  const retryBudget = new Map<string, number>();
  // conditional 出边索引（2026-11-05 rework）— 评审者节点 → [{to:被评审方, max_retries}]。
  // 语义：评审者跑完 → 自己产出命中驳回标记 → 把意见带回给 to 重跑（返工）。
  const reworkOutgoing = new Map<string, Array<{ to: string; maxRetries: number }>>();
  for (const e of team.edges) {
    if (!team.members.includes(e.from) || !team.members.includes(e.to)) continue;
    if (e.from === e.to) continue;
    if (e.kind === 'conditional') {
      let list = reworkOutgoing.get(e.from);
      if (!list) { list = []; reworkOutgoing.set(e.from, list); }
      list.push({ to: e.to, maxRetries: typeof e.max_retries === 'number' ? Math.max(0, e.max_retries) : 1 });
      continue;
    }
    let list = incoming.get(e.to);
    if (!list) { list = []; incoming.set(e.to, list); }
    list.push(e.from);
    if (typeof e.max_retries === 'number') {
      retryBudget.set(e.to, Math.max(retryBudget.get(e.to) ?? 0, e.max_retries));
    }
  }

  // 上游节点终态（2026-11-05 失败传播）— 下游组 upstream 时按它决定是否注入失败通知。
  const nodeOutcome = new Map<string, AgentOutcome>();
  /** 节点 error 时的错误消息（hop detail 用，timeout/empty 有固定文案）。 */
  const errMsgOf = new Map<string, string>();

  // approve 边索引（2026-11-05）— 节点 → 是否带 approve 入边（带则跑前审批）。
  // 只有下游确有 approve 入边才需要审批；direct 入边不受影响。
  const needsApproval = new Set<string>();
  for (const e of team.edges) {
    if (e.kind === 'approve' && team.members.includes(e.from) && team.members.includes(e.to) && e.from !== e.to) {
      needsApproval.add(e.to);
    }
  }

  // hop 生命周期辅助 — hops 未注入时全部空操作，主循环零分支。
  const hops = deps.hops;
  const edgeHop = new Map<string, string>(); // "${from}→${to}" → hopId
  const hopIdFor = (from: string, to: string): string | null => {
    if (!hops) return null;
    const key = `${from}→${to}`;
    let id = edgeHop.get(key);
    if (!id) {
      id = hops.open({ from, to });
      edgeHop.set(key, id);
    }
    return id;
  };

  const outputs = new Map<string, string>();
  const perAgent: Record<string, AgentOutcome> = {};
  let deniedEdges = 0;
  let warnedEdges = 0;
  let aborted = false;
  let reworkRuns = 0;

  /**
   * 跑一个节点一次（组 upstream → runNodeWithPolicy → 落产出/终态/hop）。
   * 层循环与 rework 循环共用；返回该次执行的终态。
   */
  const runNodeOnce = async (
    agentId: string,
    extraUpstream: UpstreamMsg[],
  ): Promise<AgentOutcome> => {
    // 上游产出按 gate 过滤。
    const upstream: UpstreamMsg[] = [...extraUpstream];
    for (const from of incoming.get(agentId) ?? []) {
      const verdict = gate(team.policy_matrix, from, agentId);
      const hopId = hopIdFor(from, agentId);
      if (verdict === 'deny') {
        deniedEdges++;
        if (hops && hopId) hops.settle(hopId, 'denied', { detail: 'policy deny' });
        await deps.postMessage({ content: `[policy] ${from} → ${agentId} 被 deny 拦截,上游产出未传递。`, sender_name: 'system', sender_kind: 'system' });
        continue;
      }
      const text = outputs.get(from);
      if (verdict === 'warn') {
        warnedEdges++;
        await deps.postMessage({
          content: text
            ? `[policy] ${from} → ${agentId} warn 提醒(交到拍板/审核点),仍放行。`
            : `[policy] ${from} → ${agentId} warn 提醒;但上游无产出,无内容可传递。`,
          sender_name: 'system', sender_kind: 'system',
        });
      }
      if (text) {
        if (hops && hopId) hops.settle(hopId, 'done', { outputChars: text.length });
        upstream.push({ from, text });
      } else {
        // 2026-11-05 失败传播 — 上游没产出（失败/超时/空/中止）时下游必须知情。
        // 上游压根没跑成 → 该交接按上游终态结算 + 注入系统失败通知。
        const upOutcome = nodeOutcome.get(from);
        if (upOutcome && upOutcome !== 'ok') {
          if (hops && hopId) {
            const failDetail = upOutcome === 'error'
              ? (errMsgOf.get(from) ?? 'error')
              : upOutcome === 'blocked'
                ? `审批被拒：${from} 未获批准，交接未发生`
                : `上游 ${from} ${upOutcome}`;
            hops.settle(
              hopId,
              upOutcome === 'blocked'
                ? 'denied'
                : upOutcome === 'aborted'
                  ? 'aborted'
                  : upOutcome === 'timeout'
                    ? 'timeout'
                    : upOutcome === 'empty'
                      ? 'empty'
                      : 'failed',
              { detail: failDetail },
            );
          }
          const upstreamName = (await deps.getAgent(from))?.name ?? from;
          upstream.push({
            from,
            text: `[系统通知] 上游 ${upstreamName} 本轮${upOutcome === 'timeout' ? '超时未响应' : upOutcome === 'empty' ? '返回空回复' : upOutcome === 'aborted' ? '被中止' : upOutcome === 'blocked' ? '未通过人工审批' : '执行失败'}，其交接内容缺失。请基于可用信息继续，并在产出中说明哪些部分因上游缺失可能不完整。`,
          });
        }
      }
    }

    const agent = await deps.getAgent(agentId);
    if (!agent) {
      perAgent[agentId] = 'error';
      nodeOutcome.set(agentId, 'error');
      await deps.postMessage({ content: `[chat] Agent '${agentId}' not found.`, sender_name: 'system', sender_kind: 'system' });
      return 'error';
    }

    // ── approve 边人工审批门（2026-11-05，OpenBot「每步发生在审批之后」）────
    // 带 approve 入边的节点在执行前挂起等用户决定；不注入 approval deps 或
    // 无 approve 入边 → 直接放行（零回归）。拒绝 → 'blocked'，出边下游走
    // 既有失败传播（「审批被拒」文案）。
    if (needsApproval.has(agentId) && deps.approval) {
      const upstreamPreview = upstream
        .map((u) => `[${u.from}] ${u.text}`)
        .join('\n')
        .slice(0, 200);
      await deps.postMessage({
        content: `[approval] ${agent.name} 的执行已挂起，等待人工审批（该节点带审批入边）。`,
        sender_name: 'system', sender_kind: 'system',
      });
      const verdict = await deps.approval.request({
        nodeId: agentId,
        nodeName: agent.name,
        preview: upstreamPreview || userPrompt,
        signal,
      });
      if (verdict !== 'approved') {
        perAgent[agentId] = 'blocked';
        nodeOutcome.set(agentId, 'blocked');
        deps.onNodeBlocked?.(agentId);
        await deps.postMessage({
          content: `[approval] ${agent.name} 的执行被拒绝，本轮该节点跳过（产出不落库、下游不再等待它）。`,
          sender_name: 'system', sender_kind: 'system',
        });
        return 'blocked';
      }
      await deps.postMessage({
        content: `[approval] ${agent.name} 已获批准，开始执行。`,
        sender_name: 'system', sender_kind: 'system',
      });
    }

    const maxRetries = retryBudget.get(agentId) ?? defaultMaxRetries;
    // 该节点作为上游的各条边在此刻 open（pending→running）——层序保证下游的
    // open 一定发生在上游 settle 之后，同层并发各自 open 自己的出边互不冲突。
    if (hops) {
      for (const e of team.edges) {
        if (e.from === agentId && e.kind !== 'conditional' && team.members.includes(e.to) && e.from !== e.to) {
          const id = hopIdFor(e.from, e.to);
          if (id) hops.start(id);
        }
      }
    }
    const res = await runNodeWithPolicy(
      (attemptSignal) => deps.callAgent({ agentId, name: agent.name, soul: agent.soul, upstream, signal: attemptSignal }),
      { timeoutMs: nodeTimeoutMs, maxRetries, signal },
    );

    if (res.reason === 'aborted') { perAgent[agentId] = 'aborted'; nodeOutcome.set(agentId, 'aborted'); aborted = true; return 'aborted'; }
    if (res.reason === 'timeout') {
      perAgent[agentId] = 'timeout';
      nodeOutcome.set(agentId, 'timeout');
      await deps.postMessage({ content: `[chat] ${agent.name} 超时(${nodeTimeoutMs}ms)未响应,已放弃该节点。`, sender_name: 'system', sender_kind: 'system' });
      return 'timeout';
    }
    if (res.reason === 'error') {
      perAgent[agentId] = 'error';
      nodeOutcome.set(agentId, 'error');
      errMsgOf.set(agentId, res.error ?? '');
      await deps.postMessage({ content: `[chat] ${agent.name} 出错：${res.error}`, sender_name: 'system', sender_kind: 'system' });
      return 'error';
    }
    const reply = res.text.trim();
    if (!reply) {
      perAgent[agentId] = 'empty';
      nodeOutcome.set(agentId, 'empty');
      await deps.postMessage({ content: `[chat] ${agent.name} 返回空回复。`, sender_name: 'system', sender_kind: 'system' });
      return 'empty';
    }
    outputs.set(agentId, reply);
    perAgent[agentId] = 'ok';
    nodeOutcome.set(agentId, 'ok');
    // U0 绑角色 — 带上发送者 agent_id（agentId 即 map key，恒在作用域）。
    await deps.postMessage({ content: reply, sender_name: agent.name, sender_kind: 'agent', agent_id: agentId });
    return 'ok';
  };

  let ranLayers = 0;
  /** 每节点已用返工次数（跨层累计——同一被评审方可能被多个评审者盯）。 */
  const reworkUsed = new Map<string, number>();
  for (const layer of layers) {
    if (signal.aborted || aborted) { aborted = true; break; } // 外部已取消或节点已 abort:不再起新层
    await mapLimit(layer, maxConcurrency, async (agentId) => {
      // 快速跳过(非唯一屏障):runNodeWithPolicy 入口会再查 signal.aborted 作硬屏障。
      if (signal.aborted || aborted) { perAgent[agentId] = 'aborted'; return; }

      const outcome = await runNodeOnce(agentId, []);
      if (outcome !== 'ok') return; // 失败传播已落,hop 收尾兜底统一处理
      if (signal.aborted || aborted) return;

      // ── conditional rework（2026-11-05 第二轮）──────────────────────────
      // 本节点是评审者（带 conditional 出边）且产出命中驳回标记 → 把意见带回
      // 给被评审方（edge.to）重跑，跑完本评审者**复审**，循环直到通过或预算用尽。
      const targets = reworkOutgoing.get(agentId);
      if (!targets?.length) return;
      const reviewerName = (await deps.getAgent(agentId))?.name ?? agentId;

      for (const t of targets) {
        if (signal.aborted || aborted) break;
        const budget = Math.min(t.maxRetries, Math.max(opts.maxReworkRounds ?? 2, 0));
        if (budget <= 0) continue;

        // 闭环循环：驳回 → 返工 → 复审 → 再判定。预算上限兜底（防坏 soul 无限循环）。
        while ((reworkUsed.get(t.to) ?? 0) < budget && detectRejection(outputs.get(agentId) ?? '')) {
          if (signal.aborted || aborted) break;
          const used = reworkUsed.get(t.to) ?? 0;
          const review = outputs.get(agentId) ?? '';
          const targetAgent = await deps.getAgent(t.to);
          const targetName = targetAgent?.name ?? t.to;
          const hopRecorder = hops; // 局部常量 — TS 收窄（闭包内 hops 可能 undefined）
          const hopId = hopRecorder ? hopRecorder.open({ from: agentId, to: t.to }) : null;
          if (hopId && hopRecorder) hopRecorder.start(hopId);
          reworkUsed.set(t.to, used + 1);
          reworkRuns++;
        await deps.postMessage({
          content: `[rework·返工] ${reviewerName} 驳回了 ${targetName} 的产出（第 ${used + 1}/${budget} 轮返工），驳回意见已带回。`,
          sender_name: 'system', sender_kind: 'system',
        });
          const reworkOutcome = await runNodeOnce(t.to, [{
            from: agentId,
            text: `[驳回意见·第 ${used + 1} 轮] ${review}`,
          }]);
          if (hopId && hopRecorder) {
            hopRecorder.settle(hopId, reworkOutcome === 'ok' ? 'done' : reworkOutcome === 'timeout' ? 'timeout' : reworkOutcome === 'aborted' ? 'aborted' : reworkOutcome === 'empty' ? 'empty' : 'failed', {
              detail: `rework round ${used + 1}`,
              outputChars: reworkOutcome === 'ok' ? (outputs.get(t.to) ?? '').length : 0,
            });
          }
          if (reworkOutcome !== 'ok') break; // 返工也失败 → 不再循环（失败传播已落）
          if (signal.aborted || aborted) break;
          // 复审：评审者重跑一次。产出覆盖 outputs（postMessage 群里可见）。
          const reReview = await runNodeOnce(agentId, []);
          if (reReview !== 'ok') break; // 复审本身挂了 → 放行现版，失败传播已落
        }

        // 预算用尽仍被驳回 → 显式放行并记录（OpenBot 审计纪律：不悬空、有交代）。
        if ((reworkUsed.get(t.to) ?? 0) >= budget && detectRejection(outputs.get(agentId) ?? '')) {
          await deps.postMessage({
            content: `[rework] ${reviewerName} 的驳回已达 ${t.to} 的返工预算上限（${budget} 轮），放行现版产出并记录在案。`,
            sender_name: 'system', sender_kind: 'system',
          });
        }
      }
    });
    ranLayers++;
  }

  // 未被执行到的节点(abort break 跳过的层)统一标 aborted。
  if (aborted) {
    for (const id of team.members) {
      if (!(id in perAgent)) perAgent[id] = 'aborted';
    }
  }

  // 收尾兜底 — 计划过的每条前向边都要有终态（OpenBot「work_items 不留悬空」的
  // 审计纪律）。没被显式结算的（下游没跑成/压根没轮到，如预先 abort）在此补
  // open + 结算，保证交接记录完整可审计。
  if (hops) {
    for (const e of team.edges) {
      if (e.kind === 'conditional' || !team.members.includes(e.from) || !team.members.includes(e.to) || e.from === e.to) continue;
      const hopId = hopIdFor(e.from, e.to);
      const fromOutcome = nodeOutcome.get(e.from) ?? 'aborted';
      const toOutcome = nodeOutcome.get(e.to);
      if (toOutcome !== 'ok') {
        // 上游被审批拒绝 → 该交接结算 denied（与 policy deny 同语义，审计可辨）。
        if (fromOutcome === 'blocked') {
          hops.settle(hopId!, 'denied', { detail: `审批被拒：${e.from} 未获批准，交接未发生` });
          continue;
        }
        const failDetail = fromOutcome === 'error'
          ? (errMsgOf.get(e.from) ?? 'error')
          : `回合结束兜底：${e.from}=${fromOutcome}, ${e.to}=${toOutcome ?? 'not_reached'}`;
        hops.settle(hopId!, fromOutcome === 'ok' ? 'aborted' : fromOutcome === 'timeout' ? 'timeout' : fromOutcome === 'empty' ? 'empty' : 'failed', {
          detail: failDetail,
        });
      }
    }
  }

  return { ranLayers, perAgent, deniedEdges, warnedEdges, aborted, reworkRuns };
}
