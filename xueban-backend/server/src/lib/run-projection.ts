/**
 * run-projection.ts — L6（GAP.md:13）Node daemon run 的 graph / task-tree 投影。
 *
 * 背景：chat 主路径（Node daemon，run-sessions.ts 的 runBus）启动的 run 只存在
 * Node 侧事件日志里，Python RuntimeService 无记录 → GET /api/runs/:id/graph 与
 * /task-tree 经 proxyFallback 打到 Python 只能 404，前端「过程」tab 显示错误条。
 *
 * 本模块把 daemon run 的事件日志（'message' / 'message-patch' 时间线帧）折叠成
 * 与 Python 侧一致的投影形状（xueban/runtime/contracts.py 的 RunGraph /
 * TaskTreeProjection，字段逐一对齐，前端 src/api/runs.ts 的类型零改动消费）：
 *
 *   - graph     → plan_card 任务 + step_panel 步骤 + tool_call 工具调用，按
 *                 出现顺序串 control_flow 边（chat run 无 workflow DAG，执行
 *                 轨迹本身就是图）。
 *   - task-tree → run(根) → task(plan_card 任务) → step(tool_call / step_panel
 *                 行) 的缩进树，边用 belongs_to_run / belongs_to_task。
 *
 * 纯函数模块：不碰 HTTP / fs / process.cwd()，输入只有 runId + 事件日志快照
 * （routes/runs.ts 经 run-sessions.getRunReplay 取得后传入）。
 */

import type { RunEventRecord, RunStatus } from './run-event-bus';
import type {
  MessagePatch,
  PlanTaskRow,
  StepRow,
  TimelineMessage,
} from './contracts';

/** getRunReplay() 的返回形状（run-sessions.ts），结构化兼容。 */
export interface DaemonRunReplay {
  status: RunStatus;
  events: RunEventRecord[];
}

// ── 投影形状（镜像 xueban/runtime/contracts.py，序列化后与 Python 一致）────

export interface ProjectionScope {
  workflow_id?: string | null;
  run_id?: string | null;
  task_id?: string | null;
  artifact_id?: string | null;
  checkpoint_id?: string | null;
}

export type RunGraphNodeStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'cancelled'
  | 'not_started';

