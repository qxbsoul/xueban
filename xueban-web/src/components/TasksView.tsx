import { useState, type FormEvent } from 'react';
import { ApiError, type SchedulePatch, type ScheduleRunDto } from '../api';
import { useWorkbench } from '../store';
import type { TaskItem } from '../types';

function localDateTime(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function runTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function errorText(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 503 && cause.code === 'EXECUTOR_UNAVAILABLE') {
    return '未配置任务执行器，本次未执行，也未记录成功（EXECUTOR_UNAVAILABLE）。';
  }
  return cause instanceof Error ? cause.message : String(cause);
}

interface EditorState {
  description: string;
  enabled: boolean;
  durationMin: number;
  mode: 'cron' | 'once';
  cron: string;
  startAt: string;
}

function editorState(task: TaskItem): EditorState {
  return {
    description: task.title,
    enabled: task.enabled,
    durationMin: task.durationMin ?? 30,
    mode: task.cronExpression ? 'cron' : 'once',
    cron: task.cronExpression ?? '0 9 * * *',
    startAt: localDateTime(task.startAt),
  };
}

function TaskEditor({ task, busy, onCancel, onSave }: {
  task: TaskItem;
  busy: boolean;
  onCancel: () => void;
  onSave: (patch: SchedulePatch) => Promise<void>;
}) {
  const [form, setForm] = useState<EditorState>(() => editorState(task));
  const [validation, setValidation] = useState('');
  const update = <K extends keyof EditorState>(key: K, value: EditorState[K]) => setForm(previous => ({ ...previous, [key]: value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setValidation('');
    if (!form.description.trim()) return setValidation('请输入任务描述');
    if (!Number.isInteger(form.durationMin) || form.durationMin < 1 || form.durationMin > 1440) return setValidation('预计时长需为 1–1440 分钟的整数');
    const patch: SchedulePatch = {
      task_description: form.description.trim(),
      enabled: form.enabled,
      agent_id: null,
      duration_min: form.durationMin,
      cron_expression: form.mode === 'cron' ? form.cron.trim() : null,
      start_at: null,
    };
    if (form.mode === 'cron') {
      if (form.cron.trim().split(/\s+/).length !== 5) return setValidation('Cron 表达式需包含 5 个字段，例如 0 9 * * 1-5');
    } else {
      const start = new Date(form.startAt);
      if (!form.startAt || Number.isNaN(start.getTime())) return setValidation('请选择有效的一次性执行时间');
      if (start.getTime() <= Date.now()) return setValidation('一次性执行时间必须晚于当前时间');
      patch.start_at = start.toISOString();
    }
    try {
      await onSave(patch);
    } catch {
      // 页面统一展示真实错误，保留表单供修正后重试。
    }
  };

  return (
    <form className="panel panel-pad" style={{ margin: '-3px 0 12px' }} onSubmit={event => void submit(event)}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12 }}>
        <div className="form-group" style={{ gridColumn: '1 / -1' }}>
          <label className="form-label">任务描述</label>
          <textarea className="form-area" style={{ minHeight: 70 }} value={form.description} maxLength={500} disabled={busy} onChange={event => update('description', event.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">执行方式</label>
          <div className="form-static">教学主管统一调度：拆解学情任务并分派给合适的教研员</div>
        </div>
        <div className="form-group">
          <label className="form-label">预计时长（分钟）</label>
          <input className="form-input" type="number" min={1} max={1440} step={1} value={form.durationMin} disabled={busy} onChange={event => update('durationMin', Number(event.target.value))} />
        </div>
        <div className="form-group">
          <label className="form-label">计划类型</label>
          <select className="form-select" value={form.mode} disabled={busy} onChange={event => update('mode', event.target.value as EditorState['mode'])}>
            <option value="cron">Cron 周期任务</option>
            <option value="once">一次性任务</option>
          </select>
        </div>
        {form.mode === 'cron' ? (
          <div className="form-group">
            <label className="form-label">Cron 表达式</label>
            <input className="form-input" value={form.cron} disabled={busy} onChange={event => update('cron', event.target.value)} placeholder="0 9 * * 1-5" />
            <small style={{ color: 'var(--muted)' }}>分 时 日 月 周；最短执行间隔由服务端校验。</small>
          </div>
        ) : (
          <div className="form-group">
            <label className="form-label">执行时间</label>
            <input className="form-input" type="datetime-local" value={form.startAt} disabled={busy} onChange={event => update('startAt', event.target.value)} />
          </div>
        )}
      </div>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, fontSize: 12 }}>
        <input type="checkbox" checked={form.enabled} disabled={busy} onChange={event => update('enabled', event.target.checked)} /> 启用此任务
      </label>
      {validation && <div className="kb-error" role="alert">{validation}</div>}
      <div className="actions">
        <button className="btn btn-dark btn-sm" type="submit" disabled={busy}>{busy ? '保存中…' : '保存任务'}</button>
        <button className="btn btn-ghost btn-sm" type="button" disabled={busy} onClick={onCancel}>取消</button>
      </div>
    </form>
  );
}

