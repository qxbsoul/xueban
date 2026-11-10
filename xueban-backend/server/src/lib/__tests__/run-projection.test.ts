/**
 * run-projection.test.ts — L6（GAP.md:13）daemon run 投影纯函数单测。
 *
 * Run (from server/):  npx vitest run src/lib/__tests__/run-projection.test.ts
 *
 * 覆盖：
 *   1. foldTimelineEvents — 'message' + 'message-patch' 折叠（tool_finalize /
 *      plan_update_task / append_step / update_step）
 *   2. buildDaemonRunGraph — 形状对齐 Python RunGraph（projection_kind /
 *      scope / 节点 entity_type + status 映射 / control_flow 顺序边）
 *   3. buildDaemonTaskTree — run→task→step 层级、belongs_to_run 边、ISO 时间戳
 *   4. 边界：空事件日志、canceled run 悬挂 running 工具、无 plan 只有工具
 */
import { describe, it, expect } from 'vitest';
import {
  buildDaemonRunGraph,
  buildDaemonTaskTree,
  foldTimelineEvents,
  DAEMON_WORKFLOW_ID,
  type DaemonRunReplay,
} from '../run-projection';
import type { RunEventRecord } from '../run-event-bus';

let seq = 0;
function ev(event: string, data: unknown, ts = ++seq): RunEventRecord {
  return { id: seq, event, data, ts };
}

function msg(overrides: Record<string, unknown>): Record<string, unknown> {
  return { id: `m${++seq}`, turn_id: 't1', ts: seq, ...overrides };
}

/** 一条典型的 daemon chat run 事件日志：user_turn → plan_card(+patch) →
 *  tool_call(+finalize) → msg_foot。 */
function typicalReplay(status: DaemonRunReplay['status'] = 'succeeded'): DaemonRunReplay {
  const planId = 'plan1';
  const toolId = 'tool1';
  return {
    status,
    events: [
      ev('message', msg({ id: 'u1', kind: 'user_turn', text: '帮我分析这份报表' }), 1000),
      ev('message', msg({
        id: planId,
        kind: 'plan_card',
        title: '执行计划',
        tasks: [
          { id: 'node:a', title: '读取数据', status: 'running', assignee: 'reader' },
          { id: 'node:b', title: '生成图表', status: 'pending', assignee: 'plotter' },
        ],
      }), 1100),
      ev('message-patch', { id: planId, op: 'plan_update_task', task_id: 'node:a', patch: { status: 'done', elapsed_ms: 500 } }, 1200),
      ev('message', msg({ id: toolId, kind: 'tool_call', name: 'read_file', args_summary: '{"path":"a.csv"}', status: 'running', ts: 1300 }), 1300),
      ev('message-patch', { id: toolId, op: 'tool_finalize', status: 'ok', duration_ms: 42, result_summary: '3 rows' }, 1400),
      ev('message', msg({ id: 'foot1', kind: 'msg_foot', status: 'done' }), 1500),
    ],
  };
}

describe('foldTimelineEvents', () => {
  it('message 帧按 id 收录，patch 落定 tool_call 终态', () => {
    const msgs = foldTimelineEvents(typicalReplay().events);
    const tool = msgs.get('tool1');
    expect(tool).toMatchObject({ kind: 'tool_call', status: 'ok', duration_ms: 42, result_summary: '3 rows' });
  });

  it('plan_update_task patch 合并进 plan_card 任务', () => {
    const msgs = foldTimelineEvents(typicalReplay().events);
    const plan = msgs.get('plan1');
    expect(plan?.kind).toBe('plan_card');
    if (plan?.kind === 'plan_card') {
      expect(plan.tasks[0]).toMatchObject({ id: 'node:a', status: 'done', elapsed_ms: 500 });
      expect(plan.tasks[1]).toMatchObject({ id: 'node:b', status: 'pending' });
    }
  });

  it('append_step / update_step 维护 step_panel.steps', () => {
    const replay: DaemonRunReplay = {
      status: 'running',
      events: [
        ev('message', msg({ id: 'sp1', kind: 'step_panel', total_steps: 0, steps: [] })),
        ev('message-patch', { id: 'sp1', op: 'append_step', step: { name: 'assemble', status: 'running' } }),
        ev('message-patch', { id: 'sp1', op: 'update_step', index: 0, patch: { status: 'done', elapsed_ms: 10 } }),
      ],
    };
    const panel = foldTimelineEvents(replay.events).get('sp1');
    if (panel?.kind === 'step_panel') {
      expect(panel.steps).toEqual([{ name: 'assemble', status: 'done', elapsed_ms: 10 }]);
    } else {
      expect.unreachable('step_panel should exist');
    }
  });

  it('未知 id 的 patch 与非时间线事件被安全忽略', () => {
    const replay: DaemonRunReplay = {
      status: 'running',
      events: [
        ev('message-patch', { id: 'ghost', op: 'tool_finalize', status: 'ok' }),
        ev('loop', { loop_id: 'l1' }),
        ev('message', { no_id: true }),
      ],
    };
    expect(foldTimelineEvents(replay.events).size).toBe(0);
  });
});

