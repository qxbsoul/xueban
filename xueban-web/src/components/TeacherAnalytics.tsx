import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../api';
import './teacher-analytics.css';

type Row = Record<string, any>;

const SECTIONS: Array<{ id: string; label: string; hint: string }> = [
  { id: 'overview', label: '总体概览', hint: '任务、班级、知识点与证据质量的汇总' },
  { id: 'class', label: '班级学情', hint: '知识点掌握分布、题目表现与班级比较' },
  { id: 'student', label: '学生画像', hint: '按统一数据范围查看单个学生' },
  { id: 'knowledge', label: '知识点分析', hint: '知识点 × 学生掌握矩阵' },
  { id: 'errors', label: '错误分析', hint: '错误类型、题目与标签覆盖' },
  { id: 'trend', label: '学习趋势', hint: '跨任务可比性与 EWMA' },
  { id: 'suggest', label: '教学建议', hint: '由证据与语义分析推导' },
];

const STATUS_ORDER = ['熟练掌握', '基本掌握', '初步掌握', '掌握不足', '证据不足'];
const STATUS_CLASS: Record<string, string> = { 熟练掌握: 'st-5', 基本掌握: 'st-4', 初步掌握: 'st-3', 掌握不足: 'st-2', 证据不足: 'st-1' };
const STATUS_COLOR: Record<string, string> = { 熟练掌握: '#15803d', 基本掌握: '#65a30d', 初步掌握: '#d97706', 掌握不足: '#dc2626', 证据不足: '#cbd5e1' };
const TASK_LABEL: Record<string, string> = { pending: '排队中', running: '生成中', completed: '已完成', partial: '部分完成', failed: '失败' };
const ANSWER_STATUS_LABEL: Record<string, string> = { graded: '有效判题', needs_review: '待复核', failed: '失败', pending: '未判题', not_answered: '未作答', manual_corrected: '人工修正', manual_confirmed: '人工确认' };
const TASK_CLASS: Record<string, string> = { pending: 'warn', running: 'warn', completed: 'ok', partial: 'mid', failed: 'bad' };

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException ? error.name === 'AbortError' : (error as Row)?.name === 'AbortError';
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await apiFetch(path, { signal });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error((body && (body.detail || body.message)) || `HTTP ${response.status}`);
  return body as T;
}

function useAbortableRequest() {
  const controllers = useRef<Record<string, AbortController>>({});
  useEffect(() => () => { Object.values(controllers.current).forEach(controller => controller.abort()); }, []);
  return useCallback(async <T,>(key: string, path: string): Promise<T | undefined> => {
    controllers.current[key]?.abort();
    const controller = new AbortController();
    controllers.current[key] = controller;
    try {
      return await getJson<T>(path, controller.signal);
    } catch (error) {
      if (isAbortError(error)) return undefined;
      throw error;
    }
  }, []);
}

