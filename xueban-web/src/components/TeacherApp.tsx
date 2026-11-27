import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost, getAuthToken, type AuthUserDto } from '../api';
import { createComprehensiveCourse } from '../learning-api';
import { BatchGrading } from './BatchGrading';
import { TeacherAnalytics } from './TeacherAnalytics';

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
  const [gradeConfig, setGradeConfig] = useState<Row | null>(null);
  const [message, setMessage] = useState('');

  const refresh = async () => {
    const [s, m, b, j, g] = await Promise.all([
      apiGet<Row>('/api/teacher/summary'),
      apiGet<Row>('/api/teacher/meta'),
      apiGet<{ items: Row[] }>('/api/teacher/imports'),
      apiGet<{ items: Row[] }>('/api/teacher/grade/jobs'),
      apiGet<Row>('/api/teacher/grade/config').catch(() => null),
    ]);
    setSummary(s); setMeta(m); setBatches(b.items || []); setJobs(j.items || []); setGradeConfig(g);
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
      {gradeConfig && !gradeConfig.configured && <div className="teacher-message teacher-warning">判题模型未配置：{gradeConfig.message}。导入、预处理、学情查看仍可使用，但不能创建判题任务。</div>}
      {tab === 'workbench' && <Workbench summary={summary} batches={batches} jobs={jobs} onGo={setTab} />}
      {tab === 'textbooks' && <TextbookCenter setMessage={setMessage} />}
      {tab === 'data' && <DataManager meta={meta} batches={batches} onChanged={refresh} setMessage={setMessage} />}
      {tab === 'grading' && <BatchGrading onChanged={refresh} setMessage={setMessage} />}
      {tab === 'analytics' && <TeacherAnalytics setMessage={setMessage} />}
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
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState<Row | null>(null);
  const [result, setResult] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [audit, setAudit] = useState<Row | null>(null);
  const [classId, setClassId] = useState('');
  const [students, setStudents] = useState<Row[]>([]);
  const [studentDetail, setStudentDetail] = useState<Row | null>(null);

  useEffect(() => { apiGet<Row>('/api/teacher/audit').then(setAudit).catch(() => setAudit(null)); }, []);
  const doPreview = async () => { setBusy(true); setMessage(''); try { setPreview(await upload('/api/teacher/imports/preview', files)); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const doImport = async () => { setBusy(true); setMessage(''); try { setResult(await upload('/api/teacher/imports', files)); await onChanged(); setMessage('导入完成，原始数据已写入数据库。'); } catch (error) { setMessage(String(error)); } finally { setBusy(false); } };
  const openClass = async (id: string) => { setClassId(id); setStudentDetail(null); setStudents((await apiGet<{ items: Row[] }>(`/api/teacher/classes/${encodeURIComponent(id)}/students`)).items || []); };
  const openStudent = async (id: string) => setStudentDetail(await apiGet<Row>(`/api/teacher/students/${encodeURIComponent(id)}`));

  return <section className="teacher-section">
    <div className="teacher-tabs"><button className="on">数据导入</button></div>
    <div className="teacher-grid">
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
        {result && <div className="teacher-result"><h3>导入完成</h3><pre>{JSON.stringify(result.counts, null, 2)}</pre>{(result.errors || []).length > 0 && <p>异常记录 {result.errors.length} 条，已保留在任务中。</p>}
          {(result.student_credentials || []).length > 0 && <div><h3>学生初始账号（仅显示这一次）</h3><p>请立即分发给学生；首次登录必须修改密码，服务端只保存哈希。</p>
            <table className="teacher-table"><thead><tr><th>学号</th><th>登录邮箱</th><th>初始密码</th></tr></thead><tbody>
              {(result.student_credentials || []).map((item: Row) => <tr key={item.code}><td>{item.code}</td><td>{item.email}</td><td><code>{item.initial_password}</code></td></tr>)}
            </tbody></table></div>}
        </div>}
      </Panel>
      <Panel title="历史导入任务"><MiniTable rows={batches} columns={[['filename', '文件'], ['status', '状态'], ['created_at', '时间']]} /></Panel>
    </div>
    {audit && <div className="teacher-panel" style={{ marginTop: 14 }}><h2>数据核对</h2><table className="teacher-table"><thead><tr><th>数据类型</th><th>CSV 原始</th><th>数据库</th><th>差异</th></tr></thead><tbody>{[["students","学生"],["classes","班级"],["questions","真题"],["materials","教材/材料"],["exams","测验节点"],["answers","原始作答"]].map(([key,label]) => { const raw = key === 'answers' ? audit.batch?.counts?.answers_source : (key === 'materials' ? null : audit.batch?.counts?.[key]); const db = audit.database?.[key]; return <tr key={key}><td>{label}</td><td>{raw ?? "—"}</td><td>{db ?? "—"}</td><td>{raw == null || db == null ? "—" : db - raw}</td></tr>; })}</tbody></table><p className="teacher-muted">题表同时保存真题与教材/学习卡材料行，统计时分开计算。</p></div>}
    <div className="teacher-split" style={{ marginTop: 14 }}>
      <Panel title="班级与学生">{meta.classes?.map((item: Row) => <button className={classId === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void openClass(item.id)}><span>{item.code} 班</span><small>{item.student_count} 人</small></button>)}{students.map((item: Row) => <button className="teacher-row" key={item.id} onClick={() => void openStudent(item.id)}><span>{item.code}</span><small>{item.answers} 条作答</small></button>)}</Panel>
      <Panel title={studentDetail ? `${studentDetail.student.code} 原始作答` : '学生原始作答'}>{studentDetail ? studentDetail.answers?.map((answer: Row) => <div className="teacher-step" key={answer.id}><strong>{answer.question_id} · {answer.event_id} r{answer.revision}</strong><p>{answer.original_answer || '未作答'}</p><small>{answer.score_status} · 原始得分 {answer.original_score}/{answer.max_score}</small></div>) : <p className="teacher-muted">选择班级和学生查看原始作答。</p>}</Panel>
    </div>
    <StudentAccountsPanel setMessage={setMessage} />
  </section>;
}

function StudentAccountsPanel({ setMessage }: { setMessage: (value: string) => void }) {
  const [students, setStudents] = useState<Row[]>([]);
  const [unbound, setUnbound] = useState<Row[]>([]);
  const [bindTarget, setBindTarget] = useState('');
  const [issued, setIssued] = useState<Row | null>(null);
  const load = async () => {
    const [s, u] = await Promise.all([
      apiGet<{ items: Row[] }>('/api/teacher/students'),
      apiGet<{ items: Row[] }>('/api/teacher/student-accounts/unbound').catch(() => ({ items: [] as Row[] })),
    ]);
    setStudents(s.items || []); setUnbound(u.items || []);
  };
  useEffect(() => { void load().catch(error => setMessage(String(error))); }, []);
  const reset = async (student: Row) => {
    try {
      const result = await apiPost<Row>(`/api/teacher/students/${encodeURIComponent(student.id)}/reset-password`, {});
      setIssued(result); setMessage(`已为 ${student.code} 重置密码（仅显示一次）`);
    } catch (error) { setMessage(String(error)); }
  };
  const bind = async (student: Row) => {
    if (!bindTarget) { setMessage('请选择一个未绑定的学生账号'); return; }
    try {
      await apiPost(`/api/teacher/students/${encodeURIComponent(student.id)}/bind-account`, { user_id: bindTarget });
      setMessage(`已把账号绑定到 ${student.code}`); setBindTarget(''); await load();
    } catch (error) { setMessage(String(error)); }
  };
  return <div className="teacher-panel" style={{ marginTop: 14 }}>
    <h2>学生账号管理</h2>
    <p className="teacher-muted">导入时会为学生创建初始账号并要求首次登录改密；这里可以重置密码或绑定自注册账号。</p>
    {issued && <p>新初始密码：<code>{issued.initial_password}</code>（{issued.email}，仅显示一次）</p>}
    <table className="teacher-table"><thead><tr><th>学号</th><th>账号</th><th>操作</th></tr></thead><tbody>
      {students.map(student => <tr key={student.id}>
        <td>{student.code}</td>
        <td>{student.user_id ? '已绑定' : '未绑定'}</td>
        <td>
          <button disabled={!student.user_id} onClick={() => void reset(student)}>重置密码</button>
          <select value={bindTarget} onChange={event => setBindTarget(event.target.value)}><option value="">选择未绑定账号</option>{unbound.map(item => <option key={item.id} value={item.id}>{item.email || item.display_name}</option>)}</select>
          <button onClick={() => void bind(student)}>绑定账号</button>
        </td>
      </tr>)}
    </tbody></table>
  </div>;
}

function TeacherFileZone({ label, hint, accept, files, onAdd, onRemove, disabled }: { label: string; hint: string; accept: string; files: File[]; onAdd: (files: FileList | File[] | null) => void; onRemove: (index: number) => void; disabled?: boolean }) {
  const [dragging, setDragging] = useState(false);
  const inputId = `teacher-upload-${label.replace(/\s+/g, '-')}`;
  return <div className={`teacher-file-zone ${dragging ? 'dragging' : ''}`}>
    <div className="teacher-file-zone-head"><strong>{label}</strong><span>{hint}</span></div>
    <label htmlFor={inputId} className="teacher-dropzone" onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); onAdd(event.dataTransfer.files); }}>
      <b>拖拽文件到这里，或点击选择</b><small>{accept.split(',').join(' · ')}</small>
    </label>
    <input id={inputId} type="file" multiple accept={accept} disabled={disabled} onChange={event => { const picked = Array.from(event.target.files || []); event.currentTarget.value = ''; onAdd(picked); }} />
    {files.length > 0 && <ul className="teacher-file-list">{files.map((file, index) => <li key={`${file.name}-${index}`}><span>{file.name}</span><small>{(file.size / 1024).toFixed(0)} KB</small><button onClick={() => onRemove(index)} disabled={disabled}>移除</button></li>)}</ul>}
  </div>;
}

