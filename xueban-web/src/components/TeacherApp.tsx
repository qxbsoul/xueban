import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost, getAuthToken, type AuthUserDto } from '../api';
import { createLearningCourse } from '../learning-api';

type Tab = 'workbench' | 'textbooks' | 'data' | 'grading' | 'results';
type Row = Record<string, any>;
const TABS: Array<{ id: Tab; label: string; hint: string }> = [
  { id: 'workbench', label: '教师工作台', hint: '总览' },
  { id: 'textbooks', label: '学案课程', hint: '上传 / 共享' },
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

const STATUS_LABEL: Record<string,string> = {
  graded: '已判题', needs_review: '待复核', failed: '判题失败',
  manual_confirmed: '已确认', manual_corrected: '已人工修正',
  pending: '待判题', running: '运行中', completed: '已完成',
  partially_failed: '部分失败', queued: '排队中',
};
function statusLabel(value: unknown): string { return STATUS_LABEL[String(value || '')] || String(value || '—'); }

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

  return <div className="app teacher-app">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">学</div><div className="brand-name">智慧学伴</div></div>
      <div className="nav-label">教师端</div>
      {TABS.map(item => <button key={item.id} className={tab === item.id ? 'nav-item active' : 'nav-item'} onClick={() => setTab(item.id)}><span className="nav-text">{item.label}</span><small>{item.hint}</small></button>)}
      <div className="sidebar-foot"><div className="user-card"><div className="user-avatar">{user.display_name?.slice(0, 1) || '师'}</div><div className="user-info"><div className="user-name">{user.display_name}</div><div className="user-role">教师端</div></div></div><button className="account-action" onClick={onLogout}>退出当前账号</button></div>
    </aside>
    <div className="main teacher-main">
      <header className="teacher-head"><div><p>TEACHER CONSOLE · 原始数据 / 判题分层</p><h1>{TABS.find(item => item.id === tab)?.label}</h1></div><span className="teacher-live">数据库实时数据</span></header>
      {message && <div className="teacher-message">{message}</div>}
      {tab === 'workbench' && <Workbench summary={summary} batches={batches} jobs={jobs} onGo={setTab} />}
      {tab === 'textbooks' && <TextbookCenter setMessage={setMessage} />}
      {tab === 'data' && <DataManager meta={meta} batches={batches} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'grading' && <Grading meta={meta} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'results' && <Results meta={meta} setMessage={setMessage} />}
    </div>
  </div>;
}

