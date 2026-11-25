import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost, getAuthToken, type AuthUserDto } from '../api';
import { createLearningCourse } from '../learning-api';
import { BatchGrading } from './BatchGrading';
import { LearningAnalytics } from './LearningAnalytics';

type Tab = 'workbench' | 'textbooks' | 'data' | 'grading' | 'analytics';
type Row = Record<string, any>;
const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'workbench', label: '教师工作台' },
  { id: 'textbooks', label: '学案课程' },
  { id: 'data', label: '数据管理' },
  { id: 'grading', label: '判题中心' },
  { id: 'analytics', label: '学情分析' },
];

async function upload(path: string, files: File[] | null): Promise<Row> {
  const form = new FormData();
  (files || []).forEach(file => form.append('files', file));
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
      <button type="button" className="brand"><div className="brand-mark">学</div><div className="brand-name">智慧学伴</div></button>
      {TABS.map(item => <button key={item.id} className={tab === item.id ? 'nav-item active' : 'nav-item'} onClick={() => setTab(item.id)}><span className="nav-text">{item.label}</span></button>)}
      <div className="sidebar-foot"><div className="user-card"><div className="user-avatar">{user.display_name?.slice(0, 1) || '师'}</div><div className="user-info"><div className="user-name">{user.display_name}</div><div className="user-role">教师端</div></div></div><button className="account-action" onClick={onLogout}>退出当前账号</button></div>
    </aside>
    <div className="main teacher-main">
      <header className="teacher-head"><div><p>TEACHER CONSOLE · 原始数据 / 判题分层</p><h1>{TABS.find(item => item.id === tab)?.label}</h1></div><span className="teacher-live">数据库实时数据</span></header>
      {message && <div className="teacher-message">{message}</div>}
      {tab === 'workbench' && <Workbench summary={summary} batches={batches} jobs={jobs} onGo={setTab} />}
      {tab === 'textbooks' && <TextbookCenter setMessage={setMessage} />}
      {tab === 'data' && <DataManager meta={meta} batches={batches} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'grading' && <BatchGrading onChanged={refresh} setMessage={setMessage} />}
      {tab === 'analytics' && <LearningAnalytics setMessage={setMessage} />}
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
      <Panel title="最近导入任务">{batches.length === 0 ? <p className="teacher-muted">暂无导入任务。</p> : <MiniTable rows={batches.slice(0, 5)} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} />}</Panel>
      <Panel title="最近判题任务">{jobs.length === 0 ? <p className="teacher-muted">暂无判题任务。</p> : <MiniTable rows={jobs.slice(0, 5)} columns={[['status', '状态'], ['processed', '已处理'], ['total', '总数'], ['failed', '失败']]} />}</Panel>
    </div>
    <div className="teacher-actions"><button onClick={() => onGo('data')}>进入数据管理</button><button onClick={() => onGo('grading')}>进入判题中心</button></div>
  </section>;
}

function DataManager({ meta, batches, onChanged, setMessage }: { meta: Row; batches: Row[]; onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [mode, setMode] = useState<'import' | 'courses'>('import');
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
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
        <label className={`teacher-dropzone ${dragging ? 'dragging' : ''}`} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); setFiles(Array.from(event.dataTransfer.files).filter(file => file.name.toLowerCase().endsWith('.csv'))); }}>
          <input type="file" multiple accept=".csv" onChange={event => setFiles(Array.from(event.target.files || []))} />
          <strong>拖拽 CSV 到这里，或点击选择</strong>
          <span>支持多文件 · 学生 / 课程要求 / 测验节点 / 题目 / 作答记录</span>
        </label>
        {files.length > 0 && <div className="teacher-file-list">{files.map((file, index) => <div className="teacher-file-item" key={`${file.name}-${file.size}`}><div><strong>{file.name}</strong><small>{Math.max(1, Math.round(file.size / 1024))} KB</small></div><button onClick={() => setFiles(files.filter((_, i) => i !== index))}>移除</button></div>)}</div>}
        <div className="teacher-actions"><button disabled={busy || files.length === 0} onClick={() => void doPreview()}>预览与校验</button><button disabled={busy || files.length === 0} onClick={() => void doImport()}>确认导入</button></div>
        {preview && <div className="teacher-result"><h3>预览结果</h3><p>错误 {preview.error_count ?? 0} 条</p>{preview.files?.map((file: Row) => <div className="teacher-file" key={file.filename}><strong>{file.filename}</strong><span>{file.rows} 行</span><small>{file.fields?.join('、')}</small></div>)}{(preview.errors || []).slice(0, 8).map((error: Row, index: number) => <p className="teacher-error" key={index}>{error.message}</p>)}</div>}
        {result && <div className="teacher-result"><h3>导入完成</h3><pre>{JSON.stringify(result.counts, null, 2)}</pre>{(result.errors || []).length > 0 && <p>异常记录 {result.errors.length} 条，已保留在任务中。</p>}</div>}
      </Panel>
      <Panel title="历史导入任务"><MiniTable rows={batches} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} /></Panel>
    </div> : null}
    {audit && <div className="teacher-panel" style={{ marginTop: 14 }}><h2>数据核对</h2><table className="teacher-table"><thead><tr><th>数据类型</th><th>CSV 原始</th><th>数据库</th><th>差异</th></tr></thead><tbody>{[["students","学生"],["classes","班级"],["questions","题目"],["exams","测验节点"],["answers","原始作答"]].map(([key,label]) => { const raw = key === 'answers' ? audit.batch?.counts?.answers_source : audit.batch?.counts?.[key]; const db = audit.database?.[key]; return <tr key={key}><td>{label}</td><td>{raw ?? "—"}</td><td>{db ?? "—"}</td><td>{raw == null || db == null ? "—" : db - raw}</td></tr>; })}</tbody></table></div>}
    <div className="teacher-split" style={{ marginTop: 14 }}>
      <Panel title="班级与学生">{meta.classes?.map((item: Row) => <button className={classId === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void openClass(item.id)}><span>{item.code} 班</span><small>{item.student_count} 人</small></button>)}{students.map((item: Row) => <button className="teacher-row" key={item.id} onClick={() => void openStudent(item.id)}><span>{item.code}</span><small>{item.answers} 条作答</small></button>)}</Panel>
      <Panel title={studentDetail ? `${studentDetail.student.code} 原始作答` : '学生原始作答'}>{studentDetail ? studentDetail.answers?.map((answer: Row) => <div className="teacher-step" key={answer.id}><strong>{answer.question_id} · {answer.event_id} r{answer.revision}</strong><p>{answer.original_answer || '未作答'}</p><small>{answer.score_status} · 原始得分 {answer.original_score}/{answer.max_score}</small></div>) : <p className="teacher-muted">选择班级和学生查看原始作答。</p>}</Panel>
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