const pct = (value: number | null | undefined) => (value == null ? '—' : `${Math.round(value * 100)}%`);
const pp = (value: number | null | undefined) => (value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(1)}pp`);

export function TeacherAnalytics({ setMessage }: { setMessage: (value: string) => void }) {
  const [section, setSection] = useState('overview');
  const [batches, setBatches] = useState<Row[]>([]);
  const [scope, setScope] = useState<'single' | 'all' | 'multi'>('single');
  const [batchId, setBatchId] = useState('');
  const [selectedBatches, setSelectedBatches] = useState<string[]>([]);
  const [classes, setClasses] = useState<Row[]>([]);
  const [classId, setClassId] = useState('');
  const [studentId, setStudentId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [overview, setOverview] = useState<Row | null>(null);
  const [trace, setTrace] = useState<Row | null>(null);
  const [classProfile, setClassProfile] = useState<Row | null>(null);
  const [studentProfile, setStudentProfile] = useState<Row | null>(null);
  const [matrix, setMatrix] = useState<Row | null>(null);
  const [errors, setErrors] = useState<Row | null>(null);
  const [trend, setTrend] = useState<Row | null>(null);
  const [suggestions, setSuggestions] = useState<Row | null>(null);
  const [compare, setCompare] = useState<Row | null>(null);
  const [tasks, setTasks] = useState<Row | null>(null);
  const [llmResult, setLlmResult] = useState<Row | null>(null);
  const [taskBusy, setTaskBusy] = useState(false);
  const [ladder, setLadder] = useState<{ kind: 'students'; kp: Row; status: string } | { kind: 'kp'; kpId: string; name: string } | null>(null);
  const [kpDetail, setKpDetail] = useState<Row | null>(null);
  const [questionId, setQuestionId] = useState('');
  const [questionDetail, setQuestionDetail] = useState<Row | null>(null);
  const request = useAbortableRequest();

  const effectiveBatch = batchId || batches[0]?.id || '';
  const effectiveBatches = useMemo(
    () => (scope === 'single' ? [effectiveBatch].filter(Boolean) : (selectedBatches.length ? selectedBatches : batches.map(item => item.id))),
    [scope, effectiveBatch, selectedBatches, batches],
  );
  const scopeLabel = scope === 'single'
    ? `单次任务 · ${batches.find(item => item.id === effectiveBatch)?.filename || '—'}`
    : `${scope === 'all' ? '全部任务' : '多次任务'} · ${effectiveBatches.length} 个`;

  const run = useCallback(async (task: () => Promise<void>) => {
    setLoading(true); setError('');
    try { await task(); }
    catch (err) { setError(String(err)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void run(async () => {
      const data = await request<{ items: Row[] }>('batches', '/api/teacher/batches');
      if (!data) return;
      setBatches(data.items || []);
      setBatchId(current => current || data.items?.[0]?.id || '');
      setSelectedBatches(current => current.length ? current : (data.items || []).map(item => item.id));
    });
  }, [request, run]);

  useEffect(() => {
    if (!effectiveBatch) return;
    void run(async () => {
      const data = await request<{ classes: Row[] }>('classes', `/api/teacher/analysis/classes?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (!data) return;
      setClasses(data.classes || []);
      setClassId(current => ((data.classes || []).some(item => item.id === current) ? current : (data.classes?.[0]?.id || '')));
    });
  }, [effectiveBatch, request, run]);

  const loadTasks = useCallback(async () => {
    if (!effectiveBatch) return;
    const data = await request<Row>('tasks', `/api/teacher/analysis/tasks?batch_id=${encodeURIComponent(effectiveBatch)}`);
    if (data) setTasks(data);
    return data;
  }, [effectiveBatch, request]);

  const triggerAnalysis = useCallback(async (force = false, kind: 'deterministic' | 'llm' = 'deterministic') => {
    if (!effectiveBatch) return;
    setTaskBusy(true);
    try {
      const response = await apiFetch('/api/teacher/analysis/tasks/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ batch_id: effectiveBatch, scope: 'batch', kind, force }) });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
      await loadTasks();
      setMessage(kind === 'llm' ? '语义分析任务已受理，完成后会显示引用证据的解释。' : (body.reused ? '已有同版本分析结果，直接复用。' : '分析任务已受理。'));
    } catch (err) { setError(String(err)); }
    finally { setTaskBusy(false); }
  }, [effectiveBatch, loadTasks, setMessage]);

  // 页面打开/切换任务：有结果就复用，没有或已过期才自动生成（不会每次点击都重算）。
  useEffect(() => {
    if (!effectiveBatch) return;
    void (async () => {
      const data = await loadTasks().catch(() => null);
      if (!data) return;
      const deterministic = data.latest?.deterministic;
      if (!deterministic || deterministic.stale) void triggerAnalysis(false);
    })();
  }, [effectiveBatch, loadTasks, triggerAnalysis]);

  // 任务执行中轮询真实状态
  useEffect(() => {
    const running = (tasks?.tasks || []).some((task: Row) => task.status === 'pending' || task.status === 'running');
    if (!running) return;
    const timer = window.setInterval(() => { void loadTasks(); }, 2000);
    return () => window.clearInterval(timer);
  }, [tasks, loadTasks]);

  const llmTaskId = tasks?.latest?.llm?.status === 'completed' ? tasks.latest.llm.id : '';
  useEffect(() => {
    if (!llmTaskId) { setLlmResult(null); return; }
    void (async () => {
      const data = await request<Row>('llmDetail', `/api/teacher/analysis/tasks/${encodeURIComponent(llmTaskId)}`).catch(() => undefined);
      if (data?.result?.llm) setLlmResult(data.result);
    })();
  }, [llmTaskId, request]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'overview') return;
    void run(async () => {
      const data = await request<Row>('overview', `/api/teacher/analysis/overview?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setOverview(data);
    });
  }, [section, effectiveBatch, request, run]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'overview') return;
    void run(async () => {
      const data = await request<Row>('trace', `/api/teacher/analysis/evidence-trace?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setTrace(data);
    });
  }, [section, effectiveBatch, request, run]);

  useEffect(() => {
    if (!effectiveBatch || (section !== 'class' && section !== 'student') || !classId) return;
    void run(async () => {
      const query = `/api/teacher/analysis/classes/${encodeURIComponent(classId)}?batch_id=${encodeURIComponent(effectiveBatch)}${scope !== 'single' ? `&scope=multi&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}` : ''}`;
      const data = await request<Row>('classProfile', query);
      if (data) setClassProfile(data);
    });
  }, [section, effectiveBatch, classId, scope, effectiveBatches.join(','), request, run]);

  // 班级比较：2 个以上班级时自动加载（同一任务内）
  useEffect(() => {
    if (!effectiveBatch || section !== 'class' || classes.length < 2) { setCompare(null); return; }
    void run(async () => {
      const data = await request<Row>('compare', `/api/teacher/analysis/compare-detailed?batch_id=${encodeURIComponent(effectiveBatch)}&class_ids=${encodeURIComponent(classes.map(item => item.id).join(','))}`);
      if (data) setCompare(data);
    });
  }, [section, effectiveBatch, classes, request, run]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'knowledge') return;
    void run(async () => {
      const data = await request<Row>('matrix', `/api/teacher/analysis/knowledge-matrix?batch_id=${encodeURIComponent(effectiveBatch)}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}`);
      if (data) setMatrix(data);
    });
  }, [section, effectiveBatch, classId, request, run]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'errors') return;
    void run(async () => {
      const data = await request<Row>('errors', `/api/teacher/analysis/errors?batch_id=${encodeURIComponent(effectiveBatch)}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}`);
      if (data) setErrors(data);
    });
  }, [section, effectiveBatch, classId, request, run]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'trend' || !classId) return;
    void run(async () => {
      const data = await request<Row>('classTrend', `/api/teacher/analysis/trend/class/${encodeURIComponent(classId)}?batch_ids=${encodeURIComponent(effectiveBatches.join(','))}`);
      if (data) setTrend(data);
    });
  }, [section, effectiveBatch, classId, effectiveBatches.join(','), request, run]);

  useEffect(() => {
    if (!effectiveBatch || section !== 'suggest') return;
    void run(async () => {
      const data = await request<Row>('suggest', `/api/teacher/analysis/suggestions?batch_id=${encodeURIComponent(effectiveBatch)}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}`);
      if (data) setSuggestions(data);
    });
  }, [section, effectiveBatch, classId, request, run]);

  // 学生画像：跟随全局数据范围，不再单独维护「本次/全部」开关
  useEffect(() => {
    if (section !== 'student' || !studentId || !effectiveBatch) return;
    void run(async () => {
      const query = scope === 'single'
        ? `batch_id=${encodeURIComponent(effectiveBatch)}&scope=single`
        : `batch_id=${encodeURIComponent(effectiveBatch)}&scope=all&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}`;
      const data = await request<Row>('student', `/api/teacher/analysis/students/${encodeURIComponent(studentId)}?${query}`);
      if (data) setStudentProfile(data);
    });
  }, [section, studentId, scope, effectiveBatch, effectiveBatches.join(','), request, run]);

  const openKp = async (kpId: string, name: string) => {
    setLadder({ kind: 'kp', kpId, name });
    setKpDetail(null);
    await run(async () => {
      const data = await request<Row>('kpDetail', `/api/teacher/analysis/classes/${encodeURIComponent(classId)}/knowledge/${encodeURIComponent(kpId)}?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setKpDetail(data);
    });
  };

  const openQuestion = async (qid: string) => {
    setQuestionId(qid);
    setQuestionDetail(null);
    await run(async () => {
      const data = await request<Row>('questionDetail', `/api/teacher/analysis/classes/${encodeURIComponent(classId)}/questions/${encodeURIComponent(qid)}?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setQuestionDetail(data);
    });
  };

  const students = useMemo(() => (classProfile?.students || []).map((item: Row) => item.student), [classProfile]);
  const selectSection = (id: string) => { setSection(id); setError(''); setLadder(null); setQuestionId(''); };
  const deterministic = tasks?.latest?.deterministic;
  const llm = tasks?.latest?.llm;

  return <div className="ta-shell">
    <aside className="ta-nav" aria-label="学情分析二级导航">
      <div className="ta-nav-title">学情分析</div>
      {SECTIONS.map(item => <button key={item.id} className={`ta-nav-item ${section === item.id ? 'on' : ''}`} onClick={() => selectSection(item.id)} title={item.hint}>{item.label}</button>)}
      <div className="ta-nav-foot"><span className={`ta-dot ${loading ? 'busy' : ''}`} />{loading ? '加载中' : '已就绪'}</div>
    </aside>

    <section className="ta-main">
      <header className="ta-head">
        <div>
          <div className="ta-crumbs"><button onClick={() => selectSection('overview')}>学情分析</button><em>/</em><strong>{SECTIONS.find(item => item.id === section)?.label}</strong></div>
          <h1>{SECTIONS.find(item => item.id === section)?.label}</h1>
          <p>{SECTIONS.find(item => item.id === section)?.hint}</p>
        </div>
        <div className="ta-scope">
          <div className="ta-scope-row">
            <label>数据范围</label>
            <select value={scope} onChange={event => { setScope(event.target.value as 'single' | 'all' | 'multi'); setStudentProfile(null); }}>
              <option value="single">单次任务</option>
              <option value="all">全部任务</option>
              <option value="multi">多次任务</option>
            </select>
            {scope === 'single' && <select value={effectiveBatch} onChange={event => { setBatchId(event.target.value); setStudentProfile(null); setClassProfile(null); }}>
              {batches.map(item => <option key={item.id} value={item.id}>{item.filename || item.id}</option>)}
            </select>}
          </div>
          {scope !== 'single' && <div className="ta-batch-picker">
            {batches.map(item => <label key={item.id}><input type="checkbox" checked={selectedBatches.includes(item.id)} onChange={() => setSelectedBatches(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} /><span>{item.filename || item.id}</span></label>)}
            <small>{scopeLabel} · 仅合并通过可比性校验的任务</small>
          </div>}
          <div className="ta-scope-row">
            <label>班级</label>
            <select value={classId} onChange={event => { setClassId(event.target.value); setStudentId(''); setStudentProfile(null); }}>
              {classes.length === 0 && <option value="">暂无班级</option>}
              {classes.map(item => <option key={item.id} value={item.id}>{item.code}</option>)}
            </select>
            {section === 'student' && <select value={studentId} onChange={event => setStudentId(event.target.value)}>
              <option value="">选择学生</option>
              {students.map((student: Row) => <option key={student.id} value={student.id}>{student.code}</option>)}
            </select>}
          </div>
        </div>
      </header>

      <AnalysisStatusBar deterministic={deterministic} llm={llm} busy={taskBusy} onRun={() => void triggerAnalysis(true)} onRunLlm={() => void triggerAnalysis(true, 'llm')} />
      {error && <div className="ta-error">读取失败：{error}<button onClick={() => selectSection(section)}>重试</button></div>}
      {!effectiveBatch && <div className="ta-empty">还没有可分析的任务，请先在「数据管理」导入 CSV。</div>}

      {section === 'overview' && overview && <Overview data={overview} trace={trace} onOpenClass={(id) => { setClassId(id); selectSection('class'); }} />}
      {section === 'class' && classProfile && <ClassPanel
        data={classProfile}
        classes={classes}
        compare={compare}
        onOpenStudent={(id) => { setStudentId(id); selectSection('student'); }}
        onOpenQuestion={(qid) => void openQuestion(qid)}
        onOpenStatus={(kp, status) => setLadder({ kind: 'students', kp, status })}
        onOpenKp={(kp) => void openKp(kp.knowledge_point_id, kp.name)}
      />}
      {section === 'student' && <StudentPanel data={studentProfile} studentId={studentId} scope={scope} />}
      {section === 'knowledge' && matrix && <KnowledgePanel data={matrix} />}
      {section === 'errors' && errors && <ErrorsPanel data={errors} />}
      {section === 'trend' && <TrendPanel data={trend} />}
      {section === 'suggest' && <SuggestPanel data={suggestions} tasks={tasks} llm={llmResult} />}
      {section === 'class' && !classProfile && !loading && <div className="ta-empty">请选择班级。</div>}
      {section === 'student' && !studentId && <div className="ta-empty">请选择学生。</div>}

      {(ladder || questionId) && <button className="ta-backdrop" aria-label="关闭详情" onClick={() => { setLadder(null); setQuestionId(''); }} />}
      {ladder?.kind === 'students' && <StudentLadder kp={ladder.kp} status={ladder.status} onClose={() => setLadder(null)} onOpenStudent={(id) => { setStudentId(id); setLadder(null); selectSection('student'); }} />}
      {ladder?.kind === 'kp' && <KpDrawer data={kpDetail} name={ladder.name} onClose={() => setLadder(null)} onOpenQuestion={(qid) => void openQuestion(qid)} />}
      {questionId && <QuestionDrawer data={questionDetail} questionId={questionId} onClose={() => setQuestionId('')} />}
    </section>
  </div>;
}

function AnalysisStatusBar({ deterministic, llm, busy, onRun, onRunLlm }: { deterministic?: Row; llm?: Row; busy: boolean; onRun: () => void; onRunLlm: () => void }) {
  const status = deterministic?.status || 'pending';
  const progress = status === 'running' && typeof deterministic?.progress === 'number' ? ` ${Math.round(deterministic.progress * 100)}%` : '';
  return <div className={`ta-task-bar ${TASK_CLASS[status] || 'warn'}`}>
    <div className="ta-task-item"><span>确定性分析</span><b>{TASK_LABEL[status] || status}{progress}</b>{deterministic?.stale && <em>数据已变化，需要重新生成</em>}{deterministic?.error && <em>{deterministic.error}</em>}</div>
    <div className="ta-task-item"><span>语义分析（LLM）</span><b>{llm ? (TASK_LABEL[llm.status] || llm.status) : '未生成'}</b>{llm?.error && <em>{llm.error}</em>}</div>
    <button disabled={busy || status === 'running'} onClick={onRun}>{status === 'running' ? '生成中…' : (deterministic?.stale ? '重新生成分析' : '刷新分析')}</button>
    <button disabled={busy || llm?.status === 'running'} onClick={onRunLlm}>{llm?.status === 'running' ? '生成中…' : (llm ? '重新生成语义分析' : '生成语义分析')}</button>
  </div>;
}

function Stats({ items }: { items: Array<[string, unknown]> }) {
  return <div className="ta-stats">{items.map(([label, value]) => <div key={label}><b>{value == null || value === '' ? '—' : String(value)}</b><span>{label}</span></div>)}</div>;
}

function Overview({ data, trace, onOpenClass }: { data: Row; trace: Row | null; onOpenClass: (id: string) => void }) {
  const quality = data.data_quality || {};
  const performance = data.question_performance || {};
  const summary = performance.summary || {};
  return <>
    <Stats items={[['班级', data.classes?.length ?? 0], ['有效学生', data.classes?.reduce((sum: number, item: Row) => sum + (item.valid_student_count || 0), 0)], ['可分析知识点', data.knowledge_points?.length ?? 0], ['应作答', summary.expected ?? '—'], ['实际作答', summary.answered ?? '—'], ['未作答', summary.not_answered ?? '—']]} />
    <div className="ta-grid">
      <div className="ta-card">
        <h2>班级概览</h2>
        {data.classes?.length ? <table className="ta-table"><thead><tr><th>班级</th><th>学生</th><th>有效估计</th><th>整体掌握</th><th>薄弱率</th><th>知识点</th><th>操作</th></tr></thead><tbody>
          {data.classes.map((item: Row) => <tr key={item.id}><td>{item.code}</td><td>{item.student_count}</td><td>{item.valid_student_count}</td><td>{pct(item.overall_mastery)}</td><td>{pct(item.overall_weak_rate)}</td><td>{item.knowledge_point_count}</td><td><button onClick={() => onOpenClass(item.id)}>进入班级</button></td></tr>)}
        </tbody></table> : <p className="ta-muted">暂无班级。</p>}
      </div>
      <div className="ta-card">
        <h2>数据质量</h2>
        <table className="ta-table"><tbody>
          <tr><td>原始作答</td><td>{quality.total ?? 0}</td></tr>
          <tr><td>有效</td><td>{quality.valid ?? 0}</td></tr>
          <tr><td>异常</td><td>{quality.abnormal ?? 0}</td></tr>
          <tr><td>待确认</td><td>{quality.pending_review ?? 0}</td></tr>
          <tr><td>已排除</td><td>{quality.excluded ?? 0}</td></tr>
          <tr><td>对账</td><td>{quality.reconciled ? '通过' : '不一致'}</td></tr>
        </tbody></table>
        <p className="ta-muted">主状态互斥：原始记录 = 有效 + 异常 + 待确认 + 已排除。</p>
      </div>
    </div>
    {performance.summary && <div className="ta-card">
      <h2>题目表现汇总</h2>
      <Stats items={[['应作答', summary.expected], ['实际作答', summary.answered], ['有效判题', summary.graded], ['待复核', summary.review], ['失败', summary.failed], ['未作答', summary.not_answered]]} />
      <p className="ta-muted">口径：{performance.basis?.note || '—'}；实际作答按学生去重。{(performance.warnings || []).length > 0 && ` ${performance.warnings[0]}`}</p>
    </div>}
    <div className="ta-card">
      <h2>知识点掌握分布</h2>
      {data.knowledge_points?.length ? <div className="ta-kp-list">{data.knowledge_points.map((item: Row) => <div className="ta-kp" key={item.knowledge_point_id}>
        <div className="ta-kp-head"><strong>{item.name}</strong><span>{pct(item.mean_mastery)}</span></div>
        <div className="ta-bar"><i style={{ width: `${Math.max(0, Math.min(100, (item.mean_mastery || 0) * 100))}%` }} /></div>
        <small>班级 {item.classes?.join('、')} · 有效学生 {item.valid_students}{item.valid_students < 3 ? ' · 样本不足，不下班级结论' : ''}</small>
      </div>)}</div> : <p className="ta-muted">没有达到证据门槛的知识点。</p>}
    </div>
    {trace && <div className="ta-card">
      <h2>证据链追溯</h2>
      <table className="ta-table"><thead><tr><th>阶段</th><th>保留</th><th>排除</th><th>说明</th></tr></thead><tbody>
        {(trace.stages || []).map((stage: Row) => <tr key={stage.stage}><td>{stage.label}</td><td>{stage.count}</td><td>{stage.excluded}</td><td>{stage.reason}</td></tr>)}
      </tbody></table>
      {(trace.samples || []).length > 0 && <details><summary>被排除样本（{trace.samples.length}）</summary><table className="ta-table"><thead><tr><th>学生</th><th>题目</th><th>阶段</th><th>原因</th></tr></thead><tbody>
        {trace.samples.slice(0, 40).map((item: Row, index: number) => <tr key={`${item.answer_id}-${index}`}><td>{item.student_code}</td><td>{item.question_id}</td><td>{item.stage}</td><td>{item.reason}</td></tr>)}
      </tbody></table></details>}
    </div>}
    <div className="ta-grid">
      <div className="ta-card"><h2>高频错误</h2>{data.error_types?.length ? <table className="ta-table"><thead><tr><th>类型</th><th>加权次数</th><th>知识点</th></tr></thead><tbody>{data.error_types.slice(0, 8).map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.knowledge_points?.join('、') || '—'}</td></tr>)}</tbody></table> : <p className="ta-muted">暂无错误类型证据。</p>}</div>
      <div className="ta-card"><h2>教学建议</h2><ul className="ta-list">{(data.suggestions || []).map((text: string, index: number) => <li key={index}>{text}</li>)}</ul></div>
    </div>
  </>;
}

