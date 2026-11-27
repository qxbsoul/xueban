import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../api';
import './teacher-analytics.css';

type Row = Record<string, any>;

const SECTIONS: Array<{ id: string; label: string; hint: string }> = [
  { id: 'overview', label: '总体概览', hint: '任务、班级、知识点与证据质量的汇总' },
  { id: 'class', label: '班级学情', hint: '班级掌握分布、题目表现与班级比较' },
  { id: 'student', label: '学生画像', hint: '单个学生的证据、掌握与建议' },
  { id: 'knowledge', label: '知识点分析', hint: '知识点 × 学生掌握矩阵' },
  { id: 'errors', label: '错误分析', hint: '错误类型、题目与标签覆盖' },
  { id: 'trend', label: '学习趋势', hint: '跨任务可比性与 EWMA' },
  { id: 'suggest', label: '教学建议', hint: '由证据推导的教学行动' },
];

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

/** 每个数据源独立控制器：切页/切范围时取消旧请求，AbortError 不计为业务失败。 */
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
const STATUS_CLASS: Record<string, string> = { 熟练掌握: 'ta-good', 基本掌握: 'ta-good', 初步掌握: 'ta-mid', 掌握不足: 'ta-bad', 证据不足: 'ta-muted' };

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
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [studentScope, setStudentScope] = useState<'single' | 'all'>('single');
  const [questionId, setQuestionId] = useState('');
  const request = useAbortableRequest();

  const effectiveBatch = batchId || batches[0]?.id || '';
  const effectiveBatches = scope === 'single' ? [effectiveBatch].filter(Boolean) : (selectedBatches.length ? selectedBatches : batches.map(item => item.id));
  const scopeLabel = scope === 'single' ? `单次任务 · ${batches.find(item => item.id === effectiveBatch)?.filename || '—'}` : scope === 'all' ? `全部任务 · ${effectiveBatches.length} 个` : `多次任务 · ${effectiveBatches.length} 个`;

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
      setClassId(current => (data.classes || []).some(item => item.id === current) ? current : (data.classes?.[0]?.id || ''));
    });
  }, [effectiveBatch, request, run]);

  // 每一次范围/页面变化都重新取数；请求被取消时直接忽略返回值。
  // 每个数据源独立取数：只有本页依赖的范围变化才重新请求。
  // 之前把 overview 和 trace 放在同一个 Promise.all 里，班级列表更新导致 classId 变化时
  // 会把正在进行的请求一起 abort，页面就停在「空白但无报错」的状态。
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
    if (!effectiveBatch || section !== 'class' || !classId) return;
    void run(async () => {
      const data = await request<Row>('classProfile', `/api/teacher/analysis/classes/${encodeURIComponent(classId)}?batch_id=${encodeURIComponent(effectiveBatch)}${scope !== 'single' ? `&scope=multi&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}` : ''}`);
      if (data) setClassProfile(data);
    });
  }, [section, effectiveBatch, classId, scope, effectiveBatches.join(','), request, run]);

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

  useEffect(() => {
    if (section !== 'student' || !studentId || !effectiveBatch) return;
    void run(async () => {
      const data = await request<Row>('student', `/api/teacher/analysis/students/${encodeURIComponent(studentId)}?batch_id=${encodeURIComponent(effectiveBatch)}&scope=${studentScope}${studentScope === 'all' ? `&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}` : ''}`);
      if (data) setStudentProfile(data);
    });
  }, [section, studentId, studentScope, effectiveBatch, effectiveBatches.join(','), request, run]);

  const compareClasses = async () => {
    if (compareIds.length < 2) { setMessage('至少选择两个班级再对比。'); return; }
    await run(async () => {
      const data = await request<Row>('compare', `/api/teacher/analysis/compare?batch_id=${encodeURIComponent(effectiveBatch)}&class_ids=${encodeURIComponent(compareIds.join(','))}`);
      if (data) setCompare(data);
    });
  };

  const students = useMemo(() => (classProfile?.students || []).map((item: Row) => item.student), [classProfile]);
  const selectSection = (id: string) => { setSection(id); setError(''); setStudentProfile(null); setCompare(null); };

  return <div className="ta-shell">
    <aside className="ta-nav" aria-label="学情分析二级导航">
      <div className="ta-nav-title">学情分析</div>
      {SECTIONS.map(item => <button key={item.id} className={`ta-nav-item ${section === item.id ? 'on' : ''}`} onClick={() => selectSection(item.id)} title={item.hint}>
        <span>{item.label}</span>
      </button>)}
      <div className="ta-nav-foot">
        <span className={`ta-dot ${loading ? 'busy' : ''}`} />{loading ? '加载中' : '已就绪'}
      </div>
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
            <select value={scope} onChange={event => { const value = event.target.value as 'single' | 'all' | 'multi'; setScope(value); setStudentProfile(null); setCompare(null); }}>
              <option value="single">单次任务</option>
              <option value="all">全部任务</option>
              <option value="multi">多次任务</option>
            </select>
            {scope === 'single' && <select value={effectiveBatch} onChange={event => { setBatchId(event.target.value); setStudentProfile(null); setClassProfile(null); setCompare(null); }}>
              {batches.map(item => <option key={item.id} value={item.id}>{item.filename || item.id}</option>)}
            </select>}
          </div>
          {scope !== 'single' && <div className="ta-batch-picker">
            {batches.map(item => <label key={item.id}><input type="checkbox" checked={selectedBatches.includes(item.id)} onChange={() => setSelectedBatches(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} /><span>{item.filename || item.id}</span></label>)}
            <small>{scopeLabel}</small>
          </div>}
          <div className="ta-scope-row">
            <label>班级</label>
            <select value={classId} onChange={event => { setClassId(event.target.value); setStudentId(''); setStudentProfile(null); setCompare(null); }}>
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

      {error && <div className="ta-error">读取失败：{error}<button onClick={() => selectSection(section)}>重试</button></div>}
      {!effectiveBatch && <div className="ta-empty">还没有可分析的任务，请先在「数据管理」导入 CSV。</div>}

      {section === 'overview' && overview && <Overview data={overview} trace={trace} onOpenClass={(id) => { setClassId(id); selectSection('class'); }} />}
      {section === 'class' && classProfile && <>
        <ClassPanel data={classProfile} classes={classes} compareIds={compareIds} onToggleCompare={(id) => setCompareIds(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])} onCompare={compareClasses} compare={compare} onOpenStudent={(id) => { setStudentId(id); selectSection('student'); }} onOpenQuestion={setQuestionId} />
        {questionId && <QuestionDetailPanel batchId={effectiveBatch} classId={classId} questionId={questionId} onClose={() => setQuestionId('')} />}
      </>}
      {section === 'student' && <StudentPanel data={studentProfile} studentId={studentId} scope={studentScope} onScope={setStudentScope} />}
      {section === 'knowledge' && matrix && <KnowledgePanel data={matrix} />}
      {section === 'errors' && errors && <ErrorsPanel data={errors} />}
      {section === 'trend' && <TrendPanel data={trend} />}
      {section === 'suggest' && suggestions && <SuggestPanel data={suggestions} />}
      {section === 'class' && !classProfile && !loading && <div className="ta-empty">请选择班级。</div>}
      {section === 'student' && !studentId && <div className="ta-empty">请选择学生。</div>}
    </section>
  </div>;
}