function TextbookCenter({ setMessage }: { setMessage: (v: string) => void }) {
  const [items, setItems] = useState<Row[]>([]);
  const [title, setTitle] = useState('');
  const [subject, setSubject] = useState('数学');
  const [grade, setGrade] = useState('七年级');
  const [bookFiles, setBookFiles] = useState<File[]>([]);
  const [planFiles, setPlanFiles] = useState<File[]>([]);
  const [lessonFile, setLessonFile] = useState<File | null>(null);
  const [busyBook, setBusyBook] = useState(false);
  const [busyLesson, setBusyLesson] = useState(false);
  const [bookStatus, setBookStatus] = useState('');
  const [bookError, setBookError] = useState('');
  const [lessonStatus, setLessonStatus] = useState('');
  const [lessonError, setLessonError] = useState('');
  const [detail, setDetail] = useState<Row | null>(null);

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

  const addFiles = (setter: React.Dispatch<React.SetStateAction<File[]>>) => (files: FileList | File[] | null) => {
    if (!files) return;
    setter(current => {
      const existing = new Set(current.map(file => `${file.name}:${file.size}`));
      return [...current, ...Array.from(files).filter(file => !existing.has(`${file.name}:${file.size}`))];
    });
  };

  const uploadBook = async () => {
    if (!title.trim()) { setBookError('请填写教材名称。'); return; }
    if (!bookFiles.length) { setBookError('请选择至少一份教材文件。'); return; }
    if (!planFiles.length) { setBookError('请选择至少一份教案/课程标准文件。'); return; }
    setBusyBook(true); setBookError(''); setBookStatus('正在上传教材与教案，创建解析任务…');
    try {
      const result = await createComprehensiveCourse({ title: title.trim(), subject, grade, curriculumFiles: planFiles, textbookFiles: bookFiles });
      setBookStatus(`课程已创建：${result.course.title} · 解析进度 ${result.course.progress || 0}%`);
      setMessage(`教材已创建，解析任务 ${result.job.id} 已启动。`);
      setBookFiles([]); setPlanFiles([]); setTitle(''); await load();
    } catch (error) { setBookError(String(error)); setBookStatus(''); } finally { setBusyBook(false); }
  };

  const importLesson = async (file: File | null) => {
    if (!file) return;
    setLessonFile(file); setBusyLesson(true); setLessonError(''); setLessonStatus(`正在导入 ${file.name}…`);
    try {
      const form = new FormData(); form.append('file', file);
      const response = await fetch('/api/learning/courses/import-markdown', { method: 'POST', headers: { Authorization: `Bearer ${getAuthToken()}` }, body: form });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.error?.message || '学案导入失败');
      setLessonStatus(`导入成功：正文 ${body.imported_chars} / 原文 ${body.source_chars} 字。`);
      setMessage(`学案已导入：正文 ${body.imported_chars} / 原文 ${body.source_chars} 字。`);
      setLessonFile(null); await load();
    } catch (error) { setLessonError(String(error)); setLessonStatus(''); } finally { setBusyLesson(false); }
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
    <div className="teacher-upload-grid">
      <div className="teacher-panel teacher-upload-card">
        <h2>上传教材生成课程</h2>
        <p className="teacher-muted">教材与教案分别上传；解析成功后默认共享给当前教师绑定的全部学生。</p>
        <div className="teacher-field-grid">
          <label>教材名称<input value={title} onChange={event => setTitle(event.target.value)} placeholder="例如：七年级数学上册" /></label>
          <label>学科<input value={subject} onChange={event => setSubject(event.target.value)} placeholder="数学" /></label>
          <label>年级<input value={grade} onChange={event => setGrade(event.target.value)} placeholder="七年级" /></label>
        </div>
        <TeacherFileZone label="教材文件" hint="PDF / DOCX / PPTX / TXT 等" accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv" files={bookFiles} onAdd={addFiles(setBookFiles)} onRemove={index => setBookFiles(files => files.filter((_, i) => i !== index))} disabled={busyBook} />
        <TeacherFileZone label="教案 / 课程标准" hint="与教材一起生成课程" accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv" files={planFiles} onAdd={addFiles(setPlanFiles)} onRemove={index => setPlanFiles(files => files.filter((_, i) => i !== index))} disabled={busyBook} />
        {bookError && <p className="teacher-error">{bookError}</p>}
        {bookStatus && <p className="teacher-progress">{bookStatus}</p>}
        <button className="teacher-primary" disabled={busyBook} onClick={() => void uploadBook()}>{busyBook ? '正在创建…' : '创建课程'}</button>
      </div>

      <div className="teacher-panel teacher-upload-card">
        <h2>导入已有学案</h2>
        <p className="teacher-muted">支持学生端导出的 Markdown 学案；选择文件后自动导入，不需要再点第二个按钮。</p>
        <label className={`teacher-dropzone teacher-dropzone-small ${busyLesson ? 'disabled' : ''}`} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); void importLesson(event.dataTransfer.files?.[0] || null); }}>
          <b>拖拽 Markdown 学案到这里，或点击选择</b><small>.md · .markdown</small>
          <input type="file" accept=".md,.markdown" disabled={busyLesson} onChange={event => { void importLesson(event.target.files?.[0] || null); event.currentTarget.value = ''; }} />
        </label>
        {lessonFile && <p className="teacher-muted">已选择：{lessonFile.name}</p>}
        {lessonError && <p className="teacher-error">{lessonError}</p>}
        {lessonStatus && <p className="teacher-progress">{lessonStatus}</p>}
      </div>
    </div>

    <div className="teacher-split"><Panel title="我的教材">{items.length === 0 ? <p className="teacher-muted">还没有教材。上传一份真实教材后才会出现。</p> : items.map(course => <div className="teacher-answer-row" key={course.id}><button onClick={() => void open(course)}><div><strong>{course.title}</strong><span>{course.subject || '未设置学科'} · {course.grade || '未设置年级'} · {course.status} · {course.progress || 0}%</span></div></button><button onClick={() => void shareAll(course)}>共享全部学生</button></div>)}</Panel>
      <Panel title="教材详情">{detail ? <><h3>{detail.course?.title}</h3><p>{detail.course?.subject} · {detail.course?.grade} · {detail.course?.status} · {detail.course?.progress}%</p><p className="teacher-muted">{detail.course?.error || '解析完成后显示目录与章节总结。'}</p>{detail.chapters?.map((chapter: Row) => <div className="teacher-step" key={chapter.id}><strong>{chapter.title}</strong><small>{chapter.summary}</small>{chapter.sections?.map((section: Row) => <div key={section.id}><p>{section.title}</p><small>{section.summary}</small></div>)}</div>)}</> : <p className="teacher-muted">选择教材查看目录、章节和总结。</p>}</Panel></div>
  </section>;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) { return <div className="teacher-panel"><h2>{title}</h2>{children}</div>; }
function MiniTable({ rows, columns }: { rows: Row[]; columns: Array<[string, string]> }) { return <table className="teacher-table"><thead><tr>{columns.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.id || index}>{columns.map(([key]) => <td key={key}>{String(row[key] ?? '—')}</td>)}</tr>)}</tbody></table>; }