function StackedBar({ kp, onStatus }: { kp: Row; onStatus: (status: string) => void }) {
  const total = kp.status_breakdown?.reduce((sum: number, item: Row) => sum + item.count, 0) || kp.total_students || 0;
  return <div className="ta-stack" role="img" aria-label={`${kp.name} 掌握状态分布`}>
    {(kp.status_breakdown || []).filter((item: Row) => item.count > 0).map((item: Row) => <button
      key={item.status}
      className={`ta-stack-seg ${STATUS_CLASS[item.status] || 'st-1'}`}
      style={{ width: `${total ? (item.count / total) * 100 : 0}%`, background: STATUS_COLOR[item.status] || '#cbd5e1' }}
      title={`${item.status} ${item.count} 人（${Math.round(item.share * 100)}%）`}
      onClick={() => onStatus(item.status)}
    ><span>{item.count}</span></button>)}
  </div>;
}

function ClassPanel({ data, classes, compare, onOpenStudent, onOpenQuestion, onOpenStatus, onOpenKp }: { data: Row; classes: Row[]; compare: Row | null; onOpenStudent: (id: string) => void; onOpenQuestion: (id: string) => void; onOpenStatus: (kp: Row, status: string) => void; onOpenKp: (kp: Row) => void }) {
  const summary = data.questions?.length ? {
    expected: data.questions.reduce((sum: number, item: Row) => sum + (item.expected || 0), 0),
    answered: data.questions.reduce((sum: number, item: Row) => sum + (item.answered || 0), 0),
    not_answered: data.questions.reduce((sum: number, item: Row) => sum + (item.not_answered || 0), 0),
  } : null;
  return <>
    <Stats items={[['班级', data.class?.code], ['学生', data.student_count], ['有效估计', data.valid_student_count], ['整体掌握', pct(data.overall_mastery)], ['薄弱率', pct(data.overall_weak_rate)], ['应/实际/未答', summary ? `${summary.expected}/${summary.answered}/${summary.not_answered}` : '—']]} />
    <p className="ta-note">{data.overall_mastery_note || '班级掌握以学生为基本单位等权聚合。'}</p>

    <div className="ta-card">
      <h2>知识点掌握分布</h2>
      <p className="ta-muted">每段代表一个掌握状态的人数，点击区段查看学生名单；点击知识点进入详情。证据不足单独一段，不按 0 分。</p>
      <div className="ta-dist">{data.knowledge_points?.map((kp: Row) => <div className="ta-dist-row" key={kp.knowledge_point_id}>
        <button className="ta-dist-name" onClick={() => onOpenKp(kp)} title={kp.name}>{kp.name}</button>
        <div className="ta-dist-bar"><StackedBar kp={kp} onStatus={(status) => onOpenStatus(kp, status)} /></div>
        <div className="ta-dist-meta"><b>{pct(kp.mean_mastery)}</b><span>σ {kp.std_dev ?? '—'}</span><span>{kp.valid_students}/{kp.total_students} 人</span><span>覆盖 {pct(kp.coverage)}</span>{!kp.status_total_check && <em>人数对账不一致</em>}</div>
      </div>)}
        {!data.knowledge_points?.length && <p className="ta-muted">该班级暂无知识点数据。</p>}
      </div>
      <div className="ta-legend">{STATUS_ORDER.map(status => <span key={status}><i style={{ background: STATUS_COLOR[status] }} />{status}</span>)}</div>
    </div>

    <div className="ta-card">
      <h2>班级比较</h2>
      {compare ? <CompareBars data={compare} /> : <p className="ta-muted">至少需要两个班级才能对比。</p>}
    </div>

    <div className="ta-card">
      <h2>题目表现</h2>
      <p className="ta-muted">应作答 = 参与该题所属测验且题目在范围内的人数；实际作答按学生去重；有效/待复核/失败互斥。点击行查看单题详情。</p>
      {data.questions?.length ? <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>题目</th><th>应作答</th><th>实际作答</th><th>有效判题</th><th>待复核</th><th>失败</th><th>未判题</th><th>未作答</th><th>平均分</th><th>得分率</th></tr></thead><tbody>
        {data.questions.map((item: Row) => <tr key={item.question_id} className="ta-clickable" onClick={() => onOpenQuestion(item.question_id)}>
          <td>{item.question_id}</td><td>{item.expected ?? '不可计算'}</td>
          <td>{item.answered}{item.expected != null ? `/${item.expected}` : ''}</td>
          <td>{item.graded}</td><td>{item.review}</td><td>{item.failed}</td><td>{item.pending}</td>
          <td>{item.not_answered ?? '—'}</td><td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td>
        </tr>)}
      </tbody></table></div> : <p className="ta-muted">{data.question_scope_note || '暂无题目数据。'}</p>}
      {data.questions?.[0] && <p className="ta-muted">口径：{data.questions[0].expected_note}；超出范围的作答不计入应作答。</p>}
    </div>

    <div className="ta-card">
      <h2>学生</h2>
      <div className="ta-students">{(data.students || []).map((profile: Row) => {
        const points = (profile.mastery || []).filter((item: Row) => item.status !== '证据不足');
        const average = points.length ? points.reduce((sum: number, item: Row) => sum + item.mastery_estimate, 0) / points.length : null;
        return <button key={profile.student.id} className="ta-student" onClick={() => onOpenStudent(profile.student.id)}>
          <strong>{profile.student.code}</strong>
          <span>{pct(average)} · 证据 {profile.data_quality?.evidence_count ?? 0} 条</span>
          <em>{points.length ? `可分析知识点 ${points.length}` : '证据不足'}</em>
        </button>;
      })}</div>
    </div>
  </>;
}