function Stats({ items }: { items: Array<[string, unknown]> }) {
  return <div className="ta-stats">{items.map(([label, value]) => <div key={label}><b>{value == null || value === '' ? '—' : String(value)}</b><span>{label}</span></div>)}</div>;
}

function Overview({ data, trace, onOpenClass }: { data: Row; trace: Row | null; onOpenClass: (id: string) => void }) {
  const quality = data.data_quality || {};
  return <>
    <Stats items={[['班级', data.classes?.length ?? 0], ['有效学生', data.classes?.reduce((sum: number, item: Row) => sum + (item.valid_student_count || 0), 0)], ['可分析知识点', data.knowledge_points?.length ?? 0], ['有效作答', quality.valid ?? 0], ['待确认', quality.pending_review ?? 0], ['异常', quality.abnormal ?? 0]]} />
    <div className="ta-grid">
      <div className="ta-card">
        <h2>班级概览</h2>
        {data.classes?.length ? <table className="ta-table"><thead><tr><th>班级</th><th>学生</th><th>有效估计</th><th>整体掌握</th><th>薄弱率</th><th>知识点</th><th>操作</th></tr></thead><tbody>
          {data.classes.map((item: Row) => <tr key={item.id}><td>{item.code}</td><td>{item.student_count}</td><td>{item.valid_student_count}</td><td>{pct(item.overall_mastery)}</td><td>{pct(item.overall_weak_rate)}</td><td>{item.knowledge_point_count}</td><td><button onClick={() => onOpenClass(item.id)}>进入班级</button></td></tr>)}
        </tbody></table> : <p className="ta-muted">暂无班级。</p>}
      </div>
      <div className="ta-card">
        <h2>证据质量</h2>
        <table className="ta-table"><tbody>
          <tr><td>原始作答</td><td>{quality.total ?? 0}</td></tr>
          <tr><td>有效</td><td>{quality.valid ?? 0}</td></tr>
          <tr><td>异常</td><td>{quality.abnormal ?? 0}</td></tr>
          <tr><td>待确认</td><td>{quality.pending_review ?? 0}</td></tr>
          <tr><td>已排除</td><td>{quality.excluded ?? 0}</td></tr>
          <tr><td>对账</td><td>{quality.reconciled ? '通过（总数一致）' : '不一致'}</td></tr>
        </tbody></table>
        <p className="ta-muted">主状态互斥：原始记录 = 有效 + 异常 + 待确认 + 已排除。</p>
      </div>
    </div>
    <div className="ta-card">
      <h2>知识点掌握分布</h2>
      {data.knowledge_points?.length ? <div className="ta-kp-list">{data.knowledge_points.map((item: Row) => <div className="ta-kp" key={item.knowledge_point_id}>
        <div className="ta-kp-head"><strong>{item.name}</strong><span>{pct(item.mean_mastery)}</span></div>
        <div className="ta-bar"><i style={{ width: `${Math.max(0, Math.min(100, (item.mean_mastery || 0) * 100))}%` }} /></div>
        <small>班级 {item.classes?.join('、')} · 有效学生 {item.valid_students}</small>
      </div>)}</div> : <p className="ta-muted">没有达到证据门槛的知识点。</p>}
    </div>
    {trace && <div className="ta-card">
      <h2>证据链追溯</h2>
      <p className="ta-muted">从原始作答到证据行，每一级的数量与排除原因；用于定位「为什么没有证据」。</p>
      <table className="ta-table"><thead><tr><th>阶段</th><th>保留</th><th>排除</th><th>说明</th></tr></thead><tbody>
        {(trace.stages || []).map((stage: Row) => <tr key={stage.stage}><td>{stage.label}</td><td>{stage.count}</td><td>{stage.excluded}</td><td>{stage.reason}</td></tr>)}
      </tbody></table>
      {(trace.samples || []).length > 0 && <details><summary>查看被排除的样本（{trace.samples.length}）</summary><table className="ta-table"><thead><tr><th>学生</th><th>题目</th><th>阶段</th><th>原因</th></tr></thead><tbody>
        {trace.samples.slice(0, 40).map((item: Row, index: number) => <tr key={`${item.answer_id}-${index}`}><td>{item.student_code}</td><td>{item.question_id}</td><td>{item.stage}</td><td>{item.reason}</td></tr>)}
      </tbody></table></details>}
    </div>}
    <div className="ta-grid">
      <div className="ta-card"><h2>高频错误</h2>{data.error_types?.length ? <table className="ta-table"><thead><tr><th>类型</th><th>加权次数</th><th>知识点</th></tr></thead><tbody>{data.error_types.slice(0, 8).map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.knowledge_points?.join('、') || '—'}</td></tr>)}</tbody></table> : <p className="ta-muted">暂无错误类型证据。</p>}</div>
      <div className="ta-card"><h2>教学建议</h2><ul className="ta-list">{(data.suggestions || []).map((text: string, index: number) => <li key={index}>{text}</li>)}</ul></div>
    </div>
  </>;
}