export interface DaemonRunGraphNode {
  id: string;
  label: string;
  kind: string;
  type: string;
  entity_type: 'workflow_node';
  status: RunGraphNodeStatus;
  step_id?: string;
  index?: number;
  entrypoint: boolean;
  refs: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface DaemonGraphEdge {
  from_id: string;
  to_id: string;
  type: string;
  edge_type?: string;
  condition?: string;
  intervention: boolean;
  metadata: Record<string, unknown>;
}

/** RunGraph（Python contracts.py:787）。 */
export interface DaemonRunGraph {
  projection_kind: 'run_graph';
  version: 'v1';
  run_id: string;
  workflow_id: string;
  status: string;
  entrypoint: string;
  scope: ProjectionScope;
  summary: Record<string, unknown>;
  filters: Record<string, unknown>;
  nodes: DaemonRunGraphNode[];
  edges: DaemonGraphEdge[];
  metadata: Record<string, unknown>;
}

export interface DaemonProjectionNode {
  id: string;
  entity_type: 'run' | 'task' | 'step';
  label: string;
  status?: string;
  parent_id?: string | null;
  refs: Record<string, unknown>;
  timestamps: {
    started_at?: string | null;
    ended_at?: string | null;
    created_at?: string | null;
  };
  metadata: Record<string, unknown>;
}

export interface DaemonProjectionEdge {
  id: string;
  edge_type: 'belongs_to_run' | 'belongs_to_task' | 'control_flow' | 'delegation';
  from_id: string;
  to_id: string;
  intervention: boolean;
  condition?: string;
  metadata: Record<string, unknown>;
}

/** TaskTreeProjection（Python contracts.py:690，ProjectionGraph 子类）。 */
export interface DaemonTaskTree {
  projection_kind: 'task_tree';
  version: 'v1';
  scope: ProjectionScope;
  summary: Record<string, unknown>;
  filters: Record<string, unknown>;
  nodes: DaemonProjectionNode[];
  edges: DaemonProjectionEdge[];
  metadata: Record<string, unknown>;
}

/** daemon chat run 无 workflow 定义，workflow_id 用固定合成值并在 metadata 标注。 */
export const DAEMON_WORKFLOW_ID = 'node-daemon-chat';

// ── 事件日志折叠：'message' 全量帧 + 'message-patch' 增量帧 → 终态消息表 ──────

function isTimelineMessage(data: unknown): data is TimelineMessage {
  return (
    typeof data === 'object' &&
    data !== null &&
    typeof (data as { id?: unknown }).id === 'string' &&
    typeof (data as { kind?: unknown }).kind === 'string'
  );
}

/** 只应用影响投影的 patch op；文本/思考流等展示型 op 与投影无关，忽略。 */
function applyPatch(
  messages: Map<string, TimelineMessage>,
  patch: MessagePatch,
): void {
  const msg = messages.get(patch.id);
  if (!msg) return;
  switch (patch.op) {
    case 'tool_finalize':
      if (msg.kind === 'tool_call') {
        msg.status = patch.status;
        if (patch.duration_ms !== undefined) msg.duration_ms = patch.duration_ms;
        if (patch.result_summary !== undefined) msg.result_summary = patch.result_summary;
      }
      break;
    case 'plan_update_task':
      if (msg.kind === 'plan_card') {
        const task = msg.tasks.find((t) => t.id === patch.task_id);
        if (task) Object.assign(task, patch.patch);
      }
      break;
    case 'append_step':
      if (msg.kind === 'step_panel') {
        msg.steps.push(patch.step);
        msg.total_steps = msg.steps.length;
      }
      break;
    case 'update_step':
      if (msg.kind === 'step_panel' && msg.steps[patch.index]) {
        Object.assign(msg.steps[patch.index], patch.patch);
      }
      break;
    case 'append_substep':
      if (msg.kind === 'step_panel' && msg.steps[patch.step_index]) {
        const step = msg.steps[patch.step_index]!;
        step.substeps = step.substeps ?? [];
        step.substeps.push(patch.sub);
      }
      break;
    case 'update_substep':
      if (msg.kind === 'step_panel') {
        const sub = msg.steps[patch.step_index]?.substeps?.[patch.sub_index];
        if (sub) Object.assign(sub, patch.patch);
      }
      break;
    default:
      break;
  }
}

/** 折叠事件日志 → id 有序的终态 TimelineMessage 表。 */
export function foldTimelineEvents(
  events: RunEventRecord[],
): Map<string, TimelineMessage> {
  const messages = new Map<string, TimelineMessage>();
  for (const rec of events) {
    if (rec.event === 'message' && isTimelineMessage(rec.data)) {
      // status_line 等 singleton 种类会重发同 kind 新 id——按 id 存即可，不去重。
      messages.set(rec.data.id, rec.data);
    } else if (rec.event === 'message-patch') {
      applyPatch(messages, rec.data as MessagePatch);
    }
  }
  return messages;
}

// ── 状态映射：daemon 词表 → Python 投影词表 ──────────────────────────────────

function mapRunStatus(status: RunStatus): string {
  // run-event-bus 用 'canceled'（单 l），Python 投影用 'cancelled'（双 l）。
  return status === 'canceled' ? 'cancelled' : status;
}

function mapPlanTaskStatus(status: PlanTaskRow['status']): RunGraphNodeStatus {
  switch (status) {
    case 'running':
      return 'running';
    case 'done':
      return 'succeeded';
    case 'failed':
      return 'failed';
    // blocked = 等前置，语义上仍是未启动。
    case 'blocked':
    case 'pending':
    default:
      return 'pending';
  }
}

function mapStepRowStatus(status: StepRow['status']): RunGraphNodeStatus {
  switch (status) {
    case 'done':
      return 'succeeded';
    case 'running':
      return 'running';
    case 'pending':
    default:
      return 'pending';
  }
}

/**
 * tool_call 状态：running/ok/error。run 已到终态但仍挂着 running 的工具
 * （进程中断、缺 tool_result），落定到 run 的终态，避免永远转圈。
 */
function mapToolStatus(
  status: 'running' | 'ok' | 'error' | undefined,
  runStatus: RunStatus,
): RunGraphNodeStatus {
  if (status === 'ok') return 'succeeded';
  if (status === 'error') return 'failed';
  if (runStatus === 'running') return 'running';
  return mapRunStatus(runStatus) as RunGraphNodeStatus;
}

function isoOrNull(ts: number | undefined): string | null {
  return typeof ts === 'number' && Number.isFinite(ts)
    ? new Date(ts).toISOString()
    : null;
}

/** 取该 kind 的最后一条消息（每个 turn 重新开一条，最后一条是最新状态）。 */
function lastOfKind<K extends TimelineMessage['kind']>(
  messages: Map<string, TimelineMessage>,
  kind: K,
): Extract<TimelineMessage, { kind: K }> | undefined {
  let last: Extract<TimelineMessage, { kind: K }> | undefined;
  for (const m of messages.values()) {
    if (m.kind === kind) last = m as Extract<TimelineMessage, { kind: K }>;
  }
  return last;
}

// ── graph 投影 ────────────────────────────────────────────────────────────────

export function buildDaemonRunGraph(
  runId: string,
  replay: DaemonRunReplay,
): DaemonRunGraph {
  const messages = foldTimelineEvents(replay.events);
  const nodes: DaemonRunGraphNode[] = [];

  const plan = lastOfKind(messages, 'plan_card');
  if (plan) {
    plan.tasks.forEach((task, i) => {
      nodes.push({
        id: task.id,
        label: task.title,
        kind: 'task',
        type: 'plan_task',
        entity_type: 'workflow_node',
        status: mapPlanTaskStatus(task.status),
        index: i,
        entrypoint: i === 0,
        refs: {
          run_id: runId,
          workflow_id: DAEMON_WORKFLOW_ID,
          task_id: task.id,
          agent_id: task.agent_id ?? null,
        },
        metadata: {
          assignee: task.assignee ?? null,
          batch: task.batch ?? null,
          elapsed_ms: task.elapsed_ms ?? null,
          note: task.note ?? null,
        },
      });
    });
  }

  const panel = lastOfKind(messages, 'step_panel');
  if (panel) {
    panel.steps.forEach((step, i) => {
      nodes.push({
        id: `step-${i}`,
        label: step.name,
        kind: 'step',
        type: 'pipeline_step',
        entity_type: 'workflow_node',
        status: mapStepRowStatus(step.status),
        step_id: `step-${i}`,
        index: nodes.length,
        entrypoint: nodes.length === 0 && i === 0,
        refs: { run_id: runId, workflow_id: DAEMON_WORKFLOW_ID, step_id: `step-${i}` },
        metadata: {
          elapsed_ms: step.elapsed_ms ?? null,
          substep_count: step.substeps?.length ?? 0,
        },
      });
    });
  }

  for (const m of messages.values()) {
    if (m.kind !== 'tool_call') continue;
    nodes.push({
      id: m.id,
      label: m.name,
      kind: 'tool',
      type: 'tool_call',
      entity_type: 'workflow_node',
      status: mapToolStatus(m.status, replay.status),
      step_id: m.id,
      index: nodes.length,
      entrypoint: nodes.length === 0,
      refs: { run_id: runId, workflow_id: DAEMON_WORKFLOW_ID, step_id: m.id },
      metadata: {
        args_summary: m.args_summary,
        result_summary: m.result_summary ?? null,
        duration_ms: m.duration_ms ?? null,
      },
    });
  }

  // chat run 无 workflow 边定义；执行顺序即控制流。
  const edges: DaemonGraphEdge[] = [];
  for (let i = 1; i < nodes.length; i++) {
    edges.push({
      from_id: nodes[i - 1]!.id,
      to_id: nodes[i]!.id,
      type: 'control_flow',
      edge_type: 'control_flow',
      intervention: false,
      metadata: {},
    });
  }

  const toolCallCount = nodes.filter((n) => n.type === 'tool_call').length;
  const planTaskCount = plan?.tasks.length ?? 0;

  return {
    projection_kind: 'run_graph',
    version: 'v1',
    run_id: runId,
    workflow_id: DAEMON_WORKFLOW_ID,
    status: mapRunStatus(replay.status),
    entrypoint: nodes[0]?.id ?? '',
    scope: { workflow_id: DAEMON_WORKFLOW_ID, run_id: runId },
    summary: {
      step_count: nodes.length,
      plan_task_count: planTaskCount,
      tool_call_count: toolCallCount,
      step_panel_step_count: panel?.steps.length ?? 0,
      artifact_count: 0,
      event_count: replay.events.length,
    },
    filters: {},
    nodes,
    edges,
    metadata: {
      source: 'node-daemon',
      projection_note:
        'Node daemon chat run：无 workflow DAG 定义，节点来自 plan_card/step_panel/tool_call 时间线帧，边为执行顺序合成的 control_flow。',
    },
  };
}

// ── task-tree 投影 ────────────────────────────────────────────────────────────

export function buildDaemonTaskTree(
  runId: string,
  replay: DaemonRunReplay,
): DaemonTaskTree {
  const messages = foldTimelineEvents(replay.events);
  const nodes: DaemonProjectionNode[] = [];
  const edges: DaemonProjectionEdge[] = [];

  const firstTs = replay.events[0]?.ts;
  const lastTs = replay.events[replay.events.length - 1]?.ts;
  const terminal = replay.status !== 'running';

  // run 根节点：label 取首个 user_turn 文本（真实数据），没有则退回 runId。
  const userTurn = lastOfKind(messages, 'user_turn') ?? [...messages.values()].find(
    (m) => m.kind === 'user_turn',
  );
  const goalText =
    userTurn && userTurn.kind === 'user_turn' ? userTurn.text.trim() : '';
  const runLabel = goalText
    ? goalText.length > 80
      ? `${goalText.slice(0, 80)}…`
      : goalText
    : runId;

  nodes.push({
    id: runId,
    entity_type: 'run',
    label: runLabel,
    status: mapRunStatus(replay.status),
    parent_id: null,
    refs: { run_id: runId, workflow_id: DAEMON_WORKFLOW_ID },
    timestamps: {
      started_at: isoOrNull(firstTs),
      ended_at: terminal ? isoOrNull(lastTs) : null,
    },
    metadata: { source: 'node-daemon' },
  });

  const plan = lastOfKind(messages, 'plan_card');
  let taskCount = 0;
  if (plan) {
    for (const task of plan.tasks) {
      nodes.push({
        id: task.id,
        entity_type: 'task',
        label: task.title || task.id,
        status: mapPlanTaskStatus(task.status),
        parent_id: runId,
        refs: {
          run_id: runId,
          task_id: task.id,
          agent_id: task.agent_id ?? null,
        },
        timestamps: {},
        metadata: {
          assignee: task.assignee ?? null,
          batch: task.batch ?? null,
          elapsed_ms: task.elapsed_ms ?? null,
        },
      });
      edges.push({
        id: `edge-task-run-${task.id}`,
        edge_type: 'belongs_to_run',
        from_id: task.id,
        to_id: runId,
        intervention: false,
        metadata: { run_id: runId },
      });
      taskCount += 1;
    }
  }

  // step 层：优先 tool_call（真实工具执行轨迹）；没有 tool 调用时退回
  // step_panel 行（assembler 管线步骤），两者皆无则树只有 run 根。
  let stepCount = 0;
  const toolCalls = [...messages.values()].filter((m) => m.kind === 'tool_call');
  if (toolCalls.length > 0) {
    for (const m of toolCalls) {
      if (m.kind !== 'tool_call') continue;
      nodes.push({
        id: m.id,
        entity_type: 'step',
        label: m.name,
        status: mapToolStatus(m.status, replay.status),
        parent_id: runId,
        refs: { run_id: runId, step_id: m.id },
        timestamps: {
          started_at: isoOrNull(m.ts),
          ended_at:
            m.duration_ms !== undefined ? isoOrNull(m.ts + m.duration_ms) : null,
        },
        metadata: {
          args_summary: m.args_summary,
          result_summary: m.result_summary ?? null,
          duration_ms: m.duration_ms ?? null,
        },
      });
      edges.push({
        id: `edge-step-run-${m.id}`,
        edge_type: 'belongs_to_run',
        from_id: m.id,
        to_id: runId,
        intervention: false,
        metadata: { run_id: runId },
      });
      stepCount += 1;
    }
  } else {
    const panel = lastOfKind(messages, 'step_panel');
    panel?.steps.forEach((step, i) => {
      const stepId = `step-${i}`;
      nodes.push({
        id: stepId,
        entity_type: 'step',
        label: step.name,
        status: mapStepRowStatus(step.status),
        parent_id: runId,
        refs: { run_id: runId, step_id: stepId },
        timestamps: {},
        metadata: {
          elapsed_ms: step.elapsed_ms ?? null,
          substep_count: step.substeps?.length ?? 0,
        },
      });
      edges.push({
        id: `edge-step-run-${stepId}`,
        edge_type: 'belongs_to_run',
        from_id: stepId,
        to_id: runId,
        intervention: false,
        metadata: { run_id: runId },
      });
      stepCount += 1;
    });
  }

  return {
    projection_kind: 'task_tree',
    version: 'v1',
    scope: { workflow_id: DAEMON_WORKFLOW_ID, run_id: runId },
    summary: {
      run_count: 1,
      task_count: taskCount,
      step_count: stepCount,
      event_count: replay.events.length,
    },
    filters: {},
    nodes,
    edges,
    metadata: {
      run_status: mapRunStatus(replay.status),
      source: 'node-daemon',
      projection_note:
        'task_tree is a graph projection over task-level lineage relations. ' +
        'Node daemon chat run：task 来自 plan_card，step 来自 tool_call/step_panel 时间线帧。',
    },
  };
}
