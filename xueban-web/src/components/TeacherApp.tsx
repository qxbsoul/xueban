import { useEffect, useState } from 'react';
import { apiGet, apiPost, getAuthToken, type AuthUserDto } from '../api';

type Tab = 'workbench' | 'data' | 'grading' | 'results';
type Row = Record<string, any>;
const TABS: Array<{ id: Tab; label: string; hint: string }> = [
  { id: 'workbench', label: '教师工作台', hint: '总览' },
  { id: 'data', label: '数据管理', hint: '导入 / 课程' },
  { id: 'grading', label: '判题中心', hint: '范围 / 任务' },
  { id: 'results', label: '判题结果', hint: '逐步骤' },
];

async function upload(path: string, files: FileList | null): Promise<Row> {
  const form = new FormData();
  Array.from(files || []).forEach(file => form.append('files', file));
  const response = await fetch(path, { method: 'POST', headers: { Authorization: `Bearer ${getAuthToken()}` }, body: form });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail?.error?.message || body?.detail || '请求失败');
  return body;
}

export default function TeacherApp({ user, onLogout }: { user: AuthUserDto; onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>('workbench');
  const [summary, setSummary] = useState<Row | null>(null);
  const [meta, setMeta] = useState<Row>({ courses: [], standards: [], classes: [], exams: [] });
  const [batches, setBatches] = useState<Row[]>([]);
  const [jobs, setJobs] = useState<Row[]>([]);
  const [message, setMessage] = useState('');

  const refresh = async () => {
    const [s, m, b, j] = await Promise.all([
      apiGet<Row>('/api/teacher/summary'),
      apiGet<Row>('/api/teacher/meta'),
      apiGet<{ items: Row[] }>('/api/teacher/imports'),
      apiGet<{ items: Row[] }>('/api/teacher/grade/jobs'),
    ]);
    setSummary(s); setMeta(m); setBatches(b.items || []); setJobs(j.items || []);
  };
  useEffect(() => { void refresh().catch(error => setMessage(String(error))); }, []);

  return <div className="teacher-app">
    <aside className="teacher-side">
      <div className="teacher-brand"><span>学</span><div><strong>智慧学伴</strong><small>教师端</small></div></div>
      <nav>{TABS.map(item => <button key={item.id} className={tab === item.id ? 'on' : ''} onClick={() => setTab(item.id)}><span>{item.label}</span><small>{item.hint}</small></button>)}</nav>
      <div className="teacher-user"><div>{user.display_name?.slice(0, 1) || '师'}</div><span>{user.display_name}</span><button onClick={onLogout}>退出</button></div>
    </aside>
    <main className="teacher-main">
      <header className="teacher-head"><div><p>TEACHER CONSOLE · 原始数据 / 判题分层</p><h1>{TABS.find(item => item.id === tab)?.label}</h1></div><span className="teacher-live">数据库实时数据</span></header>
      {message && <div className="teacher-message">{message}</div>}
      {tab === 'workbench' && <Workbench summary={summary} batches={batches} jobs={jobs} onGo={setTab} />}
      {tab === 'data' && <DataManager meta={meta} batches={batches} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'grading' && <Grading meta={meta} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'results' && <Results meta={meta} setMessage={setMessage} />}
    </main>
  </div>;
}

function Workbench({ summary, batches, jobs, onGo }: { summary: Row | null; batches: Row[]; jobs: Row[]; onGo: (tab: Tab) => void }) {
  const cards = [
    ['班级', summary?.classes], ['学生', summary?.students], ['课程', summary?.courses], ['题目', summary?.questions],
    ['原始作答', summary?.answers], ['待判题', summary?.pending], ['已完成判题', summary?.graded], ['待复核', summary?.review],
    ['运行中任务', summary?.jobs_running],
  ];
  return <section className="teacher-section">
    <div className="teacher-cards">{cards.map(([label, value]) => <div className="teacher-card" key={String(label)}><span>{label}</span><strong>{value ?? '—'}</strong></div>)}</div>
    <div className="teacher-grid">
      <Panel title="最近导入批次">{batches.length === 0 ? <p className="teacher-muted">暂无导入批次。</p> : <MiniTable rows={batches.slice(0, 5)} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} />}</Panel>
      <Panel title="最近判题任务">{jobs.length === 0 ? <p className="teacher-muted">暂无判题任务。</p> : <MiniTable rows={jobs.slice(0, 5)} columns={[['status', '状态'], ['processed', '已处理'], ['total', '总数'], ['failed', '失败']]} />}</Panel>
    </div>
    <div className="teacher-actions"><button onClick={() => onGo('data')}>进入数据管理</button><button onClick={() => onGo('grading')}>进入判题中心</button><button onClick={() => onGo('results')}>查看判题结果</button></div>
  </section>;
}

