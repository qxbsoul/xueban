import { useEffect, useMemo, useRef, useState } from 'react';
import {
  JOB_STATUS_LABEL,
  MEMORY_CATEGORY_LABEL,
  acceptCandidate,
  archiveMemory,
  blockMemory,
  correctMemory,
  deleteMemory,
  getMemory,
  getMemoryByKey,
  getMemoryStats,
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
  type MemoryStats,
} from '../memory-api';
import { useWorkbench } from '../store';
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

export default function MemoryCenterView({ active = true }: { active?: boolean }) {
  const { openConversation } = useWorkbench();
  const PAGE_SIZE = 50;
  const [tab, setTab] = useState<Tab>('overview');
  const [activeMemories, setActiveMemories] = useState<MemoryItem[]>([]);
  const [archivedMemories, setArchivedMemories] = useState<MemoryItem[]>([]);
  const [blockedMemories, setBlockedMemories] = useState<MemoryItem[]>([]);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [pending, setPending] = useState<CandidateItem[]>([]);
  const [conflicts, setConflicts] = useState<CandidateItem[]>([]);
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [outbox, setOutbox] = useState<MemoryOutboxRow[]>([]);

  const [listLoading, setListLoading] = useState<{ active: boolean; archived: boolean }>({ active: false, archived: false });
  const [memoryError, setMemoryError] = useState('');
  const [candidateLoading, setCandidateLoading] = useState(false);
  const [candidateError, setCandidateError] = useState('');
  const [jobLoading, setJobLoading] = useState(false);
  const [jobError, setJobError] = useState('');
  const [actionError, setActionError] = useState('');

  const [hasMore, setHasMore] = useState<{ active: boolean; archived: boolean }>({ active: false, archived: false });
  const [notice, setNotice] = useState('');
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [editReason, setEditReason] = useState('');
  const [candidate, setCandidate] = useState<CandidateItem | null>(null);
  const [conflictExisting, setConflictExisting] = useState<MemoryDetail | null>(null);
  const [conflictLoading, setConflictLoading] = useState(false);
  const [conflictError, setConflictError] = useState('');
  const [candidateEvidenceExpanded, setCandidateEvidenceExpanded] = useState(false);
  const [busy, setBusy] = useState(false);

  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [subject, setSubject] = useState('');
  const [category, setCategory] = useState('');
  const [explicitness, setExplicitness] = useState('');
  const [listStatus, setListStatus] = useState<'active' | 'archived'>('active');
  const [sort, setSort] = useState<'updated' | 'created'>('updated');

  const aliveRef = useRef(true);
  const loadedOnceRef = useRef(false);
  const lastLoadedRef = useRef(0);
  const requestRef = useRef<{ active: number; archived: number }>({ active: 0, archived: 0 });
  const appendLockRef = useRef<{ active: boolean; archived: boolean }>({ active: false, archived: false });

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function loadStats() {
    try {
      const next = await getMemoryStats();
      if (aliveRef.current) setStats(next);
    } catch (err) {
      if (aliveRef.current) setMemoryError(err instanceof Error ? err.message : '加载记忆统计失败');
    }
  }

  async function loadList(status: 'active' | 'archived', append = false) {
    if (append && appendLockRef.current[status]) return;
    if (append) appendLockRef.current[status] = true;
    const sequence = (requestRef.current[status] += 1);
    if (!append) setMemoryError('');
    setListLoading((previous) => ({ ...previous, [status]: true }));
    try {
      const current = status === 'active' ? activeMemories : archivedMemories;
      const last = current[current.length - 1];
      const cursor = append && last ? `${sort === 'created' ? last.created_at : last.updated_at}|${last.id}` : undefined;
      const items = await listMemories({
        status,
        q: search || undefined,
        subject: subject || undefined,
        category: category || undefined,
        explicitness: explicitness || undefined,
        sort,
        limit: PAGE_SIZE,
        cursor,
      });
      if (!aliveRef.current || sequence !== requestRef.current[status]) return;
      const setter = status === 'active' ? setActiveMemories : setArchivedMemories;
      setter((previous) => {
        if (!append) return items;
        const seen = new Set(previous.map((item) => item.id));
        return [...previous, ...items.filter((item) => !seen.has(item.id))];
      });
      setHasMore((previous) => ({ ...previous, [status]: items.length === PAGE_SIZE }));
    } catch (err) {
      if (aliveRef.current && sequence === requestRef.current[status]) {
        setMemoryError(err instanceof Error ? err.message : '加载记忆列表失败');
      }
    } finally {
      appendLockRef.current[status] = false;
      if (aliveRef.current && sequence === requestRef.current[status]) {
        setListLoading((previous) => ({ ...previous, [status]: false }));
      }
    }
  }

  async function loadCandidates() {
    setCandidateLoading(true);
    setCandidateError('');
    try {
      const [pendingItems, conflictItems] = await Promise.all([
        listCandidates('pending'),
        listCandidates('conflict'),
      ]);
      if (!aliveRef.current) return;
      setPending(pendingItems);
      setConflicts(conflictItems);
    } catch (err) {
      if (aliveRef.current) setCandidateError(err instanceof Error ? err.message : '加载候选记忆失败');
    } finally {
      if (aliveRef.current) setCandidateLoading(false);
    }
  }

  async function loadJobs() {
    setJobLoading(true);
    setJobError('');
    try {
      const [jobItems, outboxItems] = await Promise.all([listJobs(), listOutbox()]);
      if (!aliveRef.current) return;
      setJobs(jobItems);
      setOutbox(outboxItems);
    } catch (err) {
      if (aliveRef.current) setJobError(err instanceof Error ? err.message : '加载处理记录失败');
    } finally {
      if (aliveRef.current) setJobLoading(false);
    }
  }

  async function loadBlocked() {
    try {
      const items = await listMemories({ status: 'blocked', limit: 200 });
      if (aliveRef.current) setBlockedMemories(items);
    } catch (err) {
      if (aliveRef.current) setMemoryError(err instanceof Error ? err.message : '加载不再学习列表失败');
    }
  }

  function refreshAll() {
    lastLoadedRef.current = Date.now();
    loadedOnceRef.current = true;
    void loadStats();
    void loadList(listStatus, false);
    void loadCandidates();
    void loadJobs();
    void loadBlocked();
  }

  useEffect(() => {
    if (!active) return;
    if (!loadedOnceRef.current) {
      refreshAll();
      return;
    }
    // 切回记忆页时，数据超过 60 秒才自动刷新，避免每次切换都打 7 个接口。
    if (Date.now() - lastLoadedRef.current > 60_000) refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  useEffect(() => {
    if (!active || !loadedOnceRef.current) return;
    // 筛选条件变化：先重置对应列表与分页状态，再按新条件加载第一页。
    if (listStatus === 'active') setActiveMemories([]);
    else setArchivedMemories([]);
    setHasMore((previous) => ({ ...previous, [listStatus]: false }));
    void loadList(listStatus, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, subject, category, explicitness, sort, listStatus]);

  useEffect(() => {
    if (!active) return;
    if (tab === 'blocked' && blockedMemories.length === 0) void loadBlocked();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, tab]);

  useEffect(() => {
    setCandidateEvidenceExpanded(false);
  }, [candidate?.id]);

  useEffect(() => {
    let cancelled = false;
    setConflictExisting(null);
    setConflictError('');
    if (!candidate || candidate.status !== 'conflict') {
      setConflictLoading(false);
      return () => { cancelled = true; };
    }
    setConflictLoading(true);
    void (async () => {
      try {
        const memory = await getMemoryByKey(candidate.memory_key);
        const detail = await getMemory(memory.id);
        if (!cancelled) setConflictExisting(detail);
      } catch (err) {
        if (!cancelled) setConflictError(err instanceof Error ? err.message : '加载原记忆失败');
      } finally {
        if (!cancelled) setConflictLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate?.id, candidate?.memory_key, candidate?.status]);

  async function jumpToEvidence(conversationId?: string, messageId?: string) {
    if (!conversationId) {
      setActionError('这条证据没有记录来源会话，无法跳转。');
      return;
    }
    try {
      const conversation = await openConversation(conversationId);
      if (!conversation) {
        setActionError('原对话不存在或已被删除，无法跳转。');
        return;
      }
      if (messageId) {
        sessionStorage.setItem('sf_scroll_to_message', JSON.stringify({ conversationId, messageId, createdAt: Date.now() }));
        window.dispatchEvent(new CustomEvent('sf:scroll-to-message'));
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '打开原对话失败');
    }
  }

  function evidenceJump(conversationId?: string, messageId?: string) {
    if (!conversationId) return null;
    return (
      <button type="button" className="memory-link" onClick={() => void jumpToEvidence(conversationId, messageId)}>
        查看原对话 ›
      </button>
    );
  }

  async function openMemory(id: string) {
    setDetailLoading(true);
    setActionError('');
    try {
      const result = await getMemory(id);
      setDetail(result);
      setDraft(result.memory.content);
      setEditReason('');
      setEditing(false);
      setCandidate(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '加载记忆详情失败');
    } finally {
      setDetailLoading(false);
    }
  }

  async function run(action: () => Promise<void>, successText: string) {
    if (busy) return;
    setBusy(true);
    setActionError('');
    try {
      await action();
      setNotice(successText);
      loadedOnceRef.current = true;
      lastLoadedRef.current = Date.now();
      await Promise.allSettled([loadStats(), loadList(listStatus, false), loadCandidates(), loadJobs(), loadBlocked()]);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '操作失败');
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

  const latestUpdate = stats?.latest_update ?? 0;
  const memoryLoading = listLoading[listStatus];
  const initialLoading = listLoading.active && !stats && activeMemories.length === 0;

  function renderOverview() {
    const pendingCount = pending.length;
    const conflictCount = conflicts.length;
    return (
      <div className="memory-overview">
        <div className="memory-stat-grid">
          <div className="memory-stat"><span>正式记忆</span><strong>{stats?.active ?? 0}</strong></div>
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
              const count = stats?.by_category[key] ?? 0;
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
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
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
        <p className="memory-hint">搜索与筛选由服务端执行，并作用于全部记忆；列表按页加载。</p>

        {sourceMemories.length === 0 ? (
          <p className="memory-empty">{search || subject || category || explicitness ? '没有符合条件的记忆。' : '这个分类下还没有记忆。'}</p>
        ) : (
          <ul className="memory-list">
            {sourceMemories.map((item) => (
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
        {hasMore[listStatus] && (
          <div className="memory-load-more">
            <button type="button" className="memory-btn" disabled={memoryLoading} onClick={() => void loadList(listStatus, true)}>
              {memoryLoading ? '加载中…' : '加载更多'}
            </button>
          </div>
        )}
        {!hasMore[listStatus] && sourceMemories.length > 0 && (
          <p className="memory-hint memory-end">已加载全部记忆。</p>
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
                  {evidenceJump(item.conversation_id, item.message_id)}
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
            {candidate.status === 'conflict' && (
              <section className="memory-section memory-conflict-compare">
                <h4>新旧记忆对照</h4>
                {conflictLoading && <p className="memory-empty">正在加载原记忆…</p>}
                {conflictError && <p className="memory-error">{conflictError}</p>}
                {conflictExisting ? (
                  <>
                    <div className="memory-compare-grid">
                      <div className="memory-compare-col">
                        <header><strong>原记忆</strong><span>v{conflictExisting.memory.version}</span></header>
                        <p className="memory-content">{conflictExisting.memory.content}</p>
                        <div className="memory-meta">
                          <span>{explicitnessLabel(conflictExisting.memory.explicitness)}</span>
                          <span>置信度 {Math.round(conflictExisting.memory.confidence * 100)}%</span>
                          <span>证据 {conflictExisting.memory.evidence_count} 条</span>
                          <span>更新 {formatTime(conflictExisting.memory.updated_at)}</span>
                        </div>
                        <ul className="memory-evidence">
                          {conflictExisting.evidence.slice(0, 3).map((item) => (
                            <li key={item.id}>
                              <blockquote>{item.quote}</blockquote>
                              <div className="memory-meta">
                                <span>会话 {item.conversation_id || '—'}</span>
                                <span>消息 {item.message_id || '—'}</span>
                                <span>证据时间 {formatTime(item.created_at)}</span>
                              </div>
                              {evidenceJump(item.conversation_id, item.message_id)}
                            </li>
                          ))}
                          {conflictExisting.evidence.length === 0 && <li><p className="memory-empty">没有可展示的证据。</p></li>}
                        </ul>
                      </div>
                      <div className="memory-compare-col next">
                        <header><strong>新候选</strong><span>待确认</span></header>
                        <p className="memory-content">{candidate.content}</p>
                        <div className="memory-meta">
                          <span>{explicitnessLabel(candidate.explicitness)}</span>
                          <span>置信度 {Math.round(candidate.confidence * 100)}%</span>
                          <span>证据 {candidate.evidence.length} 条</span>
                          <span>提取 {formatTime(candidate.created_at)}</span>
                        </div>
                        <ul className="memory-evidence">
                          {(candidateEvidenceExpanded ? candidate.evidence : candidate.evidence.slice(0, 3)).map((item, index) => (
                            <li key={`${item.message_id}-${index}`}>
                              <blockquote>{item.quote}</blockquote>
                              <div className="memory-meta">
                                <span>会话 {item.conversation_id || '—'}</span>
                                <span>消息 {item.message_id || '—'}</span>
                                <span>证据时间 {formatTime(item.created_at ?? candidate.created_at)}</span>
                              </div>
                              {evidenceJump(item.conversation_id, item.message_id)}
                            </li>
                          ))}
                          {candidate.evidence.length === 0 && <li><p className="memory-empty">没有可展示的证据。</p></li>}
                        </ul>
                        {candidate.evidence.length > 3 && (
                          <button type="button" className="memory-link" onClick={() => setCandidateEvidenceExpanded((value) => !value)}>
                            {candidateEvidenceExpanded ? '收起证据' : `展开全部 ${candidate.evidence.length} 条证据`}
                          </button>
                        )}
                        <p className="memory-meta">提取时间 {formatTime(candidate.created_at)}</p>
                      </div>
                    </div>
                    <button type="button" className="memory-link" onClick={() => { setCandidate(null); setTab('memories'); void openMemory(conflictExisting.memory.id); }}>
                      查看原记忆完整详情 ›
                    </button>
                  </>
                ) : (!conflictLoading && !conflictError && (
                  <p className="memory-empty">没有找到对应的原记忆，可能已被删除或归档。</p>
                ))}
              </section>
            )}
            {candidate.status !== 'conflict' && (
              <>
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
                            <span>证据时间 {formatTime(item.created_at ?? candidate.created_at)}</span>
                          </div>
                          {evidenceJump(item.conversation_id, item.message_id)}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              </>
            )}

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
        <p className="memory-hint">这些内容已被学生要求不再学习。系统不再召回，也不会自动重建同类记忆。</p>
        {blockedMemories.length === 0 ? (
          <p className="memory-empty">当前没有设置为不再学习的内容。</p>
        ) : (
          <ul className="memory-list">
            {blockedMemories.map((item) => (
              <li key={item.id}>
                <div className="memory-blocked-row">
                  <div>
                    <strong>{MEMORY_CATEGORY_LABEL[item.category] ?? item.category} · 已设置禁止学习</strong>
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
            <button type="button" className="memory-btn" onClick={() => refreshAll()} disabled={initialLoading}>刷新</button>
          </header>

          {notice && <div className="memory-notice">{notice}</div>}
          {actionError && (
            <div className="memory-error">
              {actionError}
              <button type="button" onClick={() => setActionError('')}>关闭</button>
            </div>
          )}
          {memoryError && (tab === 'overview' || tab === 'memories' || tab === 'blocked') && (
            <div className="memory-error">
              {memoryError}
              <button type="button" onClick={() => { void loadStats(); void loadList(listStatus); void loadBlocked(); }}>重试</button>
            </div>
          )}
          {candidateError && tab === 'pending' && (
            <div className="memory-error">
              {candidateError}
              <button type="button" onClick={() => void loadCandidates()}>重试</button>
            </div>
          )}
          {jobError && tab === 'jobs' && (
            <div className="memory-error">
              {jobError}
              <button type="button" onClick={() => void loadJobs()}>重试</button>
            </div>
          )}

        {initialLoading ? (
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
            {tab === 'pending' && (candidateLoading && pending.length === 0 ? <p className="memory-empty">加载候选记忆…</p> : renderPending())}
            {tab === 'blocked' && renderBlocked()}
            {tab === 'jobs' && (jobLoading && jobs.length === 0 ? <p className="memory-empty">加载处理记录…</p> : renderJobs())}
          </>
        )}
        </section>
        </div>
      </div>
    </div>
  );
}
