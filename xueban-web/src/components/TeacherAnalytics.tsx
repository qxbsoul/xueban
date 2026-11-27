import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../api';
import './teacher-analytics.css';

type Row = Record<string, any>;

const SECTIONS: Array<{ id: string; label: string; hint: string }> = [
  { id: 'class', label: '班级学情', hint: '选择班级，查看掌握分布、题目表现与教学建议' },
  { id: 'compare', label: '班级比较', hint: '同一任务内班级知识点差异与样本提示' },
  { id: 'student', label: '学生画像', hint: '按班级浏览学生，查看个人长期与本次表现' },
  { id: 'knowledge', label: '知识点分析', hint: '知识点 × 学生掌握矩阵' },
  { id: 'errors', label: '错误类型分析', hint: '错误类型、题目与判题状态' },
  { id: 'trend', label: '学习趋势', hint: '只使用可比任务的历史趋势' },
  { id: 'dependency', label: '知识依赖', hint: '知识点前后置关系与证据' },
];

const STATUS_ORDER = ['熟练掌握', '基本掌握', '初步掌握', '掌握不足', '证据不足'];
const STATUS_COLOR: Record<string, string> = { 熟练掌握: '#15803d', 基本掌握: '#65a30d', 初步掌握: '#d97706', 掌握不足: '#dc2626', 证据不足: '#cbd5e1' };
const STATUS_CLASS: Record<string, string> = { 熟练掌握: 'st-5', 基本掌握: 'st-4', 初步掌握: 'st-3', 掌握不足: 'st-2', 证据不足: 'st-1' };
const ANSWER_STATUS_LABEL: Record<string, string> = { graded: '有效判题', needs_review: '待复核', failed: '失败', pending: '未判题', not_answered: '未作答', manual_corrected: '人工修正', manual_confirmed: '人工确认' };

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
  const [section, setSection] = useState('class');
  const [batches, setBatches] = useState<Row[]>([]);
  const [scope, setScope] = useState<'current' | 'all'>('current');
  const [batchId, setBatchId] = useState('');
  const [cards, setCards] = useState<Row[]>([]);
  const [classId, setClassId] = useState('');
  const [classProfile, setClassProfile] = useState<Row | null>(null);
  const [compare, setCompare] = useState<Row | null>(null);
  const [studentList, setStudentList] = useState<Row | null>(null);
  const [studentId, setStudentId] = useState('');
  const [studentProfile, setStudentProfile] = useState<Row | null>(null);
  const [studentSemantic, setStudentSemantic] = useState<Row | null>(null);
  const [classSemantic, setClassSemantic] = useState<Row | null>(null);
  const [matrix, setMatrix] = useState<Row | null>(null);
  const [errors, setErrors] = useState<Row | null>(null);
  const [trend, setTrend] = useState<Row | null>(null);
  const [graph, setGraph] = useState<Row | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [ladder, setLadder] = useState<{ kind: 'students'; kp: Row; status: string } | { kind: 'kp'; kpId: string; name: string } | null>(null);
  const [kpDetail, setKpDetail] = useState<Row | null>(null);
  const [questionId, setQuestionId] = useState('');
  const [questionDetail, setQuestionDetail] = useState<Row | null>(null);
  const request = useAbortableRequest();

  const clearDetail = () => { setClassId(''); setStudentId(''); setStudentProfile(null); setStudentSemantic(null); setClassProfile(null); setKpDetail(null); setQuestionId(''); };
  const changeScope = (next: 'current' | 'all') => { if (next === scope) return; clearDetail(); setScope(next); };
  const changeBatch = (next: string) => { if (next === effectiveBatch) return; clearDetail(); setBatchId(next); };
  const effectiveBatch = batchId || batches[0]?.id || '';
  const allBatchIds = useMemo(() => batches.map(item => item.id), [batches]);
  const effectiveBatches = scope === 'current' ? [effectiveBatch].filter(Boolean) : allBatchIds;

  const run = useCallback(async (task: () => Promise<void>) => {
    setLoading(true); setLoadError('');
    try { await task(); }
    catch (err) { setLoadError(String(err)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void run(async () => {
      const data = await request<{ items: Row[] }>('batches', '/api/teacher/batches');
      if (!data) return;
      setBatches(data.items || []);
      setBatchId(current => current || data.items?.[0]?.id || '');
    });
  }, [request, run]);

  useEffect(() => {
    if (!effectiveBatch) return;
    void run(async () => {
      const data = await request<{ cards: Row[] }>('cards', `/api/teacher/analysis/class-cards?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (!data) return;
      const list = data.cards || [];
      setCards(list);
      setClassId(current => (list.some(item => item.id === current) ? current : (list[0]?.id || '')));
    });
  }, [effectiveBatch, request, run]);

  useEffect(() => {
    if (!effectiveBatch || !classId) return;
    void run(async () => {
      const query = scope === 'current'
        ? `/api/teacher/analysis/classes/${encodeURIComponent(classId)}?batch_id=${encodeURIComponent(effectiveBatch)}`
        : `/api/teacher/analysis/classes/${encodeURIComponent(classId)}?batch_id=${encodeURIComponent(effectiveBatch)}&scope=multi&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}`;
      const data = await request<Row>('classProfile', query);
      if (data) setClassProfile(data);
    });
  }, [effectiveBatch, classId, scope, effectiveBatches.join(','), request, run]);

  useEffect(() => {
    if (section !== 'class' || !effectiveBatch || !classId) return;
    void (async () => {
      const data = await request<Row>('classSemantic', `/api/teacher/analysis/classes/${encodeURIComponent(classId)}/semantic?batch_id=${encodeURIComponent(effectiveBatch)}`).catch(() => null);
      setClassSemantic(data ?? null);
    })();
  }, [section, effectiveBatch, classId, request]);

  useEffect(() => {
    const status = classSemantic?.task?.status;
    if (section !== 'class' || !effectiveBatch || !classId || (status !== 'pending' && status !== 'running')) return;
    const timer = window.setInterval(async () => {
      const data = await request<Row>('classSemantic', `/api/teacher/analysis/classes/${encodeURIComponent(classId)}/semantic?batch_id=${encodeURIComponent(effectiveBatch)}`).catch(() => undefined);
      if (data) setClassSemantic(data);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [classSemantic, section, effectiveBatch, classId, request]);

  useEffect(() => {
    const status = studentSemantic?.task?.status;
    if (section !== 'student' || !studentId || !effectiveBatch || (status !== 'pending' && status !== 'running')) return;
    const timer = window.setInterval(async () => {
      const data = await request<Row>('studentSemantic', `/api/teacher/analysis/students/${encodeURIComponent(studentId)}/semantic?batch_id=${encodeURIComponent(effectiveBatch)}`).catch(() => undefined);
      if (data) setStudentSemantic(data);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [studentSemantic, section, studentId, effectiveBatch, request]);

  useEffect(() => {
    if (section !== 'compare') { setCompare(null); return; }
    if (!effectiveBatch || cards.length < 2) return;
    void run(async () => {
      const data = await request<Row>('compare', `/api/teacher/analysis/compare-detailed?batch_id=${encodeURIComponent(effectiveBatch)}&class_ids=${encodeURIComponent(cards.map(item => item.id).join(','))}`);
      if (data) setCompare(data);
    });
  }, [section, effectiveBatch, cards, request, run]);

  useEffect(() => {
    if (section !== 'student' || !effectiveBatch || studentId) return;
    void run(async () => {
      const data = await request<Row>('studentList', `/api/teacher/analysis/student-list?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setStudentList(data);
    });
  }, [section, effectiveBatch, studentId, request, run]);

  useEffect(() => {
    if (section !== 'student' || !studentId || !effectiveBatch) return;
    void run(async () => {
      const query = scope === 'current'
        ? `batch_id=${encodeURIComponent(effectiveBatch)}&scope=single`
        : `batch_id=${encodeURIComponent(effectiveBatch)}&scope=all&batch_ids=${encodeURIComponent(effectiveBatches.join(','))}`;
      const data = await request<Row>('student', `/api/teacher/analysis/students/${encodeURIComponent(studentId)}?${query}`);
      if (data) setStudentProfile(data);
    });
  }, [section, studentId, scope, effectiveBatch, effectiveBatches.join(','), request, run]);

  useEffect(() => {
    if (section !== 'student' || !studentId || !effectiveBatch) { setStudentSemantic(null); return; }
    void (async () => {
      const data = await request<Row>('studentSemantic', `/api/teacher/analysis/students/${encodeURIComponent(studentId)}/semantic?batch_id=${encodeURIComponent(effectiveBatch)}`).catch(() => null);
      setStudentSemantic(data ?? null);
    })();
  }, [section, studentId, effectiveBatch, request]);

  useEffect(() => {
    if (section !== 'knowledge' || !effectiveBatch) return;
    void run(async () => {
      const data = await request<Row>('matrix', `/api/teacher/analysis/knowledge-matrix?batch_id=${encodeURIComponent(effectiveBatch)}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}`);
      if (data) setMatrix(data);
    });
  }, [section, effectiveBatch, classId, request, run]);

  useEffect(() => {
    if (section !== 'errors' || !effectiveBatch) return;
    void run(async () => {
      const data = await request<Row>('errors', `/api/teacher/analysis/errors?batch_id=${encodeURIComponent(effectiveBatch)}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}`);
      if (data) setErrors(data);
    });
  }, [section, effectiveBatch, classId, request, run]);

  useEffect(() => {
    if (section !== 'trend' || !effectiveBatch || !classId) return;
    void run(async () => {
      const data = await request<Row>('trend', `/api/teacher/analysis/trend/class/${encodeURIComponent(classId)}?batch_ids=${encodeURIComponent(effectiveBatches.join(','))}`);
      if (data) setTrend(data);
    });
  }, [section, effectiveBatch, classId, effectiveBatches.join(','), request, run]);

  useEffect(() => {
    if (section !== 'dependency' || !effectiveBatch) return;
    void run(async () => {
      const data = await request<Row>('graph', `/api/teacher/knowledge-graph?batch_id=${encodeURIComponent(effectiveBatch)}`);
      if (data) setGraph(data);
    });
  }, [section, effectiveBatch, request, run]);

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

  const selectSection = (id: string) => {
    setSection(id); setLoadError(''); setLadder(null); setQuestionId('');
    if (id !== 'student') setStudentId('');
  };
  const openStudent = (id: string) => { setStudentId(id); setSection('student'); };
  const backToStudents = () => { setStudentId(''); setStudentProfile(null); setStudentSemantic(null); };

  return <div className="ta-shell">
    <aside className="ta-nav" aria-label="学情分析二级导航">
      <div className="ta-nav-title">学情分析</div>
      {SECTIONS.map(item => <button key={item.id} className={`ta-nav-item ${section === item.id ? 'on' : ''}`} onClick={() => selectSection(item.id)} title={item.hint}>{item.label}</button>)}
      <div className="ta-nav-foot"><span className={`ta-dot ${loading ? 'busy' : ''}`} />{loading ? '加载中' : '已就绪'}</div>
    </aside>

    <section className="ta-main">
      <header className="ta-page-head">
        <div>
          <div className="ta-crumbs"><button onClick={() => selectSection('class')}>学情分析</button><em>/</em><strong>{SECTIONS.find(item => item.id === section)?.label}</strong></div>
          <h1>{SECTIONS.find(item => item.id === section)?.label}</h1>
          <p>{SECTIONS.find(item => item.id === section)?.hint}</p>
        </div>
        <div className="ta-scope" role="group" aria-label="数据范围">
          <label>数据范围</label>
          <div className="ta-segmented">
            <button className={scope === 'current' ? 'on' : ''} onClick={() => changeScope('current')}>本次任务</button>
            <button className={scope === 'all' ? 'on' : ''} onClick={() => changeScope('all')}>全部任务</button>
          </div>
          {scope === 'current' && <select value={effectiveBatch} onChange={event => changeBatch(event.target.value)} aria-label="具体任务">
            {batches.map(item => <option key={item.id} value={item.id}>{item.filename || item.id}</option>)}
          </select>}
        </div>
      </header>

      {loadError && <div className="ta-error">读取失败：{loadError}<button onClick={() => selectSection(section)}>重试</button></div>}
      {!effectiveBatch && <div className="ta-empty">还没有可分析的任务，请先在「数据管理」导入 CSV。</div>}

      {section === 'class' && <>
        <ClassCards cards={cards} selected={classId} onSelect={setClassId} />
        {!classId && !loading && <div className="ta-empty">当前任务没有班级。</div>}
        {classProfile && <ClassPanel
          data={classProfile}
          semantic={classSemantic}
          onOpenStudent={openStudent}
          onOpenQuestion={(qid) => void openQuestion(qid)}
          onOpenStatus={(kp, status) => setLadder({ kind: 'students', kp, status })}
          onOpenKp={(kp) => void openKp(kp.knowledge_point_id, kp.name)}
        />}
      </>}

      {section === 'compare' && (compare ? <ComparePanel data={compare} /> : <SectionLoading loading={loading} empty="至少需要两个班级才能对比。" />)}
      {section === 'student' && (studentId
        ? <StudentDetail data={studentProfile} semantic={studentSemantic} scope={scope} batchId={scope === 'current' ? effectiveBatch : ''} batchIds={scope === 'all' ? allBatchIds.join(',') : ''} onBack={backToStudents} />
        : <StudentGroups data={studentList} loading={loading} onOpen={openStudent} />)}
      {section === 'knowledge' && (matrix ? <KnowledgePanel data={matrix} /> : <SectionLoading loading={loading} empty="暂无可展示的知识点矩阵。" />)}
      {section === 'errors' && (errors ? <ErrorsPanel data={errors} /> : <SectionLoading loading={loading} empty="暂无错误数据。" />)}
      {section === 'trend' && (trend ? <TrendPanel data={trend} /> : <SectionLoading loading={loading} empty="暂无趋势数据。" />)}
      {section === 'dependency' && (graph ? <DependencyPanel data={graph} /> : <SectionLoading loading={loading} empty="暂无知识依赖数据。" />)}

      {ladder?.kind === 'students' && <StudentLadder kp={ladder.kp} status={ladder.status} onClose={() => setLadder(null)} onOpenStudent={(id) => { setLadder(null); openStudent(id); }} />}
      {ladder?.kind === 'kp' && <KpDrawer data={kpDetail} name={ladder.name} onClose={() => setLadder(null)} onOpenQuestion={(qid) => void openQuestion(qid)} />}
      {(ladder || questionId) && <button className="ta-backdrop" aria-label="关闭详情" onClick={() => { setLadder(null); setQuestionId(''); }} />}
      {questionId && <QuestionDrawer data={questionDetail} questionId={questionId} onClose={() => setQuestionId('')} />}
    </section>
  </div>;
}

function SectionLoading({ loading, empty }: { loading: boolean; empty: string }) {
  if (loading) return <div className="ta-skeleton"><span /><span /><span /></div>;
  return <div className="ta-empty">{empty}</div>;
}

function ClassCards({ cards, selected, onSelect }: { cards: Row[]; selected: string; onSelect: (id: string) => void }) {
  if (!cards.length) return null;
  return <div className="ta-cards">
    {cards.map(card => <button key={card.id} className={`ta-class-card ${card.id === selected ? 'on' : ''}`} onClick={() => onSelect(card.id)}>
      <div className="ta-class-card-head"><strong>{card.code}</strong><span>{card.student_count} 人</span></div>
      <div className="ta-class-card-metrics">
        <div><b>{pct(card.average_mastery)}</b><span>平均掌握</span></div>
        <div><b>{pct(card.knowledge_point_coverage)}</b><span>知识点覆盖</span></div>
        <div><b>{card.at_risk_count}</b><span>重点关注</span></div>
      </div>
    </button>)}
  </div>;
}

function Stats({ items }: { items: Array<[string, unknown]> }) {
  return <div className="ta-stats">{items.map(([label, value]) => <div key={label}><b>{value == null || value === '' ? '—' : String(value)}</b><span>{label}</span></div>)}</div>;
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

function ClassPanel({ data, semantic, onOpenStudent, onOpenQuestion, onOpenStatus, onOpenKp }: { data: Row; semantic: Row | null; onOpenStudent: (id: string) => void; onOpenQuestion: (id: string) => void; onOpenStatus: (kp: Row, status: string) => void; onOpenKp: (kp: Row) => void }) {
  const questions = data.questions || [];
  const sum = (key: string) => questions.reduce((total: number, item: Row) => total + (item[key] || 0), 0);
  const expectedKnown = questions.length > 0 && questions.every((item: Row) => item.expected != null);
  const scope = {
    expected: expectedKnown ? sum('expected') : null,
    pool: sum('pool_reference'),
    answered: sum('answered'), graded: sum('graded'), review: sum('review'),
    failed: sum('failed'), pending: sum('pending'), confirmed: sum('confirmed_unsubmitted'),
    unknownQuestions: questions.filter((item: Row) => item.expected == null).length,
    outOfScope: sum('out_of_scope_answered'),
  };
  const scopeCards = [
    ['应作答组合数', scope.expected == null ? '无法确定' : scope.expected, '逐生题单缺失时不推算'],
    ['实际提交组合数', scope.answered, '按学生+题目+任务去重'],
    ['成功判题', scope.graded, '有有效判题结果'],
    ['待复核', scope.review, '判题结果待人工复核'],
    ['判题失败', scope.failed, '记录失败，不等于 0 分'],
    ['尚未判题', scope.pending, '已提交但未判'],
    ['确认未提交', scope.confirmed, '仅有明确未提交标记'],
    ['无法确定应答范围', scope.unknownQuestions, '道题缺少逐生题单'],
  ];
  return <>
    <Stats items={[['班级', data.class?.code], ['学生', data.student_count], ['有效估计', data.valid_student_count], ['整体掌握', pct(data.overall_mastery)], ['薄弱率', pct(data.overall_weak_rate)], ['题目池参考', scope.pool || '—']]} />
    <div className="ta-scope-metrics">{scopeCards.map(([label, value, note]) => <div className="ta-scope-metric" key={String(label)}><strong>{value}</strong><span>{label}</span><small>{note}</small></div>)}</div>
    {scope.expected == null && <p className="ta-note">应作答无法确定：当前任务只有题目池（按测验节点出现的题目），没有逐个学生的题单或可靠参与范围。题目池参考 {scope.pool} 不是应作答数，不能用来推算未提交。{scope.outOfScope > 0 ? `另有 ${scope.outOfScope} 条作答超出题目池范围。` : ''}</p>}

    <div className="ta-card">
      <h2>知识点掌握分布</h2>
      <p className="ta-muted">每段是一个掌握状态的人数，点击区段查看名单；点击知识点名称查看详情。证据不足单独一段，不按 0 分。</p>
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
      <h2>题目表现</h2>
      <p className="ta-muted">应作答 = 范围内实际提交 + 确认未提交；实际提交 = 成功 + 待复核 + 失败 + 尚未判题。只有逐生题单才能确定应作答；题目池不放大的未提交。点击行查看单题学生名单。</p>
      {questions.length ? <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>题目</th><th>应作答</th><th>实际提交</th><th>成功</th><th>待复核</th><th>失败</th><th>未判</th><th>确认未提交</th><th>无法确定</th><th>平均分</th><th>得分率</th></tr></thead><tbody>
        {questions.map((item: Row) => <tr key={item.question_id} className="ta-clickable" onClick={() => onOpenQuestion(item.question_id)}>
          <td>{item.question_id}</td><td>{item.expected ?? '无法确定'}</td>
          <td>{item.answered}</td>
          <td>{item.graded}</td><td>{item.review}</td><td>{item.failed}</td><td>{item.pending}</td>
          <td>{item.confirmed_unsubmitted || 0}</td><td>{item.expected == null ? '是' : '否'}</td>
          <td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td>
        </tr>)}
      </tbody></table></div> : <p className="ta-muted">{data.question_scope_note || '暂无题目数据。'}</p>}
    </div>

    <SemanticCard title="班级语义分析" semantic={semantic} />

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

function SemanticCard({ title, semantic }: { title: string; semantic: Row | null }) {
  const task = semantic?.task || {};
  const content = semantic?.result?.llm || null;
  return <div className="ta-card">
    <h2>{title}</h2>
    {!semantic && <p className="ta-muted">语义分析加载中…</p>}
    {semantic && !content && <p className="ta-muted">{task.status === 'running' || task.status === 'pending' ? '语义分析生成中，完成后自动显示。' : task.error ? `语义分析暂不可用：${task.error}` : '暂无语义分析结果。'}</p>}
    {content && <>
      {content.class_observations?.length > 0 && <div className="ta-suggest-list">{content.class_observations.map((item: Row, index: number) => <div className="ta-suggest" key={`obs-${index}`}>
        <div className="ta-suggest-head"><strong>{item.class_code} · 班级观察</strong></div>
        <p>{item.summary}</p>
        <div className="ta-chips">{(item.evidence_refs || []).map((ref: string) => <span key={ref}>{ref}</span>)}</div>
      </div>)}</div>}
      {content.teaching_suggestions?.length > 0 && <><h3>教学建议</h3><div className="ta-suggest-list">{content.teaching_suggestions.map((item: Row, index: number) => <div className="ta-suggest" key={`sug-${index}`}>
        <div className="ta-suggest-head"><strong>{item.class_code}</strong><span>{item.confidence != null ? `置信度 ${Math.round(item.confidence * 100)}%` : ''}</span></div>
        <p>{item.suggestion}</p>
        <div className="ta-chips">{(item.evidence_refs || []).map((ref: string) => <span key={ref}>{ref}</span>)}</div>
      </div>)}</div></>}
      <p className="ta-muted">模型 {semantic?.result?.model || '—'} · 提示词 {semantic?.result?.prompt_version || '—'} · 证据版本 {semantic?.result?.evidence_version || '—'}</p>
    </>}
  </div>;
}

function ComparePanel({ data }: { data: Row }) {
  const classes = data.classes || [];
  const rows = (data.knowledge_points || []).filter((row: Row) => row.pp_difference != null).slice(0, 16);
  return <div className="ta-card">
    <div className="ta-compare-legend">{classes.map((cls: Row) => <span key={cls.class_id}><i />{cls.class_code}（有效 {cls.valid_student_count}）</span>)}<em>按差异绝对值排序，差值单位 pp</em></div>
    <div className="ta-compare-bars">{rows.map((row: Row) => <div className={`ta-compare-row ${row.comparable ? '' : 'insufficient'}`} key={row.knowledge_point_id}>
      <div className="ta-compare-name" title={row.name}>{row.name}</div>
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
      <div><h3>班级优势知识点</h3>{(data.advantages || []).length ? <ul>{data.advantages.slice(0, 6).map((item: Row) => <li key={item.knowledge_point_id}>{item.class} · {item.name} {pp(item.pp_difference)}</li>)}</ul> : <p className="ta-muted">无明显优势（差值 &lt; 10pp）。</p>}</div>
      <div><h3>共同薄弱知识点</h3>{(data.common_weak || []).length ? <ul>{data.common_weak.slice(0, 6).map((item: Row) => <li key={item.knowledge_point_id}>{item.name}（{Object.entries(item.means).map(([code, mean]) => `${code} ${pct(mean as number)}`).join(' / ')}）</li>)}</ul> : <p className="ta-muted">暂无共同薄弱项。</p>}</div>
      <div><h3>样本不足</h3>{(data.insufficient || []).length ? <ul>{data.insufficient.slice(0, 6).map((item: Row) => <li key={item.knowledge_point_id}>{item.name}：{item.reason}</li>)}</ul> : <p className="ta-muted">所有知识点样本量满足比较要求。</p>}</div>
    </div>
    <p className="ta-muted">{data.note}</p>
  </div>;
}

function StudentGroups({ data, loading, onOpen }: { data: Row | null; loading: boolean; onOpen: (id: string) => void }) {
  if (!data) return <SectionLoading loading={loading} empty="当前任务没有学生数据。" />;
  const groups = data.groups || [];
  if (!groups.length) return <div className="ta-empty">当前任务没有学生数据。</div>;
  return <div className="ta-card">
    <h2>选择学生</h2>
    <p className="ta-muted">按班级分组；点击一行进入该学生画像。</p>
    {groups.map((group: Row) => <div className="ta-group" key={group.class_id}>
      <div className="ta-group-title">{group.class_code} <small>{group.students.length} 人</small></div>
      {group.students.map((student: Row) => <button className="ta-student-row" key={student.id} onClick={() => onOpen(student.id)}>
        <strong>{student.code}</strong>
        <span>得分 {student.total_score == null ? '—' : `${student.total_score}/${student.total_max_score}`}</span>
        <span>掌握 {pct(student.mastery)}</span>
        <span className={student.weak_count > 0 ? 'ta-warn-text' : ''}>薄弱知识点 {student.weak_count}</span>
        <em>查看画像</em>
      </button>)}
    </div>)}
  </div>;
}

function StudentDetail({ data, semantic, scope, batchId, batchIds, onBack }: { data: Row | null; semantic: Row | null; scope: string; batchId: string; batchIds: string; onBack: () => void }) {
  return <>
    <div className="ta-section-head">
      <button className="ta-back" onClick={onBack}>← 返回学生列表</button>
      <span className="ta-scope-note">数据范围：{scope === 'current' ? '本次任务' : '全部任务'}</span>
    </div>
    {!data ? <SectionLoading loading empty="正在计算学生画像…" /> : <>
      <Stats items={[['学生', data.student?.code], ['已作答', data.coverage?.answered_question_count ?? '—'], ['有效判题', data.coverage?.graded_question_count ?? 0], ['证据覆盖', pct(data.coverage?.evidence_coverage)], ['证据条数', data.data_quality?.evidence_count ?? 0], ['可分析知识点', data.mastery?.length ?? 0]]} />
      {(data.excluded_batches || []).length > 0 && <div className="ta-warning-box"><strong>以下任务未参与合并（不可比）</strong><ul>{data.excluded_batches.map((item: Row) => <li key={item.batch_id}>{item.batch_id}：{item.reasons.join('；')}</li>)}</ul></div>}
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
        <SemanticCard title="学生语义诊断" semantic={semantic} />
      </div>
      <EvidenceSection studentId={data.student?.id || ''} batchId={batchId} batchIds={batchIds} />
    </>}
  </>;
}

function EvidenceSection({ studentId, batchId, batchIds }: { studentId: string; batchId: string; batchIds: string }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [question, setQuestion] = useState('');
  const [point, setPoint] = useState('');
  const [errorType, setErrorType] = useState('');
  const [keyword, setKeyword] = useState('');
  const [applied, setApplied] = useState('');
  const [data, setData] = useState<Row | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!studentId || (!batchId && !batchIds)) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ scope: batchIds ? 'all' : 'single', page: String(page), page_size: String(pageSize), question_id: question, scoring_point_id: point, error_type: errorType, keyword: applied });
    if (batchIds) params.set('batch_ids', batchIds); else params.set('batch_id', batchId);
    setLoading(true); setError('');
    fetch(`/api/teacher/analysis/students/${encodeURIComponent(studentId)}/evidence?${params.toString()}`, { headers: { Authorization: `Bearer ${window.localStorage.getItem('sf_xueban_session') || ''}` }, signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error(`读取失败 ${response.status}`); return response.json(); })
      .then(setData).catch(err => { if (err.name !== 'AbortError') setError(String(err)); }).finally(() => setLoading(false));
    return () => controller.abort();
  }, [studentId, batchId, batchIds, page, pageSize, question, point, errorType, applied]);
  const resetPage = (setter: (value: string) => void) => (value: string) => { setter(value); setPage(1); };
  return <div className="ta-card">
    <h2>评分点证据</h2>
    <div className="ta-evidence-filters">
      <select value={question} onChange={event => resetPage(setQuestion)(event.target.value)}><option value="">全部题目</option>{data?.facets?.questions?.map((value: string) => <option key={value} value={value}>{value}</option>)}</select>
      <select value={point} onChange={event => resetPage(setPoint)(event.target.value)}><option value="">全部评分点</option>{data?.facets?.scoring_points?.map((value: string) => <option key={value} value={value}>{value}</option>)}</select>
      <select value={errorType} onChange={event => resetPage(setErrorType)(event.target.value)}><option value="">全部错误类型</option>{data?.facets?.error_types?.map((value: string) => <option key={value} value={value}>{value}</option>)}</select>
      <input value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索证据关键词" onKeyDown={event => { if (event.key === 'Enter') { setApplied(keyword); setPage(1); } }} />
      <button onClick={() => { setApplied(keyword); setPage(1); }}>搜索</button>
      <select value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1); }}><option value={20}>20 条/页</option><option value={50}>50 条/页</option><option value={100}>100 条/页</option></select>
    </div>
    {error && <p className="ta-error">{error}</p>}
    {loading && <div className="ta-skeleton"><span /><span /><span /></div>}
    {data && !loading && <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>题目</th><th>评分点</th><th>得分</th><th>证据</th><th>错误类型</th><th>来源/置信度</th></tr></thead><tbody>
      {data.items.map((item: Row, index: number) => <tr key={`${item.answer_id}-${item.scoring_point_id}-${index}`}><td>{item.question_id}</td><td>{item.scoring_point_id}</td><td>{item.earned_score}/{item.full_score}</td><td className="ta-evidence">{item.evidence_text || '—'}</td><td>{item.error_type || '—'}</td><td>{item.judgment_source || '—'}{item.error_label_confidence != null ? ` · ${item.error_label_confidence}` : ''}</td></tr>)}
      {!data.items.length && <tr><td colSpan={6} className="ta-muted">当前筛选没有证据。</td></tr>}
    </tbody></table></div>}
    {data && <div className="ta-pager"><span>共 {data.total} 条 · 第 {data.page}/{Math.max(1, Math.ceil(data.total / data.page_size))} 页</span><button disabled={data.page <= 1} onClick={() => setPage(data.page - 1)}>上一页</button><button disabled={!data.has_more} onClick={() => setPage(data.page + 1)}>下一页</button><small>{data.unit}</small></div>}
  </div>;
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
    <div className="ta-card"><h2>题目错误与判题状态</h2>{data.by_question?.length ? <div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>题目</th><th>作答</th><th>有效判题</th><th>待复核</th><th>失败</th><th>平均分</th><th>得分率</th></tr></thead><tbody>
      {data.by_question.map((item: Row) => <tr key={item.question_id}><td>{item.question_id}</td><td>{item.answered}</td><td>{item.valid}</td><td>{item.review}</td><td>{item.failed}</td><td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td></tr>)}
    </tbody></table></div> : <p className="ta-muted">暂无题目数据。</p>}</div>
    <p className="ta-note">错误类型只是分类标签，不代表掌握程度；没有人工 gold 标签时不报告 precision/recall。</p>
  </>;
}