function CompareBars({ data }: { data: Row }) {
  const classes = data.classes || [];
  const rows = (data.knowledge_points || []).filter((row: Row) => row.pp_difference != null).slice(0, 12);
  return <>
    <div className="ta-compare-legend">{classes.map((cls: Row) => <span key={cls.class_id}><i />{cls.class_code}（有效 {cls.valid_student_count}）</span>)}<em>按差异绝对值排序，差值单位 pp</em></div>
    <div className="ta-compare-bars">{rows.map((row: Row) => <div className={`ta-compare-row ${row.comparable ? '' : 'insufficient'}`} key={row.knowledge_point_id}>
      <div className="ta-compare-name">{row.name}</div>
      <div className="ta-compare-pair">{row.values.map((value: Row, index: number) => <div className="ta-compare-line" key={value.class_id}>
        <span>{value.class_code}</span>
        <div className="ta-compare-track"><i style={{ width: `${Math.max(0, Math.min(100, (value.mean_mastery || 0) * 100))}%`, background: index === 0 ? '#2563eb' : '#f97316' }} /></div>
        <b>{pct(value.mean_mastery)}</b>
        <small>{value.valid_students}/{value.total_students}</small>
      </div>)}</div>
      <div className="ta-compare-diff">{pp(row.pp_difference)}{row.sample_warning && <em>{row.sample_warning}</em>}{row.coverage_warning && <em>{row.coverage_warning}</em>}</div>
    </div>)}
      {!rows.length && <p className="ta-muted">没有可比较的知识点。</p>}
    </div>
    <div className="ta-badges">
      <div><h3>班级优势知识点</h3>{(data.advantages || []).length ? <ul>{data.advantages.slice(0, 5).map((item: Row) => <li key={item.knowledge_point_id}>{item.class} · {item.name} {pp(item.pp_difference)}</li>)}</ul> : <p className="ta-muted">无明显优势（差值 &lt; 10pp）。</p>}</div>
      <div><h3>共同薄弱知识点</h3>{(data.common_weak || []).length ? <ul>{data.common_weak.slice(0, 5).map((item: Row) => <li key={item.knowledge_point_id}>{item.name}（{Object.entries(item.means).map(([code, mean]) => `${code} ${pct(mean as number)}`).join(' / ')}）</li>)}</ul> : <p className="ta-muted">暂无共同薄弱项。</p>}</div>
      <div><h3>样本不足</h3>{(data.insufficient || []).length ? <ul>{data.insufficient.slice(0, 5).map((item: Row) => <li key={item.knowledge_point_id}>{item.name}：{item.reason}</li>)}</ul> : <p className="ta-muted">所有知识点样本量满足比较要求。</p>}</div>
    </div>
    <p className="ta-muted">{data.note}</p>
  </>;
}