export default function TasksView() {
  const { tasks, toggleTask, saveTask, runTask, getTaskRuns, deleteTask, openModal, openConversation } = useWorkbench();
  const [busy, setBusy] = useState('');
  const [editingId, setEditingId] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState('');
  const [expandedRunsId, setExpandedRunsId] = useState('');
  const [runs, setRuns] = useState<Record<string, ScheduleRunDto[]>>({});
  const [runsError, setRunsError] = useState<Record<string, string>>({});
  const [feedback, setFeedback] = useState<Record<string, { failed: boolean; text: string }>>({});

  const perform = async (key: string, operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(key);
    try {
      await operation();
    } catch (cause) {
      const id = key.split(':').pop() ?? '';
      setFeedback(previous => ({ ...previous, [id]: { failed: true, text: errorText(cause) } }));
    } finally {
      setBusy('');
    }
  };

  const loadRuns = async (id: string, force = false) => {
    if (!force && runs[id]) return;
    setRunsError(previous => ({ ...previous, [id]: '' }));
    try {
      const result = await getTaskRuns(id);
      setRuns(previous => ({ ...previous, [id]: result }));
    } catch (cause) {
      setRunsError(previous => ({ ...previous, [id]: errorText(cause) }));
    }
  };

  const toggleRuns = (id: string) => {
    const opening = expandedRunsId !== id;
    setExpandedRunsId(opening ? id : '');
    if (opening) void loadRuns(id);
  };

  return (
    <div className="view-inner">
      <div className="view-head">
        <h2>学习计划</h2>
        <div className="actions">
          <button className="btn btn-primary btn-sm" onClick={() => openModal({ kind: 'new-task' })}>新建学习计划</button>
        </div>
      </div>

      {tasks.length ? tasks.map(task => (
        <div key={task.id}>
          <div className="task-card">
            <div className="task-ico">{task.mark}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="task-name">{task.title}</div>
              <div className="task-meta">
                <span>计划：<b>{task.schedule}</b></span>
                <span>执行：<b>教学主管统一调度</b></span>
                <span>已执行 <b>{task.runs}</b> 次</span>
                <span>时长 <b>{task.durationMin ?? 30} 分钟</b></span>
                {task.once ? <span className="pill pill-wait">一次性</span> : <span className="pill pill-ok">周期</span>}
              </div>
              {feedback[task.id] && (
                <div role={feedback[task.id].failed ? 'alert' : 'status'} style={{ marginTop: 8, color: feedback[task.id].failed ? 'var(--red)' : 'var(--accent-teal)', fontSize: 12 }}>
                  {feedback[task.id].text}
                </div>
              )}
            </div>
            <div className="task-right" style={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <div className="task-sched">下次运行<b>{task.next}</b></div>
              <button className={task.enabled ? 'switch on' : 'switch'} aria-label={task.enabled ? '暂停任务' : '启用任务'} aria-pressed={task.enabled} disabled={Boolean(busy)} onClick={() => void perform(`toggle:${task.id}`, () => toggleTask(task.id))}><i /></button>
              <button className="mini-btn" disabled={Boolean(busy)} onClick={() => void perform(`run:${task.id}`, async () => {
                const result = await runTask(task.id);
                setFeedback(previous => ({ ...previous, [task.id]: {
                  failed: result.status !== 'succeeded',
                  text: result.status === 'succeeded' ? `真实执行完成：${runTime(result.triggered_at)}` : `执行失败：${result.error || result.status}`,
                } }));
                if (expandedRunsId === task.id) await loadRuns(task.id, true);
              })}>{busy === `run:${task.id}` ? '执行中…' : '立即执行'}</button>
              <button className="mini-btn" disabled={Boolean(busy)} onClick={() => { setEditingId(editingId === task.id ? '' : task.id); setConfirmDeleteId(''); }}>编辑</button>
              <button className="mini-btn" disabled={Boolean(busy)} onClick={() => toggleRuns(task.id)}>{expandedRunsId === task.id ? '收起历史' : '运行历史'}</button>
              <button className="mini-btn" disabled={Boolean(busy)} onClick={() => { setConfirmDeleteId(task.id); setEditingId(''); }}>删除</button>
            </div>
          </div>
          {editingId === task.id && <TaskEditor task={task} busy={busy === `save:${task.id}`} onCancel={() => setEditingId('')} onSave={async patch => {
            await perform(`save:${task.id}`, async () => {
              await saveTask(task.id, patch);
              setEditingId('');
              setFeedback(previous => ({ ...previous, [task.id]: { failed: false, text: '任务修改已由服务端确认。' } }));
            });
          }} />}
          {confirmDeleteId === task.id && (
            <div className="panel panel-pad" style={{ margin: '-3px 0 12px' }} role="alert">
              确认删除「{task.title}」？删除后不再自动执行。
              <div className="actions" style={{ marginTop: 10 }}>
                <button className="btn btn-danger btn-sm" disabled={Boolean(busy)} onClick={() => void perform(`delete:${task.id}`, async () => {
                  await deleteTask(task.id);
                  setConfirmDeleteId('');
                })}>{busy === `delete:${task.id}` ? '删除中…' : '确认删除'}</button>
                <button className="btn btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => setConfirmDeleteId('')}>取消</button>
              </div>
            </div>
          )}
          {expandedRunsId === task.id && (
            <div className="panel panel-pad" style={{ margin: '-3px 0 12px' }}>
              <div className="actions" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
                <b>最近运行记录</b>
                <button className="mini-btn" disabled={busy === `history:${task.id}`} onClick={() => void perform(`history:${task.id}`, () => loadRuns(task.id, true))}>刷新</button>
              </div>
              {runsError[task.id] && <div className="kb-error" role="alert">{runsError[task.id]}</div>}
              {!runsError[task.id] && !runs[task.id] && <div className="empty-tip">正在读取运行历史…</div>}
              {runs[task.id]?.length === 0 && <div className="empty-tip">暂无运行记录</div>}
              {runs[task.id]?.map(run => (
                <div key={run.run_id} style={{ padding: '10px 0', borderTop: '1px solid var(--line-soft)' }}>
                  <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 12 }}>
                    <b style={{ color: run.status === 'succeeded' ? 'var(--accent-teal)' : 'var(--red)' }}>{run.status === 'succeeded' ? '成功' : run.status === 'failed' ? '失败' : run.status}</b>
                    <span>{runTime(run.triggered_at)}</span>
                    <code>{run.run_id}</code>
                  </div>
                  {run.error && <div role="alert" style={{ color: 'var(--red)', marginTop: 6, whiteSpace: 'pre-wrap', fontSize: 12 }}>{run.error}</div>}
                  {run.output_summary && <div className="preview-box" style={{ marginTop: 8, whiteSpace: 'pre-wrap', fontSize: 12 }}>{run.output_summary}</div>}
                  {run.conversation_id && <button className="mini-btn" style={{ marginTop: 8 }} onClick={() => void openConversation(run.conversation_id!)}>查看执行对话</button>}
                </div>
              ))}
            </div>
          )}
        </div>
      )) : (
        <div className="panel panel-pad"><div className="empty-tip">还没有学习计划，点右上角「新建」</div></div>
      )}
    </div>
  );
}