function TrendPanel({ data }: { data: Row }) {
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

function DependencyPanel({ data }: { data: Row }) {
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  const nameOf = (id: string) => nodes.find((node: Row) => node.knowledge_point_id === id || node.id === id)?.name || id;
  const topics = useMemo(() => {
    const map = new Map<string, Row[]>();
    nodes.forEach((node: Row) => map.set(node.topic_title || '未分组', [...(map.get(node.topic_title || '未分组') || []), node]));
    return Array.from(map.entries());
  }, [nodes]);
  return <>
    <p className="ta-note">依赖来自知识点前后置关系；没有证据的节点不参与掌握推断。{!edges.length && ' 当前任务还没有已确认的依赖边，下面按主题展示知识点清单。'}</p>
    {edges.length > 0 && <div className="ta-card"><h2>依赖关系（{edges.length}）</h2><div className="ta-matrix-wrap"><table className="ta-table"><thead><tr><th>前置知识点</th><th>后置知识点</th><th>权重</th><th>来源</th></tr></thead><tbody>
      {edges.map((edge: Row, index: number) => <tr key={edge.id || index}><td>{nameOf(edge.from_kp || edge.from_id || edge.source)}</td><td>{nameOf(edge.to_kp || edge.to_id || edge.target)}</td><td>{edge.weight ?? '—'}</td><td>{edge.source_type || edge.source || '—'}</td></tr>)}
    </tbody></table></div></div>}
    {!edges.length && topics.map(([topic, items]) => <div className="ta-card" key={topic}>
      <h2>{topic}</h2>
      <div className="ta-chips">{items.map((node: Row) => <span key={node.knowledge_point_id || node.code}>{node.name || node.code}</span>)}</div>
    </div>)}
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
      <p className="ta-note">统计范围：{data.scope?.basis === 'assignment' ? '逐生题单' : data.scope?.basis === 'class_roster' ? '班级题单 + 在册学生' : '题目池参考，不能推算未提交'}；{data.scope?.note || ''}{data.scope?.pool_reference ? `；题目池参考 ${data.scope.pool_reference} 人次` : ''}</p>
      <div className="ta-name-lists">
        {[['实际提交', data.lists?.answered], ['成功判题', data.lists?.graded], ['待复核', data.lists?.review], ['判题失败', data.lists?.failed], ['尚未判题', data.lists?.pending], ['确认未提交', data.lists?.confirmed_unsubmitted], ['未作答（仅题单确定时）', data.lists?.not_answered], ['超出题目池', data.lists?.out_of_scope]].map(([label, list]: any) => <div key={label}><strong>{label} {(list || []).length}</strong><span>{(list || []).join('、') || '—'}</span></div>)}
      </div>
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
      <Stats items={[['应作答', overall.expected ?? '无法确定'], ['实际提交', overall.answered], ['成功判题', overall.graded], ['待复核', overall.review], ['判题失败', overall.failed], ['尚未判题', overall.pending], ['确认未提交', overall.confirmed_unsubmitted ?? 0], ['无法确定范围', overall.expected == null ? '是' : '否'], ['平均分', overall.student_average_score ?? '—'], ['得分率', overall.score_rate == null ? '—' : `${overall.score_rate}%`]]} />
      <p className="ta-note">{overall.expected_note}；判题失败不等于 0 分，未判题、确认未提交、无法确定范围三者分开统计。</p>
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
