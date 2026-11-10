import { useCallback, useEffect, useState } from 'react';
import {
  getProject,
  getRoster,
  getTaskExecutions,
  updateProjectTask,
} from '../api';
import type {
  ProjectDetailDto,
  ProjectTaskDto,
  RosterEmployeeDto,
  TaskExecutionDto,
} from '../api';
import { useWorkbench } from '../store';

export default function ProjectAuditPanel({ projectId, refreshKey }: { projectId: string; refreshKey?: string }) {
  const { companyId, toast } = useWorkbench();
  const [detail, setDetail] = useState<ProjectDetailDto | null>(null);
  const [roster, setRoster] = useState<RosterEmployeeDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [historyTaskId, setHistoryTaskId] = useState<string | null>(null);
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [project, employees] = await Promise.all([
        getProject(projectId),
        companyId ? getRoster(companyId) : Promise.resolve([]),
      ]);
      setDetail(project);
      setRoster(employees.filter(item => item.employment !== 'retired' && item.status !== 'offline'));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '项目任务加载失败');
    } finally {
      setLoading(false);
    }
  }, [projectId, companyId, refreshKey]);

  useEffect(() => { void load(); }, [load]);

  const saved = async (task: ProjectTaskDto) => {
    setDetail(current => current ? {
      ...current,
      tasks: current.tasks.map(item => item.id === task.id ? task : item),
    } : current);
    setEditingTaskId(null);
    if (historyTaskId === task.id) setHistoryTaskId(null);
    toast(task.status === 'todo' ? '任务已保存，可在项目执行时使用新配置' : '任务已保存');
  };

  return (
    <section className="project-audit">
      <div className="project-audit-head">
        <div><h4>任务与执行审计</h4><span>每次执行独立留痕，重试不会覆盖历史</span></div>
        <button className="mini-btn" disabled={loading} onClick={() => void load()}>刷新</button>
      </div>
      {error && <div className="audit-error">{error}<button className="mini-btn" onClick={() => void load()}>重试</button></div>}
      {loading && !detail ? <div className="empty-tip">正在读取任务与执行配置…</div> : detail?.tasks.length ? (
        <div className="audit-task-list">
          {detail.tasks.map(task => (
            <article className="audit-task" key={task.id}>
              <div className="audit-task-row">
                <div className="audit-task-title">
                  <strong>{task.title}</strong>
                  <span>{task.status} · {task.assignee?.name || '未分配'} · {task.priority || 'normal'}</span>
                </div>
                <div className="audit-task-actions">
                  <button className="mini-btn" onClick={() => setHistoryTaskId(current => current === task.id ? null : task.id)}>
                    {historyTaskId === task.id ? '收起记录' : '执行记录'}
                  </button>
                  {(task.status === 'todo' || task.status === 'failed') && (
                    <button className="mini-btn" onClick={() => setEditingTaskId(current => current === task.id ? null : task.id)}>
                      {editingTaskId === task.id ? '取消编辑' : task.status === 'failed' ? '编辑 / 重新分配' : '编辑任务'}
                    </button>
                  )}
                </div>
              </div>
              {task.status === 'failed' && task.output?.error && <div className="audit-error">最近错误：{task.output.error}</div>}
              {editingTaskId === task.id && (
                <TaskEditForm key={`${task.id}:${task.status}`} task={task} roster={roster} onSaved={saved} onCancel={() => setEditingTaskId(null)} />
              )}
              {historyTaskId === task.id && <TaskExecutionHistory taskId={task.id} />}
            </article>
          ))}
        </div>
      ) : <div className="empty-tip">该项目暂无任务记录。</div>}
    </section>
  );
}

function TaskEditForm({
  task,
  roster,
  onSaved,
  onCancel,
}: {
  task: ProjectTaskDto;
  roster: RosterEmployeeDto[];
  onSaved: (task: ProjectTaskDto) => Promise<void>;
  onCancel: () => void;
}) {
  const { toast } = useWorkbench();
  const [title, setTitle] = useState(task.title);
  const [brief, setBrief] = useState(task.brief || '');
  const [priority, setPriority] = useState(task.priority || 'normal');
  const [assignee, setAssignee] = useState(task.assignee?.id || '');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim()) return toast('任务标题不能为空');
    if (!assignee) return toast('请选择负责人');
    setBusy(true);
    try {
      const result = await updateProjectTask(task.id, { title: title.trim(), brief, priority, assignee });
      await onSaved(result.task);
    } catch (error) {
      toast(`任务保存失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="task-edit-form">
      <div className="form-group"><label className="form-label">任务标题</label><input className="form-input" value={title} disabled={busy} onChange={event => setTitle(event.target.value)} /></div>
      <div className="form-group"><label className="form-label">任务要求</label><textarea className="form-area" value={brief} disabled={busy} onChange={event => setBrief(event.target.value)} /></div>
      <div className="task-edit-grid">
        <div className="form-group"><label className="form-label">优先级</label><select className="form-select" value={priority} disabled={busy} onChange={event => setPriority(event.target.value)}><option value="low">低</option><option value="normal">普通</option><option value="high">高</option><option value="urgent">紧急</option></select></div>
        <div className="form-group"><label className="form-label">负责人</label><select className="form-select" value={assignee} disabled={busy} onChange={event => setAssignee(event.target.value)}><option value="">请选择</option>{roster.map(employee => <option key={employee.id} value={employee.id}>{employee.name} · {employee.title}</option>)}</select></div>
      </div>
      {task.status === 'failed' && <div className="empty-tip">更换负责人并保存后，任务将回到待执行；原有失败记录继续保留。</div>}
      <div className="audit-form-actions"><button className="btn btn-ghost btn-sm" disabled={busy} onClick={onCancel}>取消</button><button className="btn btn-dark btn-sm" disabled={busy} onClick={() => void submit()}>{busy ? '保存中…' : '保存任务'}</button></div>
    </div>
  );
}

function TaskExecutionHistory({ taskId }: { taskId: string }) {
  const [items, setItems] = useState<TaskExecutionDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try { setItems(await getTaskExecutions(taskId)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '执行记录加载失败'); }
    finally { setLoading(false); }
  }, [taskId]);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="execution-history">
      <div className="execution-history-head"><strong>执行历史</strong><button className="mini-btn" disabled={loading} onClick={() => void load()}>刷新记录</button></div>
      {error && <div className="audit-error">{error}</div>}
      {loading ? <div className="empty-tip">正在读取执行记录…</div> : items.length ? items.map(item => (
        <div className="execution-record" key={item.id}>
          <div className="execution-record-head">
            <strong>第 {item.attempt} 次 · {item.status}</strong>
            <span>{formatDuration(item.duration_ms)}</span>
          </div>
          <div className="audit-meta">配置 v{item.config_version} · 模型 {item.model || '—'} · {item.started_at}{item.ended_at ? ` → ${item.ended_at}` : ''}</div>
          {item.error && <div className="audit-error">错误：{item.error}</div>}
          <details><summary>输入摘要</summary><p>{item.input_summary || '无输入摘要'}</p></details>
          <details><summary>输出摘要</summary><p>{item.output_summary || '无输出摘要'}</p></details>
        </div>
      )) : <div className="empty-tip">该任务尚未产生执行记录。</div>}
    </div>
  );
}

function formatDuration(value?: number | null) {
  if (value === null || value === undefined) return '未结束';
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`;
}