function DataManager({ meta, batches, onChanged, setMessage }: { meta: Row; batches: Row[]; onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [mode, setMode] = useState<'import' | 'courses'>('import');
  const [files, setFiles] = useState<FileList | null>(null);
  const [preview, setPreview] = useState<Row | null>(null);
  const [result, setResult] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [courseId, setCourseId] = useState('');
  const [detail, setDetail] = useState<Row | null>(null);
  const [standardId, setStandardId] = useState('');
  const [standard, setStandard] = useState<Row | null>(null);
  const [classId, setClassId] = useState('');
  const [students, setStudents] = useState<Row[]>([]);
  const [studentDetail, setStudentDetail] = useState<Row | null>(null);

  const doPreview = async () => { setBusy(true); setMessage(''); try { setPreview(await upload('/api/teacher/imports/preview', files)); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const doImport = async () => { setBusy(true); setMessage(''); try { setResult(await upload('/api/teacher/imports', files)); await onChanged(); setMessage('导入完成，原始数据已写入数据库。'); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const openCourse = async (id: string) => { setCourseId(id); setStandardId(''); setStandard(null); setDetail(await apiGet<Row>(`/api/teacher/courses/${encodeURIComponent(id)}`)); };
  const openStandard = async (id: string) => { setStandardId(id); setStandard(await apiGet<Row>(`/api/teacher/standards/${encodeURIComponent(id)}`)); };
  const openClass = async (id: string) => { setClassId(id); setStudentDetail(null); setStudents((await apiGet<{ items: Row[] }>(`/api/teacher/classes/${encodeURIComponent(id)}/students`)).items || []); };
  const openStudent = async (id: string) => setStudentDetail(await apiGet<Row>(`/api/teacher/students/${encodeURIComponent(id)}`));

  return <section className="teacher-section">
    <div className="teacher-tabs"><button className={mode === 'import' ? 'on' : ''} onClick={() => setMode('import')}>数据导入</button><button className={mode === 'courses' ? 'on' : ''} onClick={() => setMode('courses')}>课程与学生</button></div>
    {mode === 'import' ? <div className="teacher-grid">
      <Panel title="上传赛题 CSV">
        <p className="teacher-muted">必须主动选择文件。一次可多选学生、课程要求、测验节点、题目、作答记录。没有文件时不会读取服务器测试目录。</p>
        <input type="file" multiple accept=".csv" onChange={event => setFiles(event.target.files)} />
        <div className="teacher-actions"><button disabled={busy} onClick={() => void doPreview()}>预览与校验</button><button disabled={busy} onClick={() => void doImport()}>确认导入</button></div>
        {preview && <div className="teacher-result"><h3>预览结果</h3><p>错误 {preview.error_count ?? 0} 条</p>{preview.files?.map((file: Row) => <div className="teacher-file" key={file.filename}><strong>{file.filename}</strong><span>{file.rows} 行</span><small>{file.fields?.join('、')}</small></div>)}{(preview.errors || []).slice(0, 8).map((error: Row, index: number) => <p className="teacher-error" key={index}>{error.message}</p>)}</div>}
        {result && <div className="teacher-result"><h3>导入完成</h3><pre>{JSON.stringify(result.counts, null, 2)}</pre>{(result.errors || []).length > 0 && <p>异常记录 {result.errors.length} 条，已保留在批次中。</p>}</div>}
      </Panel>
      <Panel title="历史导入批次"><MiniTable rows={batches} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} /></Panel>
    </div> : <div className="teacher-split">
      <Panel title="课程">{meta.courses?.map((course: Row) => <button className={courseId === course.id ? 'teacher-row on' : 'teacher-row'} key={course.id} onClick={() => void openCourse(course.id)}><span>{course.title}</span><small>{course.code} · {course.standards} 个标准</small></button>)}
        {detail && <><h3>课程标准</h3>{detail.standards?.map((item: Row) => <button className={standardId === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void openStandard(item.id)}><span>{item.code} · {item.version}</span><small>{item.source_pages}</small></button>)}</>}
      </Panel>
      <Panel title={standard ? `课程标准 ${standard.standard.code}` : '课程标准详情'}>{standard ? <>{JSON.parse(standard.standard.content_json || '[]').map((item: Row, index: number) => <div className="teacher-step" key={index}><strong>{item.topic} · {item.topic_title}</strong><p>{item.goal}</p><small>{item.indicator} · {item.description}</small></div>)}</> : <p className="teacher-muted">选择课程后再查看对应课程标准，不会跨课程混用。</p>}
        {detail?.topics && <><h3>主题与指标</h3>{detail.topics.map((topic: Row) => <div className="teacher-step" key={topic.id}><strong>{topic.code} · {topic.title}</strong><small>{topic.standard_id}</small></div>)}</>}
      </Panel>
    </div>}
    <div className="teacher-split" style={{ marginTop: 14 }}>
      <Panel title="班级与学生">{meta.classes?.map((item: Row) => <button className={classId === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void openClass(item.id)}><span>{item.code} 班</span><small>{item.student_count} 人</small></button>)}{students.map((item: Row) => <button className="teacher-row" key={item.id} onClick={() => void openStudent(item.id)}><span>{item.code}</span><small>{item.answers} 条作答</small></button>)}</Panel>
      <Panel title={studentDetail ? `${studentDetail.student.code} 原始作答` : '学生原始作答'}>{studentDetail ? studentDetail.answers?.map((answer: Row) => <div className="teacher-step" key={answer.id}><strong>{answer.question_id} · {answer.event_id} r{answer.revision}</strong><p>{answer.original_answer || '未作答'}</p><small>{answer.score_status} · 原始得分 {answer.original_score}/{answer.max_score}</small></div>) : <p className="teacher-muted">选择班级和学生查看原始作答。</p>}</Panel>
    </div>
  </section>;
}

function Grading({ meta, onChanged, setMessage }: { meta: Row; onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [courseId, setCourseId] = useState('');
  const [standardId, setStandardId] = useState('');
  const [examId, setExamId] = useState('');
  const [classId, setClassId] = useState('');
  const [answers, setAnswers] = useState<Row[]>([]);
  const [job, setJob] = useState<Row | null>(null);
  const [jobItems, setJobItems] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Row | null>(null);
  const standards = (meta.standards || []).filter((item: Row) => !courseId || item.course_id === courseId);
  const exams = (meta.exams || []).filter((item: Row) => !courseId || item.course_id === courseId);

  const loadAnswers = async () => {
    const query = new URLSearchParams({ status: 'pending', limit: '500' });
    if (courseId) query.set('course_id', courseId);
    if (standardId) query.set('standard_id', standardId);
    if (examId) query.set('exam_node_id', examId);
    if (classId) query.set('class_id', classId);
    setAnswers((await apiGet<{ items: Row[] }>(`/api/teacher/answers?${query}`)).items || []);
  };
  useEffect(() => { void loadAnswers().catch(error => setMessage(String(error))); }, [courseId, standardId, examId, classId]);

  const start = async () => {
    setBusy(true); setMessage('正在创建统一判题任务…');
    try {
      const data = await apiPost<{ job: Row }>('/api/teacher/grade/batch', { course_id: courseId, standard_id: standardId, exam_node_id: examId, class_id: classId, status: 'pending', llm_key: localStorage.getItem('sf_deepseek_key') || '' });
      setJob(data.job); setMessage(`任务已创建：${data.job.total} 条待判题。`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  useEffect(() => {
    if (!job?.id || job.status === 'completed' || job.status === 'partially_failed' || job.status === 'failed') return;
    const timer = window.setInterval(async () => {
      const data = await apiGet<{ job: Row; items: Row[] }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
      setJob(data.job); setJobItems((data as Row).items || []);
      if (data.job.status !== 'running' && data.job.status !== 'queued') window.clearInterval(timer);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);
  const gradeOne = async (id: string) => {
    setBusy(true);
    try { await apiPost(`/api/teacher/answers/${encodeURIComponent(id)}/grade`, { llm_key: localStorage.getItem('sf_deepseek_key') || '' }); await loadAnswers(); await onChanged(); setMessage('单题判题完成，旧判题历史保留。'); } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const open = async (answer: Row) => setDetail(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(answer.id)}`));
  const retry = async () => { if (!job?.id) return; const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/retry`, { llm_key: localStorage.getItem('sf_deepseek_key') || '' }); setJob(data.job); setJobItems([]); setMessage('失败项已创建新任务，旧判题记录保留。'); };
  const types = answers.reduce((acc: Record<string, number>, row: Row) => { const key = row.question_type || '未知'; acc[key] = (acc[key] || 0) + 1; return acc; }, {});
  const selectedCourse = meta.courses?.find((item: Row) => item.id === courseId);
  const selectedStandard = standards.find((item: Row) => item.id === standardId);

  return <section className="teacher-section">
    <div className="teacher-panel teacher-scope">
      <div><label>课程<select value={courseId} onChange={event => { setCourseId(event.target.value); setStandardId(''); setExamId(''); }}><option value="">全部课程</option>{meta.courses?.map((item: Row) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <label>课程标准<select value={standardId} onChange={event => setStandardId(event.target.value)}><option value="">全部标准</option>{standards.map((item: Row) => <option key={item.id} value={item.id}>{item.code} · {item.version}</option>)}</select></label>
        <label>测验节点<select value={examId} onChange={event => setExamId(event.target.value)}><option value="">全部节点</option>{exams.map((item: Row) => <option key={item.id} value={item.id}>{item.id} · {item.exam_date}</option>)}</select></label>
        <label>班级<select value={classId} onChange={event => setClassId(event.target.value)}><option value="">全部班级</option>{meta.classes?.map((item: Row) => <option key={item.id} value={item.id}>{item.code}</option>)}</select></label></div>
      <div className="teacher-scope-summary"><strong>{answers.length}</strong><span>条待判题</span><small>{selectedCourse?.title || '全部课程'} · {selectedStandard?.code || '全部标准'} · {Object.entries(types).map(([key, value]) => `${key} ${value}`).join(' / ') || '无题目'}</small><small>参考答案、原始评分点与课程标准共同作为依据</small></div>
      <div className="teacher-actions"><button disabled={busy || answers.length === 0} onClick={() => void start()}>按当前范围批量判题</button></div>
    </div>
    {job && <div className="teacher-job"><strong>判题任务 {job.id}</strong><span>{job.status} · 已完成 {job.completed ?? job.processed}/{job.total} · 失败 {job.failed} · 待复核 {job.needs_review}</span><progress value={job.processed || 0} max={Math.max(1, job.total || 1)} /><small>当前：{job.current_answer_id || '—'}</small>{jobItems.filter((item: Row) => item.status === 'failed').map((item: Row) => <p className="teacher-error" key={item.id}>失败 {item.answer_id}：{item.error || '未知错误'}</p>)}{job.failed > 0 ? <button onClick={() => void retry()}>重试失败项</button> : null}{job.status === 'partially_failed' || job.status === 'completed' ? <button onClick={() => setMessage('判题结果已写入，请到判题结果页查看。')}>查看结果入口</button> : null}</div>}
    <div className="teacher-split">
      <Panel title="待判题任务队列">{answers.map(answer => <button className="teacher-answer-row" key={answer.id} onClick={() => void open(answer)}><div><strong>{answer.question_id}</strong><span>{answer.student_code} · {answer.event_id} r{answer.revision}</span></div><small>{answer.question_type}</small></button>)}</Panel>
      <Panel title="作答与依据">{detail ? <AnswerDetail detail={detail} onGrade={() => void gradeOne(detail.answer.id)} busy={busy} /> : <p className="teacher-muted">选择一条待判题查看原始作答、课程标准和评分依据。</p>}</Panel>
    </div>
  </section>;
}

function AnswerDetail({ detail, onGrade, busy }: { detail: Row; onGrade: () => void; busy: boolean }) {
  return <><h3>{detail.question?.question_type} · {detail.question?.max_score} 分</h3><p><strong>题目：</strong>{detail.question?.stem}</p><p><strong>参考答案：</strong>{detail.question?.reference_answer}</p><p><strong>参考解析：</strong>{detail.question?.reference_solution}</p><p><strong>原始评分点：</strong>{detail.question?.scoring_points}</p><p><strong>课程标准：</strong>{detail.standard?.code || '未关联'} {detail.standard?.version || ''}</p><div className="teacher-answer-box"><strong>学生原始作答</strong><p>{detail.answer.original_answer || '未作答'}</p><pre>{detail.answer.solution_process || '无解题过程'}</pre></div><button disabled={busy} onClick={onGrade}>判题 / 重新判题</button>{detail.grades?.[0] && <GradeView grade={detail.grades[0]} />}</>;
}

function GradeView({ grade }: { grade: Row }) {
  const steps = JSON.parse(grade.step_results_json || '[]');
  return <div className="teacher-grade"><strong>{grade.score}/{grade.max_score}</strong><span>{grade.is_correct} · {grade.grading_model} · {grade.status}</span><p>{grade.feedback}</p><small>{grade.judgment_reason}</small>{steps.map((step: Row) => <div className={step.is_correct ? 'teacher-step ok' : 'teacher-step'} key={step.step_index}><strong>步骤 {step.step_index}</strong><p>{step.student_step}</p><small>{step.is_correct ? '正确' : '错误'} · {step.score}/{step.max_score} · {step.error_type} · {step.judgment_reason}</small></div>)}</div>;
}

function Results({ meta, setMessage }: { meta: Row; setMessage: (v: string) => void }) {
  const [filters, setFilters] = useState({ course_id: '', standard_id: '', class_id: '', status: '', review: '' });
  const [items, setItems] = useState<Row[]>([]);
  const [detail, setDetail] = useState<Row | null>(null);
  const [manual, setManual] = useState({ score: '', note: '' });
  const load = async () => {
    const query = new URLSearchParams({ limit: '500' });
    Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
    setItems((await apiGet<{ items: Row[] }>(`/api/teacher/results?${query}`)).items || []);
  };
  useEffect(() => { void load().catch(error => setMessage(String(error))); }, [filters]);
  const open = async (id: string) => setDetail(await apiGet<Row>(`/api/teacher/results/${encodeURIComponent(id)}`));
  const save = async () => {
    if (!detail?.grade?.id) return;
    try { await apiPost(`/api/teacher/grades/${encodeURIComponent(detail.grade.id)}/manual`, { score: Number(manual.score), feedback: '教师人工修正', note: manual.note }); setMessage('人工修正已保存，原 AI 判题记录保留。'); setDetail(await apiGet<Row>(`/api/teacher/results/${encodeURIComponent(detail.grade.id)}`)); await load(); } catch (error) { setMessage(String(error)); }
  };
  return <section className="teacher-section">
    <div className="teacher-panel teacher-filters"><select value={filters.course_id} onChange={event => setFilters({ ...filters, course_id: event.target.value })}><option value="">全部课程</option>{meta.courses?.map((item: Row) => <option key={item.id} value={item.id}>{item.title}</option>)}</select><select value={filters.standard_id} onChange={event => setFilters({ ...filters, standard_id: event.target.value })}><option value="">全部标准</option>{meta.standards?.map((item: Row) => <option key={item.id} value={item.id}>{item.code}</option>)}</select><select value={filters.class_id} onChange={event => setFilters({ ...filters, class_id: event.target.value })}><option value="">全部班级</option>{meta.classes?.map((item: Row) => <option key={item.id} value={item.id}>{item.code}</option>)}</select><select value={filters.status} onChange={event => setFilters({ ...filters, status: event.target.value })}><option value="">全部状态</option><option value="graded">graded</option><option value="needs_review">needs_review</option><option value="failed">failed</option><option value="manual_corrected">manual_corrected</option></select><label className="teacher-check"><input type="checkbox" checked={filters.review === '1'} onChange={event => setFilters({ ...filters, review: event.target.checked ? '1' : '' })} />只看待复核</label><button onClick={() => void load()}>刷新</button></div>
    <div className="teacher-split"><Panel title={`判题结果（${items.length}）`}><table className="teacher-table"><thead><tr><th>学生</th><th>班级</th><th>题目</th><th>版本</th><th>得分</th><th>状态</th><th>判题版本</th></tr></thead><tbody>{items.map(item => <tr key={item.id} onClick={() => void open(item.id)}><td>{item.student_code}</td><td>{item.class_code}</td><td>{item.question_id}</td><td>r{item.revision}</td><td>{item.score}/{item.max_score}</td><td>{item.needs_review ? '待复核' : item.status}</td><td>{item.grading_version}</td></tr>)}</tbody></table></Panel>
      <Panel title="完整详情">{detail ? <ResultDetail detail={detail} manual={manual} setManual={setManual} onSave={save} /> : <p className="teacher-muted">选择一条判题结果查看题目、评分依据、逐步骤和历史版本。</p>}</Panel></div>
  </section>;
}

function ResultDetail({ detail, manual, setManual, onSave }: { detail: Row; manual: { score: string; note: string }; setManual: (v: { score: string; note: string }) => void; onSave: () => void }) {
  const grade = detail.grade;
  return <><h3>{detail.question?.stem}</h3><p><strong>参考答案：</strong>{detail.question?.reference_answer}</p><p><strong>参考解析：</strong>{detail.question?.reference_solution}</p><p><strong>评分点：</strong>{detail.question?.scoring_points}</p><p><strong>课程标准：</strong>{detail.standard?.code || '未关联'} {detail.standard?.version || ''}</p><div className="teacher-answer-box"><strong>学生原始作答</strong><p>{detail.answer.original_answer}</p><pre>{detail.answer.solution_process || '无解题过程'}</pre></div><GradeView grade={grade} /><h3>历史判题版本</h3>{detail.history?.map((item: Row) => <div className="teacher-step" key={item.id}><strong>v{item.grading_version} · {item.score}/{item.max_score}</strong><small>{item.grading_model} · {item.status} · {item.created_at}</small></div>)}<h3>人工修正</h3><input placeholder="修正分数" value={manual.score} onChange={event => setManual({ ...manual, score: event.target.value })} /><input placeholder="修改原因" value={manual.note} onChange={event => setManual({ ...manual, note: event.target.value })} /><button onClick={onSave}>保存修正</button>{detail.corrections?.map((item: Row) => <p className="teacher-muted" key={item.id}>{item.created_at} · {item.note}</p>)}</>;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) { return <div className="teacher-panel"><h2>{title}</h2>{children}</div>; }
function MiniTable({ rows, columns }: { rows: Row[]; columns: Array<[string, string]> }) { return <table className="teacher-table"><thead><tr>{columns.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.id || index}>{columns.map(([key]) => <td key={key}>{String(row[key] ?? '—')}</td>)}</tr>)}</tbody></table>; }