function ClassPanel({ data, classes, compareIds, onToggleCompare, onCompare, compare, onOpenStudent, onOpenQuestion }: { data: Row; classes: Row[]; compareIds: string[]; onToggleCompare: (id: string) => void; onCompare: () => void; compare: Row | null; onOpenStudent: (id: string) => void; onOpenQuestion: (id: string) => void }) {
  return <>
    <Stats items={[['班级', data.class?.code], ['学生', data.student_count], ['有效估计', data.valid_student_count], ['整体掌握', pct(data.overall_mastery)], ['薄弱率', pct(data.overall_weak_rate)], ['证据不足', data.insufficient_student_count]]} />
    <p className="ta-note">{data.overall_mastery_note || '班级掌握以学生为基本单位等权聚合。'}</p>
    <div className="ta-card">
      <h2>班级比较</h2>
      <div className="ta-picker">{classes.map(item => <label key={item.id}><input type="checkbox" checked={compareIds.includes(item.id)} onChange={() => onToggleCompare(item.id)} />{item.code}</label>)}<button onClick={onCompare}>对比所选班级</button></div>
      {compare?.rows?.length ? <table className="ta-table"><thead><tr><th>知识点</th>{(compare.classes || []).map((cls: Row) => <th key={cls.id}>{cls.code}</th>)}<th>差值</th></tr></thead><tbody>
        {compare.rows.map((row: Row) => <tr key={row.knowledge_point_id}><td>{row.name}</td>{(row.values || []).map((value: Row) => <td key={value.class_id}>{value.mean == null ? '证据不足' : pct(value.mean)} / {value.valid_students}</td>)}<td>{row.difference == null ? '—' : `${Math.round(row.difference * 100)}pp`}</td></tr>)}
      </tbody></table> : <p className="ta-muted">选择两个及以上班级后对比；样本不足时不做显著性解释。</p>}
    </div>
    <div className="ta-card">
      <h2>知识点掌握分布</h2>
      {data.knowledge_points?.length ? <table className="ta-table"><thead><tr><th>知识点</th><th>均值</th><th>标准差</th><th>有效/总人数</th><th>覆盖率</th><th>薄弱</th><th>状态分布</th></tr></thead><tbody>
        {data.knowledge_points.map((item: Row) => <tr key={item.knowledge_point_id}><td>{item.name}</td><td>{pct(item.mean_mastery)}</td><td>{item.std_dev}</td><td>{item.valid_students}/{item.total_students}</td><td>{pct(item.coverage)}</td><td>{item.weak_students}</td><td>{Object.entries(item.status_distribution || {}).map(([key, value]) => `${key}:${value}`).join(' · ')}</td></tr>)}
      </tbody></table> : <p className="ta-muted">该班级暂无达到门槛的知识点掌握估计。</p>}
    </div>
    <div className="ta-card">
      <h2>题目表现</h2>
      {data.questions?.length ? <table className="ta-table"><thead><tr><th>题目</th><th>题型</th><th>满分</th><th>作答</th><th>有效判题</th><th>待复核</th><th>失败</th><th>平均分</th><th>得分率</th></tr></thead><tbody>
        {data.questions.map((item: Row) => <tr key={item.question_id} className="ta-clickable" onClick={() => onOpenQuestion(item.question_id)}><td>{item.question_id}</td><td>{item.question_type}</td><td>{item.max_score}</td><td>{item.answered_students}</td><td>{item.valid_judgement_students}</td><td>{item.review_students}</td><td>{item.failed_students}</td><td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td></tr>)}
      </tbody></table> : <p className="ta-muted">{data.question_scope_note || '暂无题目数据。'}</p>}
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

function StudentPanel({ data, studentId, scope, onScope }: { data: Row | null; studentId: string; scope: 'single' | 'all'; onScope: (value: 'single' | 'all') => void }) {
  if (!studentId) return <div className="ta-empty">请选择学生。</div>;
  if (!data) return <div className="ta-empty">正在计算学生画像…</div>;
  const coverage = data.coverage || {};
  return <>
    <div className="ta-tabs"><button className={scope === 'all' ? 'on' : ''} onClick={() => onScope('all')}>全部任务</button><button className={scope === 'single' ? 'on' : ''} onClick={() => onScope('single')}>本次任务</button></div>
    <Stats items={[['学生', data.student?.code], ['已作答', `${coverage.answered_question_count ?? '—'}/${coverage.expected_question_count ?? '未知'}`], ['有效判题', coverage.graded_question_count ?? 0], ['证据覆盖', pct(coverage.evidence_coverage)], ['证据条数', data.data_quality?.evidence_count ?? 0], ['可分析知识点', data.mastery?.length ?? 0]]} />
    {scope === 'all' && data.per_batch?.length > 0 && <div className="ta-card"><h2>任务贡献</h2><table className="ta-table"><thead><tr><th>任务</th><th>作答题目</th><th>证据</th><th>证据覆盖</th></tr></thead><tbody>
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
  const lookup = useMemo(() => {
    const map = new Map<string, Row>();
    cells.forEach((cell: Row) => map.set(`${cell.student_id}|${cell.knowledge_point_id}`, cell));
    return map;
  }, [cells]);
  if (!students.length || !points.length) return <div className="ta-empty">当前没有可展示的知识点矩阵（需要评分点级证据）。</div>;
  return <>
    <p className="ta-note">矩阵单元格显示掌握估计；「证据不足」不等同于 0 分，也不做补值。</p>
    <div className="ta-matrix-wrap">
      <table className="ta-table ta-matrix"><thead><tr><th className="ta-sticky">学生</th>{points.map((point: Row) => <th key={point.knowledge_point_id} title={point.name}>{point.name?.slice(0, 10)}</th>)}</tr></thead><tbody>
        {students.map((student: Row) => <tr key={student.id}><td className="ta-sticky">{student.code}</td>{points.map((point: Row) => {
          const cell = lookup.get(`${student.id}|${point.knowledge_point_id}`);
          return <td key={point.knowledge_point_id} className={cell ? STATUS_CLASS[cell.status] : 'ta-muted'} title={cell ? `${cell.status} · 证据 ${cell.evidence_count} · 覆盖 ${cell.covered_question_count}/${cell.opportunity_question_count}` : '证据不足'}>{cell && cell.status !== '证据不足' ? pct(cell.mastery_estimate) : '—'}</td>;
        })}</tr>)}
      </tbody></table>
    </div>
    <div className="ta-legend">{(data.status_legend || []).map((item: string) => <span key={item} className={STATUS_CLASS[item] || 'ta-muted'}>{item}</span>)}</div>
  </>;
}

function ErrorsPanel({ data }: { data: Row }) {
  return <>
    <Stats items={[['错误类型', data.types?.length ?? 0], ['覆盖题目', data.by_question?.length ?? 0], ['规则标签', data.label_source?.rule ?? 0], ['人工真值', data.gold_labels ?? 0]]} />
    <div className="ta-card"><h2>错误类型分布</h2>{data.types?.length ? <table className="ta-table"><thead><tr><th>类型</th><th>加权次数</th><th>涉及知识点</th><th>班级</th></tr></thead><tbody>
      {data.types.map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.knowledge_points?.join('、') || '—'}</td><td>{item.classes?.join('、')}</td></tr>)}
    </tbody></table> : <p className="ta-muted">暂无错误类型证据（判题证据不足时不会凭空生成）。</p>}</div>
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
  if (timeline.length < 2 || !series.length) return <div className="ta-empty">暂无可比较数据：需要至少两个任务且证据达到门槛。不可比的任务不会强行连线。</div>;
  return <>
    <p className="ta-note">EWMA 只用于观察趋势，原始值保留；不可比的时间点显示为「证据不足」，不补 0。</p>
    <div className="ta-card"><h2>知识点趋势</h2><div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th className="ta-sticky">知识点</th>{timeline.map((point: Row) => <th key={point.batch_id}>{point.label || point.batch_id}</th>)}</tr></thead><tbody>
      {series.map((item: Row) => <tr key={item.knowledge_point_id}><td className="ta-sticky">{item.name}</td>{(item.points || []).map((value: Row) => <td key={value.batch_id} className={value.status === '证据不足' ? 'ta-muted' : ''}>{value.status === '证据不足' ? '证据不足' : pct(value.ewma_mastery ?? value.mastery_estimate)}</td>)}</tr>)}
    </tbody></table></div></div>
  </>;
}