function Workbench({ summary, batches, jobs, onGo }: { summary: Row | null; batches: Row[]; jobs: Row[]; onGo: (tab: Tab) => void }) {
  const cards = [
    ['班级', summary?.classes], ['学生', summary?.students], ['题目', summary?.questions],
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
  const [audit, setAudit] = useState<Row | null>(null);
  const [courseId, setCourseId] = useState('');
  const [detail, setDetail] = useState<Row | null>(null);
  const [standardId, setStandardId] = useState('');
  const [standard, setStandard] = useState<Row | null>(null);
  const [classId, setClassId] = useState('');
  const [students, setStudents] = useState<Row[]>([]);
  const [studentDetail, setStudentDetail] = useState<Row | null>(null);

  useEffect(() => { apiGet<Row>('/api/teacher/audit').then(setAudit).catch(() => setAudit(null)); }, []);
  const doPreview = async () => { setBusy(true); setMessage(''); try { setPreview(await upload('/api/teacher/imports/preview', files)); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const doImport = async () => { setBusy(true); setMessage(''); try { setResult(await upload('/api/teacher/imports', files)); await onChanged(); setMessage('导入完成，原始数据已写入数据库。'); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const openCourse = async (id: string) => { setCourseId(id); setStandardId(''); setStandard(null); setDetail(await apiGet<Row>(`/api/teacher/courses/${encodeURIComponent(id)}`)); };
  const openStandard = async (id: string) => { setStandardId(id); setStandard(await apiGet<Row>(`/api/teacher/standards/${encodeURIComponent(id)}`)); };
  const openClass = async (id: string) => { setClassId(id); setStudentDetail(null); setStudents((await apiGet<{ items: Row[] }>(`/api/teacher/classes/${encodeURIComponent(id)}/students`)).items || []); };
  const openStudent = async (id: string) => setStudentDetail(await apiGet<Row>(`/api/teacher/students/${encodeURIComponent(id)}`));

  return <section className="teacher-section">
    <div className="teacher-tabs"><button className="on">数据导入</button></div>
    {mode === 'import' ? <div className="teacher-grid">
      <Panel title="上传赛题 CSV">
        <p className="teacher-muted">必须主动选择文件。一次可多选学生、课程要求、测验节点、题目、作答记录。没有文件时不会读取服务器测试目录。</p>
        <input type="file" multiple accept=".csv" onChange={event => setFiles(event.target.files)} />
        <div className="teacher-actions"><button disabled={busy} onClick={() => void doPreview()}>预览与校验</button><button disabled={busy} onClick={() => void doImport()}>确认导入</button></div>
        {preview && <div className="teacher-result"><h3>预览结果</h3><p>错误 {preview.error_count ?? 0} 条</p>{preview.files?.map((file: Row) => <div className="teacher-file" key={file.filename}><strong>{file.filename}</strong><span>{file.rows} 行</span><small>{file.fields?.join('、')}</small></div>)}{(preview.errors || []).slice(0, 8).map((error: Row, index: number) => <p className="teacher-error" key={index}>{error.message}</p>)}</div>}
        {result && <div className="teacher-result"><h3>导入完成</h3><pre>{JSON.stringify(result.counts, null, 2)}</pre>{(result.errors || []).length > 0 && <p>异常记录 {result.errors.length} 条，已保留在批次中。</p>}</div>}
      </Panel>
      <Panel title="历史导入批次"><MiniTable rows={batches} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} /></Panel>
    </div> : null}
    {audit && <div className="teacher-panel" style={{ marginTop: 14 }}><h2>数据核对</h2><table className="teacher-table"><thead><tr><th>数据类型</th><th>CSV 原始</th><th>数据库</th><th>差异</th></tr></thead><tbody>{[["students","学生"],["classes","班级"],["questions","题目"],["exams","测验节点"],["answers","原始作答"]].map(([key,label]) => { const raw = key === 'answers' ? audit.batch?.counts?.answers_source : audit.batch?.counts?.[key]; const db = audit.database?.[key]; return <tr key={key}><td>{label}</td><td>{raw ?? "—"}</td><td>{db ?? "—"}</td><td>{raw == null || db == null ? "—" : db - raw}</td></tr>; })}</tbody></table></div>}
    <div className="teacher-split" style={{ marginTop: 14 }}>
      <Panel title="班级与学生">{meta.classes?.map((item: Row) => <button className={classId === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void openClass(item.id)}><span>{item.code} 班</span><small>{item.student_count} 人</small></button>)}{students.map((item: Row) => <button className="teacher-row" key={item.id} onClick={() => void openStudent(item.id)}><span>{item.code}</span><small>{item.answers} 条作答</small></button>)}</Panel>
      <Panel title={studentDetail ? `${studentDetail.student.code} 原始作答` : '学生原始作答'}>{studentDetail ? studentDetail.answers?.map((answer: Row) => <div className="teacher-step" key={answer.id}><strong>{answer.question_id} · {answer.event_id} r{answer.revision}</strong><p>{answer.original_answer || '未作答'}</p><small>{answer.score_status} · 原始得分 {answer.original_score}/{answer.max_score}</small></div>) : <p className="teacher-muted">选择班级和学生查看原始作答。</p>}</Panel>
    </div>
  </section>;
}

function Grading({ meta, onChanged, setMessage }: { meta: Row; onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [examId, setExamId] = useState('');
  const [classId, setClassId] = useState('');
  const [answers, setAnswers] = useState<Row[]>([]);
  const [job, setJob] = useState<Row | null>(null);
  const [jobItems, setJobItems] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Row | null>(null);
  const [textbooks, setTextbooks] = useState<Row[]>([]);
  const [textbookId, setTextbookId] = useState('');
  const [chapters, setChapters] = useState<Row[]>([]);
  const [chapterId, setChapterId] = useState('');
  const [chapterContext, setChapterContext] = useState('');
  const exams = (meta.exams || []);

  useEffect(() => {
    apiGet<Row[]>('/api/learning/courses').then((data) => setTextbooks((Array.isArray(data) ? data : []).filter((course) => course.status === 'ready'))).catch(() => setTextbooks([]));
  }, []);
  useEffect(() => {
    if (!textbookId) { setChapters([]); setChapterId(''); setChapterContext(''); return; }
    apiGet<Row>(`/api/learning/courses/${encodeURIComponent(textbookId)}`).then((data) => {
      const flat = (data.chapters || []).flatMap((chapter: Row) => (chapter.sections || []).map((section: Row) => ({ ...section, chapter_title: chapter.title })));
      setChapters(flat); setChapterId(''); setChapterContext('');
    }).catch(() => setChapters([]));
  }, [textbookId]);
  const chooseChapter = (id: string) => {
    setChapterId(id);
    const chapter = chapters.find((item) => item.id === id);
    setChapterContext(chapter?.lesson?.content || chapter?.summary || '');
  };

  const loadAnswers = async () => {
    const query = new URLSearchParams({ status: 'pending', limit: '500' });
    if (examId) query.set('exam_node_id', examId);
    if (classId) query.set('class_id', classId);
    setAnswers((await apiGet<{ items: Row[] }>(`/api/teacher/answers?${query}`)).items || []);
  };
  useEffect(() => { void loadAnswers().catch(error => setMessage(String(error))); }, [examId, classId]);

  const start = async () => {
    setBusy(true); setMessage('正在创建统一判题任务…');
    try {
      const data = await apiPost<{ job: Row }>('/api/teacher/grade/batch', { exam_node_id: examId, class_id: classId, status: 'pending', textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterContext });
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
    try { await apiPost(`/api/teacher/answers/${encodeURIComponent(id)}/grade`, { textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterContext }); await loadAnswers(); await onChanged(); setMessage('单题判题完成，旧判题历史保留。'); } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const open = async (answer: Row) => setDetail(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(answer.id)}`));
  const retry = async () => { if (!job?.id) return; const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/retry`, {}); setJob(data.job); setJobItems([]); setMessage('失败项已创建新任务，旧判题记录保留。'); };
  const types = answers.reduce((acc: Record<string, number>, row: Row) => { const key = row.question_type || '未知'; acc[key] = (acc[key] || 0) + 1; return acc; }, {});
  const selectedBook = textbooks.find((item: Row) => item.id === textbookId);

  return <section className="teacher-section">
    <div className="teacher-panel"><h2>第一步：选择参考教材（可选）</h2><p className="teacher-muted">教材只提供知识背景，不替代题目评分点。</p><select value={textbookId} onChange={event => setTextbookId(event.target.value)}><option value="">不使用教材辅助</option>{textbooks.map((book: Row) => <option key={book.id} value={book.id}>{book.title} · {book.subject} · {book.grade}</option>)}</select>{textbookId && <><select value={chapterId} onChange={event => chooseChapter(event.target.value)}><option value="">不指定章节</option>{chapters.map((chapter: Row) => <option key={chapter.id} value={chapter.id}>{chapter.chapter_title} / {chapter.title}</option>)}</select>{chapterContext && <p className="teacher-muted">已读取章节内容 {chapterContext.length} 字</p>}</>}</div>
    <div className="teacher-panel teacher-scope">
      <div><label>测验节点<select value={examId} onChange={event => setExamId(event.target.value)}><option value="">全部节点</option>{exams.map((item: Row) => <option key={item.id} value={item.id}>{item.id} · {item.exam_date}</option>)}</select></label>
        <label>班级<select value={classId} onChange={event => setClassId(event.target.value)}><option value="">全部班级</option>{meta.classes?.map((item: Row) => <option key={item.id} value={item.id}>{item.code}</option>)}</select></label></div>
      <div className="teacher-scope-summary"><strong>{answers.length}</strong><span>条待判题</span><small>{selectedBook?.title || '未选择学案课程'} · {Object.entries(types).map(([key, value]) => `${key} ${value}`).join(' / ') || '无题目'}</small><small>题目参考答案、解析和原始评分点决定分数；学案只提供知识背景。</small></div>
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
  return <><h3>{detail.question?.question_type} · {detail.question?.max_score} 分</h3><p><strong>题目：</strong>{detail.question?.stem}</p><p><strong>参考答案：</strong>{detail.question?.reference_answer}</p><p><strong>参考解析：</strong>{detail.question?.reference_solution}</p><p><strong>原始评分点：</strong>{detail.question?.scoring_points}</p><div className="teacher-answer-box"><strong>学生原始作答</strong><p>{detail.answer.original_answer || '未作答'}</p><pre>{detail.answer.solution_process || '无解题过程'}</pre></div><button disabled={busy} onClick={onGrade}>判题 / 重新判题</button>{detail.grades?.[0] && <GradeView grade={detail.grades[0]} />}</>;
}

function GradeView({ grade }: { grade: Row }) {
  const steps = JSON.parse(grade.step_results_json || '[]');
  return <div className="teacher-grade"><strong>{grade.score}/{grade.max_score}</strong><span>{grade.is_correct} · {grade.grading_model} · {statusLabel(grade.status)}</span><p>{grade.feedback}</p><small>{grade.judgment_reason}</small>{steps.map((step: Row) => <div className={step.is_correct ? 'teacher-step ok' : 'teacher-step'} key={step.step_index}><strong>步骤 {step.step_index}</strong><p>{step.student_step}</p><small>{step.is_correct ? '正确' : '错误'} · {step.score}/{step.max_score} · {step.error_type} · {step.judgment_reason}</small></div>)}</div>;
}

function Results({ meta, setMessage }: { meta: Row; setMessage: (v: string) => void }) {
  const [filters, setFilters] = useState({ course_id: '', standard_id: '', class_id: '', status: '', review: '' });
  const [items, setItems] = useState<Row[]>([]);
  const [detail, setDetail] = useState<Row | null>(null);
  const [manual, setManual] = useState({ score: '', note: '' });
  const [personal, setPersonal] = useState<Row[]>([]);
  const [classStats, setClassStats] = useState<Row[]>([]);
  const load = async () => {
    const query = new URLSearchParams({ limit: '500' });
    Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
    setItems((await apiGet<{ items: Row[] }>(`/api/teacher/results?${query}`)).items || []);
    const scoreQuery = new URLSearchParams(); if (filters.class_id) scoreQuery.set('class_id', filters.class_id);
    setPersonal((await apiGet<{ items: Row[] }>(`/api/teacher/scores/personal?${scoreQuery}`)).items || []);
    setClassStats((await apiGet<{ items: Row[] }>(`/api/teacher/scores/classes?${scoreQuery}`)).items || []);
  };
  useEffect(() => { void load().catch(error => setMessage(String(error))); }, [filters]);
  const open = async (id: string) => setDetail(await apiGet<Row>(`/api/teacher/results/${encodeURIComponent(id)}`));
  const save = async () => {
    if (!detail?.grade?.id) return;
    try { await apiPost(`/api/teacher/grades/${encodeURIComponent(detail.grade.id)}/manual`, { score: Number(manual.score), feedback: '教师人工修正', note: manual.note }); setMessage('人工修正已保存，原 AI 判题记录保留。'); setDetail(await apiGet<Row>(`/api/teacher/results/${encodeURIComponent(detail.grade.id)}`)); await load(); } catch (error) { setMessage(String(error)); }
  };
  const confirm = async () => {
    if (!detail?.grade?.id) return;
    try { await apiPost(`/api/teacher/grades/${encodeURIComponent(detail.grade.id)}/confirm`, {}); setMessage('已确认 AI 判题结果。'); setDetail(await apiGet<Row>(`/api/teacher/results/${encodeURIComponent(detail.grade.id)}`)); await load(); } catch (error) { setMessage(String(error)); }
  };
  return <section className="teacher-section">
    <div className="teacher-panel teacher-filters"><select value={filters.class_id} onChange={event => setFilters({ ...filters, class_id: event.target.value })}><option value="">全部班级</option>{meta.classes?.map((item: Row) => <option key={item.id} value={item.id}>{item.code}</option>)}</select><select value={filters.status} onChange={event => setFilters({ ...filters, status: event.target.value })}><option value="">全部状态</option><option value="graded">graded</option><option value="needs_review">needs_review</option><option value="failed">failed</option><option value="manual_corrected">manual_corrected</option><option value="manual_confirmed">manual_confirmed</option></select><label className="teacher-check"><input type="checkbox" checked={filters.review === '1'} onChange={event => setFilters({ ...filters, review: event.target.checked ? '1' : '' })} />只看待复核</label><button onClick={() => void load()}>刷新</button></div>
    <div className="teacher-split"><Panel title={`判题结果（${items.length}）`}><table className="teacher-table"><thead><tr><th>学生</th><th>班级</th><th>题目</th><th>版本</th><th>得分</th><th>状态</th><th>判题版本</th></tr></thead><tbody>{items.map(item => <tr key={item.id} onClick={() => void open(item.id)}><td>{item.student_code}</td><td>{item.class_code}</td><td>{item.question_id}</td><td>r{item.revision}</td><td>{item.score}/{item.max_score}</td><td>{item.needs_review ? '待复核' : statusLabel(item.status)}</td><td>{item.grading_version}</td></tr>)}</tbody></table></Panel>
      <Panel title="完整详情">{detail ? <ResultDetail detail={detail} manual={manual} setManual={setManual} onSave={save} onConfirm={confirm} /> : <p className="teacher-muted">选择一条判题结果查看题目、评分依据、逐步骤和历史版本。</p>}</Panel></div>
    <div className="teacher-split">
      <Panel title="个人测验成绩"><table className="teacher-table"><thead><tr><th>学生</th><th>班级</th><th>测验</th><th>已判/总题</th><th>得分</th><th>得分率</th></tr></thead><tbody>{personal.map((row, index) => <tr key={`${row.student_id}-${row.exam_node_id}-${index}`}><td>{row.student_code}</td><td>{row.class_code}</td><td>{row.exam_node_id}</td><td>{row.graded_questions}/{row.total_questions}</td><td>{row.score}/{row.graded_max_score}</td><td>{row.score_rate == null ? '未完成' : `${row.score_rate}%`}</td></tr>)}</tbody></table></Panel>
      <Panel title="班级成绩"><table className="teacher-table"><thead><tr><th>班级</th><th>测验</th><th>参与/完成</th><th>平均分</th><th>平均得分率</th><th>最高/最低</th></tr></thead><tbody>{classStats.map((row, index) => <tr key={`${row.class_id}-${row.exam_node_id}-${index}`}><td>{row.class_code}</td><td>{row.exam_node_id}</td><td>{row.participants}/{row.completed_students}</td><td>{row.average_score ?? '—'}</td><td>{row.average_rate == null ? '—' : `${row.average_rate}%`}</td><td>{row.highest ?? '—'} / {row.lowest ?? '—'}</td></tr>)}</tbody></table></Panel>
    </div>
  </section>;
}

function ResultDetail({ detail, manual, setManual, onSave, onConfirm }: { detail: Row; manual: { score: string; note: string }; setManual: (v: { score: string; note: string }) => void; onSave: () => void; onConfirm: () => void }) {
  const grade = detail.grade;
  return <><h3>{detail.question?.stem}</h3><p><strong>参考答案：</strong>{detail.question?.reference_answer}</p><p><strong>参考解析：</strong>{detail.question?.reference_solution}</p><p><strong>评分点：</strong>{detail.question?.scoring_points}</p><div className="teacher-answer-box"><strong>学生原始作答</strong><p>{detail.answer.original_answer}</p><pre>{detail.answer.solution_process || '无解题过程'}</pre></div><GradeView grade={grade} /><h3>历史判题版本</h3>{detail.history?.map((item: Row) => <div className="teacher-step" key={item.id}><strong>v{item.grading_version} · {item.score}/{item.max_score}</strong><small>{item.grading_model} · {item.status} · {item.created_at}</small></div>)}<h3>人工复核</h3><button onClick={onConfirm}>确认 AI 结果</button><input placeholder="修正分数" value={manual.score} onChange={event => setManual({ ...manual, score: event.target.value })} /><input placeholder="修改原因" value={manual.note} onChange={event => setManual({ ...manual, note: event.target.value })} /><button onClick={onSave}>保存修正</button>{detail.corrections?.map((item: Row) => <p className="teacher-muted" key={item.id}>{item.created_at} · {item.note}</p>)}</>;
}

function TextbookCenter({ setMessage }: { setMessage: (v: string) => void }) {
  const [items, setItems] = useState<Row[]>([]);
  const [title, setTitle] = useState('');
  const [subject, setSubject] = useState('数学');
  const [grade, setGrade] = useState('七年级');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Row | null>(null);
  const [lessonFile, setLessonFile] = useState<File | null>(null);

  const load = async () => {
    const data = await apiGet<Row[] | { items?: Row[] }>('/api/learning/courses');
    setItems(Array.isArray(data) ? data : data?.items || []);
  };
  useEffect(() => { void load(); const timer = window.setInterval(() => { void load(); }, 3000); return () => window.clearInterval(timer); }, []);
  const sharedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    items.filter((course) => course.status === 'ready').forEach((course) => {
      if (sharedRef.current.has(course.id)) return;
      sharedRef.current.add(course.id);
      void shareAll(course);
    });
  }, [items]);

  const uploadBook = async () => {
    if (!file || !title.trim()) { setMessage('请填写教材名称并选择文件。'); return; }
    setBusy(true); setMessage('正在上传并创建教材解析任务…');
    try {
      const result = await createLearningCourse({ title, subject, grade, source_type: 'textbook', source_text: '', file });
      setMessage(`教材已创建，解析任务 ${result.job.id} 已启动。`);
      setFile(null); setTitle(''); await load();
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
;
;
  const importLesson = async () => {
    if (!lessonFile) { setMessage('请选择学生端导出的 Markdown 学案文件。'); return; }
    setBusy(true); setMessage('正在导入已生成学案…');
    try {
      const form = new FormData(); form.append('file', lessonFile);
      const response = await fetch('/api/learning/courses/import-markdown', { method: 'POST', headers: { Authorization: `Bearer ${getAuthToken()}` }, body: form });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.error?.message || '学案导入失败');
      setLessonFile(null); await load(); setMessage(`学案已导入：正文 ${body.imported_chars} / 原文 ${body.source_chars} 字。`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const shareAll = async (course: Row) => {
    try {
      const students = await apiGet<{ items: Row[] }>('/api/teacher/students');
      const ids = (students.items || []).map(item => item.user_id).filter(Boolean);
      await apiPost(`/api/learning/courses/${encodeURIComponent(course.id)}/share`, { student_owner_ids: ids });
      setMessage(`已共享给 ${ids.length} 名绑定学生。`); await load();
    } catch (error) { setMessage(String(error)); }
  };
  const open = async (course: Row) => setDetail(await apiGet<Row>(`/api/learning/courses/${encodeURIComponent(course.id)}`));
  return <section className="teacher-section">
    <div className="teacher-panel"><h2>上传教材生成学案课程</h2><p className="teacher-muted">复用学生端同一套教材上传、解析、目录和章节总结流程。解析成功后默认共享给当前教师绑定的全部学生。</p><div className="teacher-form"><input value={title} onChange={event => setTitle(event.target.value)} placeholder="教材名称" /><input value={subject} onChange={event => setSubject(event.target.value)} placeholder="学科" /><input value={grade} onChange={event => setGrade(event.target.value)} placeholder="年级" /><input type="file" accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv" onChange={event => setFile(event.target.files?.[0] || null)} /><button disabled={busy} onClick={() => void uploadBook()}>上传并解析</button><input type="file" accept=".md,.markdown" onChange={event => setLessonFile(event.target.files?.[0] || null)} /><button disabled={busy} onClick={() => void importLesson()}>导入已生成学案</button></div></div>
    <div className="teacher-split"><Panel title="我的教材">{items.length === 0 ? <p className="teacher-muted">还没有教材。上传一份真实教材后才会出现。</p> : items.map(course => <div className="teacher-answer-row" key={course.id}><button onClick={() => void open(course)}><div><strong>{course.title}</strong><span>{course.subject || '未设置学科'} · {course.grade || '未设置年级'} · {course.status} · {course.progress || 0}%</span></div></button><button onClick={() => void shareAll(course)}>共享全部学生</button></div>)}</Panel>
      <Panel title="教材详情">{detail ? <><h3>{detail.course?.title}</h3><p>{detail.course?.subject} · {detail.course?.grade} · {detail.course?.status} · {detail.course?.progress}%</p><p className="teacher-muted">{detail.course?.error || '解析完成后显示目录与章节总结。'}</p>{detail.chapters?.map((chapter: Row) => <div className="teacher-step" key={chapter.id}><strong>{chapter.title}</strong><small>{chapter.summary}</small>{chapter.sections?.map((section: Row) => <div key={section.id}><p>{section.title}</p><small>{section.summary}</small></div>)}</div>)}</> : <p className="teacher-muted">选择教材查看目录、章节和总结。</p>}</Panel></div>
  </section>;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) { return <div className="teacher-panel"><h2>{title}</h2>{children}</div>; }
function MiniTable({ rows, columns }: { rows: Row[]; columns: Array<[string, string]> }) { return <table className="teacher-table"><thead><tr>{columns.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.id || index}>{columns.map(([key]) => <td key={key}>{String(row[key] ?? '—')}</td>)}</tr>)}</tbody></table>; }