function StudentPanel({ data, studentId, scope }: { data: Row | null; studentId: string; scope: string }) {
  if (!studentId) return <div className="ta-empty">请选择学生。</div>;
  if (!data) return <div className="ta-empty">正在计算学生画像…</div>;
  const coverage = data.coverage || {};
  return <>
    <p className="ta-note">{scope === 'single' ? '当前显示：本次任务（跟随顶部数据范围）。' : '当前显示：跨任务累计；只合并通过可比性校验的任务。'}</p>
    {(data.excluded_batches || []).length > 0 && <div className="ta-warning-box">
      <strong>以下任务未参与合并（不可比）</strong>
      <ul>{data.excluded_batches.map((item: Row) => <li key={item.batch_id}>{item.batch_id}：{item.reasons.join('；')}</li>)}</ul>
    </div>}
    <Stats items={[['学生', data.student?.code], ['已作答', `${coverage.answered_question_count ?? '—'}/${coverage.expected_question_count ?? '未知'}`], ['有效判题', coverage.graded_question_count ?? 0], ['证据覆盖', pct(coverage.evidence_coverage)], ['证据条数', data.data_quality?.evidence_count ?? 0], ['可分析知识点', data.mastery?.length ?? 0]]} />
    {scope !== 'single' && data.per_batch?.length > 0 && <div className="ta-card"><h2>任务贡献</h2><table className="ta-table"><thead><tr><th>任务</th><th>作答题目</th><th>证据</th><th>证据覆盖</th></tr></thead><tbody>
      {data.per_batch.map((item: Row) => <tr key={item.batch_id}><td>{item.filename || item.batch_id}</td><td>{item.answered_questions}</td><td>{item.evidence_count}</td><td>{pct(item.evidence_coverage)}</td></tr>)}
    </tbody></table></div>}
    <div className="ta-card">
      <h2>知识点掌握</h2>
      {data.mastery?.length ? <div className="ta-kp-list">{data.mastery.map((item: Row) => <div className={`ta-kp ${STATUS_CLASS[item.status] || ''}`} key={item.knowledge_point_id}>
        <div className="ta-kp-head"><strong>{item.knowledge_point_name}</strong><span>{pct(item.mastery_estimate)}</span></div>
        <div className="ta-bar"><i style={{ width: `${Math.max(0, Math.min(100, (item.mastery_estimate || 0) * 100))}%` }} /></div>
        <small>{item.status} · 区间 {pct(item.credible_interval_lower)}–{pct(item.credible_interval_upper)} · 证据 {item.evidence_count} 条 · 覆盖 {item.covered_question_count}/{item.opportunity_question_count}</small>
      </div>)}</div> : <p className="ta-muted">没有评分点级证据，暂不能估计掌握程度。</p>}
    </div>
    <div className="ta-grid">
      <div className="ta-card"><h2>薄弱知识点</h2>{data.weak_points?.length ? <ul className="ta-list">{data.weak_points.map((item: Row) => <li key={item.knowledge_point_id}>{item.knowledge_point_name} · {pct(item.mastery_estimate)}</li>)}</ul> : <p className="ta-muted">暂无。</p>}</div>
      <div className="ta-card"><h2>学习建议</h2><ul className="ta-list">{(data.suggestions || []).map((text: string, index: number) => <li key={index}>{text}</li>)}</ul></div>
    </div>
    {data.evidence?.length > 0 && <div className="ta-card"><h2>评分点证据（前 50 条）</h2><table className="ta-table"><thead><tr><th>题目</th><th>评分点</th><th>得分</th><th>证据</th><th>错误</th></tr></thead><tbody>
      {data.evidence.slice(0, 50).map((item: Row, index: number) => <tr key={`${item.answer_id}-${item.scoring_point_id}-${index}`}><td>{item.question_id}</td><td>{item.scoring_point_id}</td><td>{item.earned_score}/{item.full_score}</td><td className="ta-evidence">{item.evidence_text}</td><td>{item.error_type}</td></tr>)}
    </tbody></table></div>}
  </>;
}