describe('buildDaemonRunGraph', () => {
  it('形状对齐 Python RunGraph，plan 任务 + tool_call 顺序成链', () => {
    const g = buildDaemonRunGraph('run-1', typicalReplay());
    expect(g.projection_kind).toBe('run_graph');
    expect(g.version).toBe('v1');
    expect(g.run_id).toBe('run-1');
    expect(g.workflow_id).toBe(DAEMON_WORKFLOW_ID);
    expect(g.status).toBe('succeeded');
    expect(g.scope).toEqual({ workflow_id: DAEMON_WORKFLOW_ID, run_id: 'run-1' });

    expect(g.nodes.map((n) => n.id)).toEqual(['node:a', 'node:b', 'tool1']);
    expect(g.nodes[0]).toMatchObject({
      entity_type: 'workflow_node',
      status: 'succeeded',
      entrypoint: true,
      label: '读取数据',
    });
    expect(g.nodes[1]).toMatchObject({ status: 'pending', entrypoint: false });
    expect(g.nodes[2]).toMatchObject({ kind: 'tool', type: 'tool_call', status: 'succeeded' });

    expect(g.edges).toEqual([
      { from_id: 'node:a', to_id: 'node:b', type: 'control_flow', edge_type: 'control_flow', intervention: false, metadata: {} },
      { from_id: 'node:b', to_id: 'tool1', type: 'control_flow', edge_type: 'control_flow', intervention: false, metadata: {} },
    ]);
    expect(g.entrypoint).toBe('node:a');
    expect(g.summary).toMatchObject({ step_count: 3, plan_task_count: 2, tool_call_count: 1 });
  });

  it('空事件日志 → 合法空投影（nodes: []、entrypoint 空串）', () => {
    const g = buildDaemonRunGraph('run-empty', { status: 'running', events: [] });
    expect(g.projection_kind).toBe('run_graph');
    expect(g.nodes).toEqual([]);
    expect(g.edges).toEqual([]);
    expect(g.entrypoint).toBe('');
    expect(g.status).toBe('running');
  });

  it('canceled run 悬挂的 running tool_call 落定到 cancelled（不永远转圈）', () => {
    const replay: DaemonRunReplay = {
      status: 'canceled',
      events: [ev('message', msg({ id: 't9', kind: 'tool_call', name: 'bash', args_summary: 'ls', status: 'running' }))],
    };
    const g = buildDaemonRunGraph('run-cx', replay);
    expect(g.status).toBe('cancelled'); // 双 l，对齐 Python 词表
    expect(g.nodes[0]).toMatchObject({ status: 'cancelled' });
  });

  it('无 plan_card 时 step_panel 行成为节点', () => {
    const replay: DaemonRunReplay = {
      status: 'succeeded',
      events: [
        ev('message', msg({
          id: 'sp1',
          kind: 'step_panel',
          total_steps: 2,
          steps: [
            { name: 'assemble', status: 'done' },
            { name: 'render', status: 'running' },
          ],
        })),
      ],
    };
    const g = buildDaemonRunGraph('run-sp', replay);
    expect(g.nodes.map((n) => [n.id, n.status])).toEqual([
      ['step-0', 'succeeded'],
      ['step-1', 'running'],
    ]);
    expect(g.entrypoint).toBe('step-0');
  });
});

describe('buildDaemonTaskTree', () => {
  it('run→task→step 层级 + belongs_to_run 边 + ISO 时间戳', () => {
    const t = buildDaemonTaskTree('run-1', typicalReplay());
    expect(t.projection_kind).toBe('task_tree');
    expect(t.version).toBe('v1');
    expect(t.scope).toEqual({ workflow_id: DAEMON_WORKFLOW_ID, run_id: 'run-1' });

    const run = t.nodes.find((n) => n.entity_type === 'run');
    expect(run).toMatchObject({
      id: 'run-1',
      label: '帮我分析这份报表',
      status: 'succeeded',
      parent_id: null,
    });
    expect(run?.timestamps.started_at).toBe(new Date(1000).toISOString());
    expect(run?.timestamps.ended_at).toBe(new Date(1500).toISOString());

    const tasks = t.nodes.filter((n) => n.entity_type === 'task');
    expect(tasks.map((x) => x.id)).toEqual(['node:a', 'node:b']);
    expect(tasks.every((x) => x.parent_id === 'run-1')).toBe(true);

    const steps = t.nodes.filter((n) => n.entity_type === 'step');
    expect(steps.map((x) => x.id)).toEqual(['tool1']);
    expect(steps[0]).toMatchObject({ parent_id: 'run-1', status: 'succeeded', label: 'read_file' });
    expect(steps[0]?.timestamps.started_at).toBe(new Date(1300).toISOString());
    expect(steps[0]?.timestamps.ended_at).toBe(new Date(1342).toISOString());

    expect(t.edges).toHaveLength(3);
    expect(t.edges.every((e) => e.edge_type === 'belongs_to_run' && e.to_id === 'run-1')).toBe(true);
    expect(t.summary).toMatchObject({ run_count: 1, task_count: 2, step_count: 1 });
  });

  it('running 中的 run：ended_at 为 null', () => {
    const t = buildDaemonTaskTree('run-live', typicalReplay('running'));
    const run = t.nodes.find((n) => n.entity_type === 'run');
    expect(run?.status).toBe('running');
    expect(run?.timestamps.ended_at).toBeNull();
  });

  it('空事件日志 → 仅 run 根节点、无边，仍是 200 合法投影', () => {
    const t = buildDaemonTaskTree('run-empty', { status: 'succeeded', events: [] });
    expect(t.nodes).toHaveLength(1);
    expect(t.nodes[0]).toMatchObject({ id: 'run-empty', entity_type: 'run', label: 'run-empty' });
    expect(t.edges).toEqual([]);
    expect(t.summary).toMatchObject({ task_count: 0, step_count: 0 });
  });
});
