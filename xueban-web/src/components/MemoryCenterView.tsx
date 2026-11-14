import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  JOB_STATUS_LABEL,
  MEMORY_CATEGORY_LABEL,
  acceptCandidate,
  archiveMemory,
  blockMemory,
  correctMemory,
  deleteMemory,
  getMemory,
  listCandidates,
  listJobs,
  listMemories,
  listOutbox,
  rejectCandidate,
  restoreOutbox,
  type CandidateItem,
  type MemoryDetail,
  type MemoryItem,
  type MemoryJob,
  type MemoryOutboxRow,
} from '../memory-api';
import './memory-center.css';

type Tab = 'overview' | 'memories' | 'pending' | 'blocked' | 'jobs';

const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'overview', label: '记忆总览' },
  { key: 'memories', label: '我的记忆' },
  { key: 'pending', label: '待审核' },
  { key: 'blocked', label: '不再学习' },
  { key: 'jobs', label: '处理记录' },
];

const SUBJECTS = ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治', '科学'];

function formatTime(value: number | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { hour12: false });
}

function short(text: string, max = 90): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function explicitnessLabel(value: string): string {
  return value === 'inferred' ? '模型推断' : '明确表达';
}

export default function MemoryCenterView() {
  const [tab, setTab] = useState<Tab>('overview');
  const [activeMemories, setActiveMemories] = useState<MemoryItem[]>([]);
  const [archivedMemories, setArchivedMemories] = useState<MemoryItem[]>([]);
  const [blockedMemories, setBlockedMemories] = useState<MemoryItem[]>([]);
  const [pending, setPending] = useState<CandidateItem[]>([]);
  const [conflicts, setConflicts] = useState<CandidateItem[]>([]);
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [outbox, setOutbox] = useState<MemoryOutboxRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [editReason, setEditReason] = useState('');
  const [candidate, setCandidate] = useState<CandidateItem | null>(null);
  const [busy, setBusy] = useState(false);

  const [search, setSearch] = useState('');
  const [subject, setSubject] = useState('');
  const [category, setCategory] = useState('');
  const [explicitness, setExplicitness] = useState('');
  const [listStatus, setListStatus] = useState<'active' | 'archived'>('active');
  const [sort, setSort] = useState<'updated' | 'created'>('updated');

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [active, archived, blocked, pendingItems, conflictItems, jobItems, outboxItems] = await Promise.all([
        listMemories({ status: 'active', limit: 200 }),
        listMemories({ status: 'archived', limit: 200 }),
        listMemories({ status: 'blocked', limit: 200 }),
        listCandidates('pending'),
        listCandidates('conflict'),
        listJobs(),
        listOutbox(),
      ]);
      setActiveMemories(active);
      setArchivedMemories(archived);
      setBlockedMemories(blocked);
      setPending(pendingItems);
      setConflicts(conflictItems);
      setJobs(jobItems);
      setOutbox(outboxItems);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载记忆失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function openMemory(id: string) {
    setDetailLoading(true);
    setError('');
    try {
      const result = await getMemory(id);
      setDetail(result);
      setDraft(result.memory.content);
      setEditReason('');
      setEditing(false);
      setCandidate(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载记忆详情失败');
    } finally {
      setDetailLoading(false);
    }
  }

  async function run(action: () => Promise<void>, successText: string) {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await action();
      setNotice(successText);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败');
    } finally {
      setBusy(false);
    }
  }

  const sourceMemories = listStatus === 'active' ? activeMemories : archivedMemories;
  const subjects = useMemo(() => {
    const set = new Set<string>(SUBJECTS);
    [...activeMemories, ...archivedMemories, ...blockedMemories].forEach((item) => {
      if (item.subject) set.add(item.subject);
    });
    return [...set];
  }, [activeMemories, archivedMemories, blockedMemories]);

  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return sourceMemories
      .filter((item) => (subject ? item.subject === subject : true))
      .filter((item) => (category ? item.category === category : true))
      .filter((item) => (explicitness ? item.explicitness === explicitness : true))
      .filter((item) => (keyword
        ? `${item.title}\n${item.content}\n${item.topic}`.toLowerCase().includes(keyword)
        : true))
      .sort((a, b) => (sort === 'updated' ? b.updated_at - a.updated_at : b.created_at - a.created_at));
  }, [sourceMemories, subject, category, explicitness, search, sort]);

  const latestUpdate = activeMemories.reduce((max, item) => Math.max(max, item.updated_at), 0);

  function renderOverview() {
    const pendingCount = pending.length;
    const conflictCount = conflicts.length;
    return (
      <div className="memory-overview">
        <div className="memory-stat-grid">
          <div className="memory-stat"><span>正式记忆</span><strong>{activeMemories.length}</strong></div>
          <div className="memory-stat"><span>待审核</span><strong>{pendingCount}</strong></div>
          <div className="memory-stat"><span>冲突待处理</span><strong>{conflictCount}</strong></div>
          <div className="memory-stat"><span>最近更新</span><strong className="memory-stat-time">{formatTime(latestUpdate)}</strong></div>
        </div>

        {(pendingCount > 0 || conflictCount > 0) && (
          <button type="button" className="memory-alert" onClick={() => setTab('pending')}>
            有 {pendingCount} 条新记忆等待确认{conflictCount > 0 ? `，其中 ${conflictCount} 条与已有记忆存在冲突` : ''}。
            <span>去处理 ›</span>
          </button>
        )}

        <section className="memory-section">
          <header><h3>记忆分类</h3></header>
          <div className="memory-category-grid">
            {Object.entries(MEMORY_CATEGORY_LABEL).map(([key, label]) => {
              const count = activeMemories.filter((item) => item.category === key).length;
              return (
                <button
                  type="button"
                  key={key}
                  className="memory-category-card"
                  onClick={() => { setCategory(key); setTab('memories'); }}
                >
                  <strong>{label}</strong>
                  <span>{count} 条</span>
                </button>
              );
            })}
          </div>
        </section>

        <section className="memory-section">
          <header><h3>最近更新</h3><button type="button" className="memory-link" onClick={() => setTab('memories')}>查看全部</button></header>
          {activeMemories.length === 0 ? (
            <p className="memory-empty">还没有形成正式记忆。正常聊天后，系统会把值得长期保留的信息整理到这里。</p>
          ) : (
            <ul className="memory-recent-list">
              {[...activeMemories].sort((a, b) => b.updated_at - a.updated_at).slice(0, 5).map((item) => (
                <li key={item.id}>
                  <button type="button" onClick={() => void openMemory(item.id)}>
                    <div className="memory-item-head">
                      <strong>{item.title}</strong>
                      <span className={`memory-badge ${item.explicitness === 'inferred' ? 'inferred' : 'explicit'}`}>
                        {explicitnessLabel(item.explicitness)}
                      </span>
                    </div>
                    <p>{short(item.content)}</p>
                    <div className="memory-meta">
                      <span>{item.subject || '通用'}{item.topic ? ` · ${item.topic}` : ''}</span>
                      <span>{formatTime(item.updated_at)}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    );
  }

  function renderMemoryList() {
    return (
      <div className="memory-list-pane">
        <div className="memory-toolbar">
          <input
            className="memory-search"
            placeholder="搜索标题或内容"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <select value={subject} onChange={(event) => setSubject(event.target.value)}>
            <option value="">全部学科</option>
            {subjects.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">全部类别</option>
            {Object.entries(MEMORY_CATEGORY_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
          <select value={explicitness} onChange={(event) => setExplicitness(event.target.value)}>
            <option value="">全部来源</option>
            <option value="explicit">明确表达</option>
            <option value="inferred">模型推断</option>
          </select>
          <select value={listStatus} onChange={(event) => setListStatus(event.target.value as 'active' | 'archived')}>
            <option value="active">正常使用</option>
            <option value="archived">已归档</option>
          </select>
          <select value={sort} onChange={(event) => setSort(event.target.value as 'updated' | 'created')}>
            <option value="updated">按更新时间</option>
            <option value="created">按创建时间</option>
          </select>
        </div>
        <p className="memory-hint">搜索与筛选作用于当前已加载的最多 200 条记忆。</p>

        {filtered.length === 0 ? (
          <p className="memory-empty">{search || subject || category || explicitness ? '没有符合条件的记忆。' : '这个分类下还没有记忆。'}</p>
        ) : (
          <ul className="memory-list">
            {filtered.map((item) => (
              <li key={item.id} className={detail?.memory.id === item.id ? 'selected' : ''}>
                <button type="button" onClick={() => void openMemory(item.id)}>
                  <div className="memory-item-head">
                    <strong>{item.title}</strong>
                    <span className={`memory-badge ${item.explicitness === 'inferred' ? 'inferred' : 'explicit'}`}>
                      {explicitnessLabel(item.explicitness)}
                    </span>
                  </div>
                  <p>{short(item.content)}</p>
                  <div className="memory-meta">
                    <span>{MEMORY_CATEGORY_LABEL[item.category] ?? item.category}</span>
                    <span>{item.subject || '通用'}{item.topic ? ` · ${item.topic}` : ''}</span>
                    <span>置信度 {Math.round(item.confidence * 100)}%</span>
                    <span>{item.evidence_count} 条证据</span>
                    <span>{formatTime(item.updated_at)}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  function renderDetail() {
    if (detailLoading) return <p className="memory-empty">加载详情…</p>;
    if (!detail) return <p className="memory-empty">从左侧选择一条记忆查看详情。</p>;
    const memory = detail.memory;
    return (
      <div className="memory-detail">
        <header>
          <div>
            <h3>{memory.title}</h3>
            <div className="memory-meta">
              <span>{MEMORY_CATEGORY_LABEL[memory.category] ?? memory.category}</span>
              <span>{memory.subject || '通用'}</span>
              {memory.topic && <span>{memory.topic}</span>}
              <span className={`memory-badge ${memory.explicitness === 'inferred' ? 'inferred' : 'explicit'}`}>
                {explicitnessLabel(memory.explicitness)}
              </span>
              <span>v{memory.version}</span>
            </div>
          </div>
          <button type="button" className="memory-close" onClick={() => { setDetail(null); setEditing(false); }}>关闭</button>
        </header>

        {editing ? (
          <div className="memory-edit">
            <label>修改内容</label>
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={5} />
            <label>修改原因（可选）</label>
            <input value={editReason} onChange={(event) => setEditReason(event.target.value)} placeholder="例如：我现在的偏好变了" />
            <div className="memory-actions">
              <button
                type="button"
                className="memory-btn primary"
                disabled={busy || !draft.trim()}
                onClick={() => void run(async () => {
                  await correctMemory(memory.id, draft.trim(), editReason.trim() || undefined);
                  await openMemory(memory.id);
                  setEditing(false);
                }, '记忆已更新')}
              >保存修改</button>
              <button type="button" className="memory-btn" onClick={() => { setEditing(false); setDraft(memory.content); }}>取消</button>
            </div>
          </div>
        ) : (
          <>
            <p className="memory-content">{memory.content}</p>
            <div className="memory-meta memory-meta-block">
              <span>置信度 {Math.round(memory.confidence * 100)}%</span>
              <span>证据 {memory.evidence_count} 条</span>
              <span>创建 {formatTime(memory.created_at)}</span>
              <span>更新 {formatTime(memory.updated_at)}</span>
            </div>
            <div className="memory-actions">
              <button type="button" className="memory-btn primary" onClick={() => setEditing(true)} disabled={memory.status !== 'active'}>修改</button>
              <button
                type="button"
                className="memory-btn"
                disabled={busy || memory.status !== 'active'}
                onClick={() => {
                  if (!window.confirm('归档后这条记忆会暂时退出召回，可在“已归档”里查看。确认归档？')) return;
                  void run(async () => { await archiveMemory(memory.id); setDetail(null); }, '已归档');
                }}
              >归档</button>
              <button
                type="button"
                className="memory-btn"
                disabled={busy || memory.status !== 'active'}
                onClick={() => {
                  if (!window.confirm('“不再学习”会清除这条记忆的原文、证据与版本，并阻止系统以后自动重建同一内容。确认？')) return;
                  void run(async () => { await blockMemory(memory.id); setDetail(null); }, '已设置为不再学习');
                }}
              >不再学习</button>
              <button
                type="button"
                className="memory-btn danger"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm('永久删除会删除正式记忆、证据与版本历史，并保留最小化墓碑防止重新学习。此操作不可通过普通接口恢复，确认？')) return;
                  void run(async () => { await deleteMemory(memory.id); setDetail(null); }, '已永久删除');
                }}
              >永久删除</button>
            </div>
          </>
        )}

        <section className="memory-section">
          <h4>来源与证据</h4>
          {detail.evidence.length === 0 ? (
            <p className="memory-empty">这条记忆没有可展示的原始证据（可能已被删除或由学生手动创建）。</p>
          ) : (
            <ul className="memory-evidence">
              {detail.evidence.map((item) => (
                <li key={item.id}>
                  <blockquote>{item.quote}</blockquote>
                  <div className="memory-meta">
                    <span>会话 {item.conversation_id || '—'}</span>
                    <span>消息 {item.message_id}</span>
                    <span>{formatTime(item.created_at)}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="memory-section">
          <h4>版本历史</h4>
          {detail.versions.length === 0 ? (
            <p className="memory-empty">暂无历史版本。</p>
          ) : (
            <ul className="memory-versions">
              {detail.versions.map((item) => (
                <li key={`${item.version}-${item.created_at}`}>
                  <div className="memory-meta">
                    <strong>v{item.version}</strong>
                    <span>{item.changed_by === 'student' ? '学生修改' : '系统更新'}</span>
                    <span>{formatTime(item.created_at)}</span>
                  </div>
                  {item.reason && <p className="memory-reason">原因：{item.reason}</p>}
                  {item.snapshot && <p className="memory-old">{short(item.snapshot.content, 120)}</p>}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    );
  }

  function renderPending() {
    return (
      <div className="memory-pending">
        <section className="memory-section">
          <h3>待审核候选</h3>
          {pending.length === 0 ? (
            <p className="memory-empty">当前没有等待确认的新记忆。</p>
          ) : (
            <ul className="memory-list">
              {pending.map((item) => (
                <li key={item.id} className={candidate?.id === item.id ? 'selected' : ''}>
                  <button type="button" onClick={() => { setCandidate(item); setDetail(null); }}>
                    <div className="memory-item-head">
                      <strong>{item.title}</strong>
                      <span className={`memory-badge ${item.explicitness === 'inferred' ? 'inferred' : 'explicit'}`}>
                        {explicitnessLabel(item.explicitness)}
                      </span>
                    </div>
                    <p>{short(item.content)}</p>
                    <div className="memory-meta">
                      <span>{MEMORY_CATEGORY_LABEL[item.category] ?? item.category}</span>
                      <span>{item.subject || '通用'}</span>
                      <span>置信度 {Math.round(item.confidence * 100)}%</span>
                      <span>{item.evidence.length} 条证据</span>
                      <span>{formatTime(item.created_at)}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="memory-section">
          <h3>冲突记忆</h3>
          {conflicts.length === 0 ? (
            <p className="memory-empty">当前没有需要处理的记忆冲突。</p>
          ) : (
            <ul className="memory-list">
              {conflicts.map((item) => (
                <li key={item.id} className={candidate?.id === item.id ? 'selected' : ''}>
                  <button type="button" onClick={() => { setCandidate(item); setDetail(null); }}>
                    <div className="memory-item-head">
                      <strong>{item.title}</strong>
                      <span className="memory-badge conflict">冲突</span>
                    </div>
                    <p>{short(item.content)}</p>
                    <div className="memory-meta">
                      <span>{item.subject || '通用'}</span>
                      <span>新候选 · {formatTime(item.created_at)}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {candidate && (
          <div className="memory-candidate-detail">
            <header>
              <h3>{candidate.title}</h3>
              <button type="button" className="memory-close" onClick={() => setCandidate(null)}>关闭</button>
            </header>
            {candidate.status === 'conflict' && (
              <p className="memory-warning">系统检测到这条新记忆与已有记忆不一致。请选择“采用新记忆”或“保留原记忆”，这不代表旧说法是错误的。</p>
            )}
            {candidate.status === 'conflict' && (() => {
              const existing = activeMemories.find((item) => item.memory_key === candidate.memory_key);
              if (!existing) return null;
              return (
                <section className="memory-section memory-conflict-existing">
                  <h4>已有记忆</h4>
                  <p className="memory-content">{existing.content}</p>
                  <div className="memory-meta">
                    <span>{MEMORY_CATEGORY_LABEL[existing.category] ?? existing.category}</span>
                    <span>置信度 {Math.round(existing.confidence * 100)}%</span>
                    <span>{existing.evidence_count} 条证据</span>
                    <span>更新 {formatTime(existing.updated_at)}</span>
                  </div>
                  <button type="button" className="memory-link" onClick={() => { setCandidate(null); setTab('memories'); void openMemory(existing.id); }}>
                    查看原记忆详情 ›
                  </button>
                </section>
              );
            })()}
            <p className="memory-content">{candidate.content}</p>
            <div className="memory-meta memory-meta-block">
              <span>{MEMORY_CATEGORY_LABEL[candidate.category] ?? candidate.category}</span>
              <span>{candidate.subject || '通用'}</span>
              <span className={`memory-badge ${candidate.explicitness === 'inferred' ? 'inferred' : 'explicit'}`}>
                {explicitnessLabel(candidate.explicitness)}
              </span>
              <span>置信度 {Math.round(candidate.confidence * 100)}%</span>
            </div>

            <section className="memory-section">
              <h4>来源证据</h4>
              {candidate.evidence.length === 0 ? (
                <p className="memory-empty">没有可展示的原始证据。</p>
              ) : (
                <ul className="memory-evidence">
                  {candidate.evidence.map((item, index) => (
                    <li key={`${item.message_id}-${index}`}>
                      <blockquote>{item.quote}</blockquote>
                      <div className="memory-meta">
                        <span>会话 {item.conversation_id || '—'}</span>
                        <span>消息 {item.message_id}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <div className="memory-actions">
              <button
                type="button"
                className="memory-btn primary"
                disabled={busy}
                onClick={() => void run(async () => {
                  const result = await acceptCandidate(candidate.id);
                  if (result.action === 'conflict' || result.action === 'rejected') {
                    throw new Error(result.action === 'conflict' ? '这条候选与已有记忆冲突，需要先处理冲突。' : '这条候选无法被接受（可能对应记忆已被删除或禁止学习）。');
                  }
                  setCandidate(null);
                }, '候选已接受')}
              >{candidate.status === 'conflict' ? '采用新记忆' : '接受'}</button>
              <button
                type="button"
                className="memory-btn"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm(candidate.status === 'conflict' ? '保留原记忆并拒绝这条新候选？' : '拒绝这条候选？')) return;
                  void run(async () => { await rejectCandidate(candidate.id); setCandidate(null); }, '候选已拒绝');
                }}
              >{candidate.status === 'conflict' ? '保留原记忆' : '拒绝'}</button>
            </div>
          </div>
        )}
      </div>
    );
  }

  function renderBlocked() {
    return (
      <div className="memory-section">
        <h3>不再学习</h3>
        <p className="memory-hint">这些内容已被学生要求不再学习。系统保留最小化墓碑，不再召回，也不会自动重建同 key 记忆。</p>
        {blockedMemories.length === 0 ? (
          <p className="memory-empty">当前没有设置为不再学习的内容。</p>
        ) : (
          <ul className="memory-list">
            {blockedMemories.map((item) => (
              <li key={item.id}>
                <div className="memory-blocked-row">
                  <div>
                    <strong>{MEMORY_CATEGORY_LABEL[item.category] ?? item.category} · {item.memory_key}</strong>
                    <p className="memory-meta">设置时间 {formatTime(item.updated_at)} · 状态：不再学习</p>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  function renderJobs() {
    return (
      <div className="memory-jobs">
        <section className="memory-section">
          <h3>整理任务</h3>
          <p className="memory-hint">这里展示系统整理长期记忆的处理状态，不会显示 API Key、堆栈或数据库细节。</p>
          {jobs.length === 0 ? (
            <p className="memory-empty">暂无处理记录。</p>
          ) : (
            <ul className="memory-job-list">
              {jobs.map((job) => (
                <li key={job.id}>
                  <div className="memory-item-head">
                    <strong>{formatTime(job.created_at)}</strong>
                    <span className={`memory-badge job-${job.status}`}>{JOB_STATUS_LABEL[job.status] ?? job.status}</span>
                  </div>
                  <div className="memory-meta">
                    <span>{job.subject || '未识别学科'}</span>
                    <span>尝试 {job.attempts} 次</span>
                    <span>更新 {formatTime(job.updated_at)}</span>
                  </div>
                  {job.status === 'waiting_credentials' && (
                    <p className="memory-warning">需要你在设置中配置有效的模型 API Key，系统会在你下次带 Key 对话时自动恢复。</p>
                  )}
                  {job.last_error && job.status === 'failed' && (
                    <p className="memory-warning">整理失败：系统没有丢弃这轮学习事件，可稍后重试。</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="memory-section">
          <h3>待处理轮次</h3>
          {outbox.length === 0 ? (
            <p className="memory-empty">没有待处理的对话轮次。</p>
          ) : (
            <ul className="memory-job-list">
              {outbox.map((row) => (
                <li key={row.id}>
                  <div className="memory-item-head">
                    <strong>{formatTime(row.created_at)}</strong>
                    <span className={`memory-badge outbox-${row.status}`}>
                      {row.status === 'awaiting_assistant' ? '等待最终回答' : row.status === 'done' ? '已创建整理任务' : '已停止自动处理'}
                    </span>
                  </div>
                  <div className="memory-meta">
                    <span>{row.subject || '未识别学科'}</span>
                    {row.abandoned_reason && <span>停止原因：{row.abandoned_reason}</span>}
                    {row.restore_count > 0 && <span>已恢复 {row.restore_count} 次</span>}
                  </div>
                  {row.status === 'abandoned' && (
                    <button
                      type="button"
                      className="memory-btn"
                      disabled={busy}
                      onClick={() => void run(async () => { await restoreOutbox(row.id); }, '已重新尝试；若已有最终回答，系统会补建整理任务')}
                    >重新尝试</button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    );
  }

  return (
    <div className="view-inner settings-page memory-page">
      <div className="view-head"><h2>学生记忆</h2></div>
      <div className="settings-shell">
        <nav className="panel settings-nav" aria-label="学生记忆目录">
          {TABS.map((item) => (
            <button
              type="button"
              key={item.key}
              className={tab === item.key ? 'settings-nav-item memory-tab active' : 'settings-nav-item memory-tab'}
              onClick={() => setTab(item.key)}
            >
              <span>{item.label}</span>
              {item.key === 'pending' && pending.length + conflicts.length > 0 && (
                <span className="memory-count">{pending.length + conflicts.length}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="settings-content memory-content-main">
        <section className="panel memory-panel">
          <header className="memory-main-head">
            <div>
              <h3>我的长期学习记忆</h3>
              <p>系统从你的长期学习互动中整理出的记忆；推断内容会明确标注，不会当成你亲口说过的话。</p>
            </div>
            <button type="button" className="memory-btn" onClick={() => void loadAll()} disabled={loading}>刷新</button>
          </header>

          {notice && <div className="memory-notice">{notice}</div>}
          {error && (
            <div className="memory-error">
              {error}
              <button type="button" onClick={() => void loadAll()}>重试</button>
            </div>
          )}

        {loading ? (
          <div className="memory-skeleton">
            <div /><div /><div />
          </div>
        ) : (
          <>
            {tab === 'overview' && renderOverview()}
            {tab === 'memories' && (
              <div className="memory-split">
                {renderMemoryList()}
                {renderDetail()}
              </div>
            )}
            {tab === 'pending' && renderPending()}
            {tab === 'blocked' && renderBlocked()}
            {tab === 'jobs' && renderJobs()}
          </>
        )}
        </section>
        </div>
      </div>
    </div>
  );
}