function SuggestPanel({ data }: { data: Row }) {
  return <>
    <p className="ta-note">建议完全由证据与错误分布推导（规则阈值），不调用模型；证据不足时不会给出确定性结论。</p>
    {data.suggestions?.length ? <div className="ta-suggest-list">{data.suggestions.map((item: Row, index: number) => <div className="ta-suggest" key={`${item.class_code}-${item.knowledge_point_id}-${index}`}>
      <div className="ta-suggest-head"><strong>{item.class_code} · {item.knowledge_point_name}</strong><span>{pct(item.mastery)}</span></div>
      <p>{item.suggestion}</p>
      <small>有效学生 {item.valid_students} · 低于阈值 {item.weak_students} · 证据不足 {item.insufficient_students}</small>
    </div>)}</div> : <div className="ta-empty">当前没有达到证据门槛的薄弱知识点，暂无教学建议。</div>}
    <p className="ta-muted">依据：{data.evidence_basis?.rule} · 模型调用 {data.evidence_basis?.model_calls ?? 0} 次</p>
  </>;
}

function QuestionDetailPanel({ batchId, classId, questionId, onClose }: { batchId: string; classId: string; questionId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [confirmGrade, setConfirmGrade] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      setError(''); setDetail(null);
      try {
        const response = await apiFetch(`/api/teacher/analysis/classes/${encodeURIComponent(classId)}/questions/${encodeURIComponent(questionId)}?batch_id=${encodeURIComponent(batchId)}`, { signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
        setDetail(body);
      } catch (err) { if (!isAbortError(err)) setError(String(err)); }
    })();
    return () => controller.abort();
  }, [batchId, classId, questionId]);
  const overall = detail?.overall || {};
  return <div className="ta-card ta-drawer">
    <div className="ta-drawer-head"><h2>单题详情 · {questionId}</h2><button onClick={onClose}>关闭</button></div>
    {error && <div className="ta-error">读取失败：{error}</div>}
    {!detail && !error && <p className="ta-muted">正在加载题目数据…</p>}
    {detail && <>
      <Stats items={[['作答学生', overall.answered_students], ['有效判题', overall.valid_judgement_students], ['事件数', overall.answered_events], ['学生等权平均', overall.student_average_score ?? '—'], ['平均得分', overall.average_score ?? '—'], ['得分率', overall.score_rate == null ? '—' : `${overall.score_rate}%`]]} />
      <p className="ta-note">学生等权平均与事件平均是两个口径；未判题、待复核不按 0 分处理。</p>
      <h3>评分点</h3>
      <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>评分点</th><th>单人满分</th><th>累计得分</th><th>学生等权</th><th>事件平均</th><th>有效事件</th><th>有效学生</th><th>未映射</th><th>异常分</th><th>缺结果</th></tr></thead><tbody>
        {(detail.scoring_points || []).map((item: Row) => <tr key={item.scoring_point_id}><td>{item.scoring_point_id}</td><td>{item.full_score_consistent ? item.full_score : '满分不一致'}</td><td>{item.total_earned_score}</td><td>{item.student_average_score ?? '—'}</td><td>{item.event_average_score ?? '—'}</td><td>{item.valid_events}</td><td>{item.valid_students}</td><td>{item.unmapped_knowledge_count}</td><td>{item.invalid_score_count}</td><td>{item.missing_scoring_point_result_count}</td></tr>)}
      </tbody></table></div>
      <h3>学生表现</h3>
      <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>学生</th><th>状态</th><th>得分</th><th>错误</th><th>理由</th><th>操作</th></tr></thead><tbody>
        {(detail.students || []).map((item: Row) => <tr key={item.answer_id}><td>{item.student_code}</td><td>{item.status}</td><td>{item.score ?? '—'}/{item.max_score}</td><td>{item.error_type}</td><td>{item.judgment_reason || '—'}</td><td>{item.grade_id && <button onClick={() => setConfirmGrade(item.grade_id)}>评分点确认</button>}</td></tr>)}
      </tbody></table></div>
      {confirmGrade && <ScoringPointConfirm gradeId={confirmGrade} onClose={() => setConfirmGrade('')} />}
    </>}
  </div>;
}