function KnowledgePanel({ data }: { data: Row }) {
  const students = data.students || [];
  const points = data.knowledge_points || [];
  const cells = data.cells || [];
  const lookup = useMemo(() => { const map = new Map<string, Row>(); cells.forEach((cell: Row) => map.set(`${cell.student_id}|${cell.knowledge_point_id}`, cell)); return map; }, [cells]);
  if (!students.length || !points.length) return <div className="ta-empty">当前没有可展示的知识点矩阵（需要评分点级证据）。</div>;
  return <>
    <p className="ta-note">矩阵单元格显示掌握估计；「证据不足」不等同于 0 分，也不做补值。</p>
    <div className="ta-matrix-wrap"><table className="ta-table ta-matrix"><thead><tr><th className="ta-sticky">学生</th>{points.map((point: Row) => <th key={point.knowledge_point_id} title={point.name}>{point.name?.slice(0, 10)}</th>)}</tr></thead><tbody>
      {students.map((student: Row) => <tr key={student.id}><td className="ta-sticky">{student.code}</td>{points.map((point: Row) => {
        const cell = lookup.get(`${student.id}|${point.knowledge_point_id}`);
        return <td key={point.knowledge_point_id} className={cell ? STATUS_CLASS[cell.status] : 'ta-muted'} title={cell ? `${cell.status} · 证据 ${cell.evidence_count}` : '证据不足'}>{cell && cell.status !== '证据不足' ? pct(cell.mastery_estimate) : '—'}</td>;
      })}</tr>)}
    </tbody></table></div>
    <div className="ta-legend">{(data.status_legend || []).map((item: string) => <span key={item}><i style={{ background: STATUS_COLOR[item] }} />{item}</span>)}</div>
  </>;
}

function ErrorsPanel({ data }: { data: Row }) {
  return <>
    <Stats items={[['错误类型', data.types?.length ?? 0], ['覆盖题目', data.by_question?.length ?? 0], ['规则标签', data.label_source?.rule ?? 0], ['人工真值', data.gold_labels ?? 0]]} />
    <div className="ta-card"><h2>错误类型分布</h2>{data.types?.length ? <table className="ta-table"><thead><tr><th>类型</th><th>加权次数</th><th>涉及知识点</th><th>班级</th></tr></thead><tbody>
      {data.types.map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.knowledge_points?.join('、') || '—'}</td><td>{item.classes?.join('、')}</td></tr>)}
    </tbody></table> : <p className="ta-muted">暂无错误类型证据。</p>}</div>
    <div className="ta-card"><h2>题目错误与判题状态</h2>{data.by_question?.length ? <table className="ta-table"><thead><tr><th>题目</th><th>作答</th><th>有效判题</th><th>待复核</th><th>失败</th><th>平均分</th><th>得分率</th></tr></thead><tbody>
      {data.by_question.map((item: Row) => <tr key={item.question_id}><td>{item.question_id}</td><td>{item.answered}</td><td>{item.valid}</td><td>{item.review}</td><td>{item.failed}</td><td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td></tr>)}
    </tbody></table> : <p className="ta-muted">暂无题目数据。</p>}</div>
    <p className="ta-note">错误类型只是分类标签，不代表掌握程度；没有人工 gold 标签时不报告 precision/recall。</p>
  </>;
}

function TrendPanel({ data }: { data: Row | null }) {
  if (!data) return <div className="ta-empty">正在读取趋势…</div>;
  const timeline = data.timeline || [];
  const series = data.knowledge_points || [];
  return <>
    {(data.excluded_batches || []).length > 0 && <div className="ta-warning-box"><strong>不可比任务已排除</strong><ul>{data.excluded_batches.map((item: Row) => <li key={item.batch_id}>{item.batch_id}：{item.reasons.join('；')}</li>)}</ul></div>}
    <p className="ta-note">EWMA 只用于观察趋势，原始值保留；不可比的时间点不参与计算，也不补 0。</p>
    {timeline.length < 2 || !series.length ? <div className="ta-empty">暂无可比较数据：需要至少两个可比任务且证据达到门槛。</div> : <div className="ta-card"><h2>知识点趋势</h2><div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th className="ta-sticky">知识点</th>{timeline.map((point: Row) => <th key={point.batch_id}>{point.label || point.batch_id}</th>)}</tr></thead><tbody>
      {series.map((item: Row) => <tr key={item.knowledge_point_id}><td className="ta-sticky">{item.name}</td>{(item.points || []).map((value: Row) => <td key={value.batch_id} className={value.status === '证据不足' ? 'ta-muted' : ''}>{value.status === '证据不足' ? '证据不足' : pct(value.ewma_mastery ?? value.mastery_estimate)}</td>)}</tr>)}
    </tbody></table></div></div>}
  </>;
}

function SuggestPanel({ data, tasks, llm }: { data: Row | null; tasks: Row | null; llm: Row | null }) {
  const task = tasks?.latest?.llm;
  const content = llm?.llm || null;
  return <>
    <p className="ta-note">统计数字来自确定性层（不调用模型）；语义解释由 LLM 生成并引用证据，不会重算数字。</p>
    {task?.status === 'partial' && <div className="ta-warning-box"><strong>语义分析未完成</strong><p>{task.error || '模型未返回结果'}</p></div>}
    {content && <div className="ta-card">
      <h2>语义分析（引用证据）</h2>
      {content.class_observations?.length > 0 && <div className="ta-suggest-list">{content.class_observations.map((item: Row, index: number) => <div className="ta-suggest" key={`obs-${index}`}>
        <div className="ta-suggest-head"><strong>{item.class_code} · 班级观察</strong></div>
        <p>{item.summary}</p>
        <div className="ta-chips">{(item.evidence_refs || []).map((ref: string) => <span key={ref}>{ref}</span>)}</div>
      </div>)}</div>}
      {content.error_explanations?.length > 0 && <><h3>错误类型解释</h3><div className="ta-suggest-list">{content.error_explanations.map((item: Row, index: number) => <div className="ta-suggest" key={`err-${index}`}>
        <div className="ta-suggest-head"><strong>{item.error_type}</strong></div>
        <p>{item.explanation}</p>
        <div className="ta-chips">{(item.evidence_refs || []).map((ref: string) => <span key={ref}>{ref}</span>)}</div>
      </div>)}</div></>}
      {content.teaching_suggestions?.length > 0 && <><h3>教学建议</h3><div className="ta-suggest-list">{content.teaching_suggestions.map((item: Row, index: number) => <div className="ta-suggest" key={`sug-${index}`}>
        <div className="ta-suggest-head"><strong>{item.class_code}</strong><span>{item.confidence != null ? `置信度 ${Math.round(item.confidence * 100)}%` : ''}</span></div>
        <p>{item.suggestion}</p>
        <div className="ta-chips">{(item.evidence_refs || []).map((ref: string) => <span key={ref}>{ref}</span>)}</div>
      </div>)}</div></>}
      <p className="ta-muted">模型 {llm?.model || '—'} · 提示词版本 {llm?.prompt_version || '—'} · 生成时间 {llm?.generated_at || '—'}</p>
    </div>}
    {data?.suggestions?.length ? <div className="ta-card"><h2>规则推导的薄弱点建议</h2><div className="ta-suggest-list">{data.suggestions.map((item: Row, index: number) => <div className="ta-suggest" key={`${item.class_code}-${item.knowledge_point_id}-${index}`}>
      <div className="ta-suggest-head"><strong>{item.class_code} · {item.knowledge_point_name}</strong><span>{pct(item.mastery)}</span></div>
      <p>{item.suggestion}</p>
      <small>有效学生 {item.valid_students} · 低于阈值 {item.weak_students} · 证据不足 {item.insufficient_students}</small>
    </div>)}</div></div> : <div className="ta-empty">当前没有达到证据门槛的薄弱知识点。</div>}
    <p className="ta-muted">依据：{data?.evidence_basis?.rule} · 模型调用 {data?.evidence_basis?.model_calls ?? 0} 次</p>
  </>;
}