function ScoringPointConfirm({ gradeId, onClose }: { gradeId: string; onClose: () => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const response = await apiFetch(`/api/teacher/grades/${encodeURIComponent(gradeId)}/scoring-points`, { signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
        setRows(body.items || []);
      } catch (err) { if (!isAbortError(err)) setError(String(err)); }
    })();
    return () => controller.abort();
  }, [gradeId]);
  const setScore = (id: string, value: string) => setRows(current => current.map(row => row.scoring_point_id === id ? { ...row, editScore: value } : row));
  const save = async () => {
    setSaving(true); setError('');
    try {
      const items = rows.map(row => ({ scoring_point_id: row.scoring_point_id, score: Number(row.editScore ?? row.confirmation?.confirmed_score ?? row.original_ai_score ?? 0), note: '教师逐评分点确认' }));
      const response = await apiFetch(`/api/teacher/grades/${encodeURIComponent(gradeId)}/scoring-points`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
      onClose();
    } catch (err) { setError(String(err)); } finally { setSaving(false); }
  };
  return <div className="ta-confirm">
    <h3>逐评分点确认</h3>
    <p className="ta-note">确认值会单独保存并写入历史；只确认评分点不等于整题已确认。</p>
    {error && <div className="ta-error">{error}</div>}
    <table className="ta-table"><thead><tr><th>评分点</th><th>满分</th><th>AI 原分</th><th>确认分</th></tr></thead><tbody>
      {rows.map((row: Row) => <tr key={row.scoring_point_id}><td>{row.scoring_point_id}</td><td>{row.max_score}</td><td>{row.original_ai_score}</td><td><input value={row.editScore ?? row.confirmation?.confirmed_score ?? row.original_ai_score ?? 0} onChange={event => setScore(row.scoring_point_id, event.target.value)} /></td></tr>)}
    </tbody></table>
    <div className="ta-tabs"><button disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存评分点确认'}</button><button onClick={onClose}>取消</button></div>
  </div>;
}