function StudentLadder({ kp, status, onClose, onOpenStudent }: { kp: Row; status: string; onClose: () => void; onOpenStudent: (id: string) => void }) {
  const bucket = (kp.status_breakdown || []).find((item: Row) => item.status === status);
  const students = bucket?.students || [];
  return <div className="ta-drawer">
    <div className="ta-drawer-head"><h2>{kp.name} · {status}（{students.length} 人）</h2><button onClick={onClose}>关闭</button></div>
    {students.length ? <table className="ta-table"><thead><tr><th>学生</th><th>掌握估计</th><th>证据</th><th>操作</th></tr></thead><tbody>
      {students.map((item: Row) => <tr key={item.student_id}><td>{item.student_code}</td><td>{pct(item.mastery_estimate)}</td><td>{item.evidence_count}</td><td><button onClick={() => onOpenStudent(item.student_id)}>查看画像</button></td></tr>)}
    </tbody></table> : <p className="ta-muted">该状态下没有学生。</p>}
  </div>;
}

function KpDrawer({ data, name, onClose, onOpenQuestion }: { data: Row | null; name: string; onClose: () => void; onOpenQuestion: (id: string) => void }) {
  return <div className="ta-drawer">
    <div className="ta-drawer-head"><h2>{name} · 知识点详情</h2><button onClick={onClose}>关闭</button></div>
    {!data ? <p className="ta-muted">正在加载知识点详情…</p> : <>
      <Stats items={[['均值', pct(data.knowledge_point?.mean_mastery)], ['标准差', data.knowledge_point?.std_dev ?? '—'], ['有效/全班', `${data.knowledge_point?.valid_students}/${data.knowledge_point?.total_students}`], ['覆盖率', pct(data.knowledge_point?.coverage)], ['证据条数', data.evidence_count], ['相关题目', data.questions?.length ?? 0]]} />
      <h3>相关题目</h3>
      {data.questions?.length ? <table className="ta-table"><thead><tr><th>题目</th><th>应作答</th><th>实际</th><th>有效</th><th>待复核</th><th>失败</th><th>得分率</th></tr></thead><tbody>
        {data.questions.map((item: Row) => <tr key={item.question_id} className="ta-clickable" onClick={() => onOpenQuestion(item.question_id)}><td>{item.question_id}</td><td>{item.expected ?? '—'}</td><td>{item.answered}</td><td>{item.graded}</td><td>{item.review}</td><td>{item.failed}</td><td>{item.score_rate ?? '—'}{item.score_rate != null ? '%' : ''}</td></tr>)}
      </tbody></table> : <p className="ta-muted">暂无题目。</p>}
      <h3>评分点</h3>
      <div className="ta-chips">{data.scoring_points?.map((item: Row) => <span key={item.scoring_point_id}>{item.scoring_point_id}<small>证据 {item.evidence_count}</small></span>)}</div>
      <h3>错误类型</h3>
      {data.error_types?.length ? <table className="ta-table"><thead><tr><th>类型</th><th>加权次数</th><th>学生数</th><th>题目</th></tr></thead><tbody>
        {data.error_types.map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.student_count}</td><td>{item.questions?.join('、')}</td></tr>)}
      </tbody></table> : <p className="ta-muted">暂无错误证据。</p>}
      <h3>证据样本</h3>
      <table className="ta-table"><thead><tr><th>学生</th><th>题目</th><th>评分点</th><th>得分</th><th>错误</th><th>证据</th></tr></thead><tbody>
        {(data.evidence_sample || []).map((item: Row, index: number) => <tr key={index}><td>{item.student_code}</td><td>{item.question_id}</td><td>{item.scoring_point_id}</td><td>{item.earned_score}/{item.full_score}</td><td>{item.error_type}</td><td className="ta-evidence">{item.evidence_text}</td></tr>)}
      </tbody></table>
      <p className="ta-muted">{data.note}</p>
    </>}
  </div>;
}

function QuestionDrawer({ data, questionId, onClose }: { data: Row | null; questionId: string; onClose: () => void }) {
  const overall = data?.overall || {};
  return <div className="ta-drawer">
    <div className="ta-drawer-head"><h2>单题详情 · {questionId}</h2><button onClick={onClose}>关闭</button></div>
    {!data ? <p className="ta-muted">正在加载题目数据…</p> : <>
      <Stats items={[['应作答', overall.expected ?? '不可计算'], ['实际作答', overall.answered], ['有效判题', overall.graded], ['待复核', overall.review], ['失败', overall.failed], ['未作答', overall.not_answered ?? '—'], ['平均分', overall.student_average_score ?? '—'], ['得分率', overall.score_rate == null ? '—' : `${overall.score_rate}%`]]} />
      <p className="ta-note">{overall.expected_note}；未判题与未作答分开统计，不按 0 分。</p>
      <h3>评分点</h3>
      <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>评分点</th><th>满分</th><th>累计得分</th><th>学生等权</th><th>有效学生</th><th>未映射</th><th>异常分</th><th>缺结果</th></tr></thead><tbody>
        {(data.scoring_points || []).map((item: Row) => <tr key={item.scoring_point_id}><td>{item.scoring_point_id}</td><td>{item.full_score_consistent ? item.full_score : '满分不一致'}</td><td>{item.total_earned_score}</td><td>{item.student_average_score ?? '—'}</td><td>{item.valid_students}</td><td>{item.unmapped_knowledge_count}</td><td>{item.invalid_score_count}</td><td>{item.missing_scoring_point_result_count}</td></tr>)}
      </tbody></table></div>
      <h3>学生表现</h3>
      <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>学生</th><th>状态</th><th>得分</th><th>错误</th><th>理由</th></tr></thead><tbody>
        {(data.students || []).map((item: Row) => <tr key={item.student_id}><td>{item.student_code}</td><td>{ANSWER_STATUS_LABEL[item.status] || item.status}</td><td>{item.score ?? '—'}/{item.max_score}</td><td>{item.error_type}</td><td className="ta-evidence">{item.judgment_reason || '—'}</td></tr>)}
      </tbody></table></div>
    </>}
  </div>;
}
