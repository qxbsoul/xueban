import { useEffect, useState } from 'react';
import { apiGet, apiPost, getAuthToken, type AuthUserDto } from '../api';

type Tab = 'workbench' | 'import' | 'students' | 'grading';
type AnyRecord = Record<string, any>;

const TABS: Array<{ id: Tab; label: string; hint: string }> = [
  { id: 'workbench', label: '教师工作台', hint: '总览' },
  { id: 'import', label: '数据导入', hint: 'CSV' },
  { id: 'students', label: '班级与学生', hint: '原始记录' },
  { id: 'grading', label: '判题中心', hint: '逐步骤' },
];

async function uploadCsv(files: FileList | null): Promise<AnyRecord> {
  const form = new FormData();
  Array.from(files || []).forEach(file => form.append('files', file));
  const response = await fetch('/api/teacher/imports', {
    method: 'POST', headers: { Authorization: `Bearer ${getAuthToken()}` }, body: form,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail?.error?.message || body?.detail || '导入失败');
  return body;
}

export default function TeacherApp({ user, onLogout }: { user: AuthUserDto; onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>('workbench');
  const [summary, setSummary] = useState<AnyRecord | null>(null);
  const [classes, setClasses] = useState<AnyRecord[]>([]);
  const [selectedClass, setSelectedClass] = useState('');
  const [students, setStudents] = useState<AnyRecord[]>([]);
  const [student, setStudent] = useState<AnyRecord | null>(null);
  const [answers, setAnswers] = useState<AnyRecord[]>([]);
  const [selectedAnswer, setSelectedAnswer] = useState<AnyRecord | null>(null);
  const [detail, setDetail] = useState<AnyRecord | null>(null);
  const [job, setJob] = useState<AnyRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [manualScore, setManualScore] = useState('');
  const [manualNote, setManualNote] = useState('');

  const loadBase = async () => {
    const [nextSummary, nextClasses] = await Promise.all([apiGet<AnyRecord>('/api/teacher/summary'), apiGet<{ items: AnyRecord[] }>('/api/teacher/classes')]);
    setSummary(nextSummary); setClasses(nextClasses.items);
  };
  useEffect(() => { void loadBase().catch(error => setMessage(String(error))); }, []);

  const loadStudents = async (classId: string) => {
    setSelectedClass(classId); setStudent(null);
    const data = await apiGet<{ items: AnyRecord[] }>(`/api/teacher/classes/${encodeURIComponent(classId)}/students`);
    setStudents(data.items);
  };
  const openStudent = async (studentId: string) => {
    const data = await apiGet<AnyRecord>(`/api/teacher/students/${encodeURIComponent(studentId)}`);
    setStudent(data.student); setAnswers(data.answers || []);
  };
  const openAnswer = async (answerId: string) => {
    const data = await apiGet<AnyRecord>(`/api/teacher/answers/${encodeURIComponent(answerId)}`);
    setSelectedAnswer(data.answer); setDetail(data);
  };
  const gradeOne = async (answerId: string) => {
    setBusy(true); setMessage('正在判题…');
    try {
      await apiPost(`/api/teacher/answers/${encodeURIComponent(answerId)}/grade`, { llm_key: localStorage.getItem('sf_deepseek_key') || '' });
      setMessage('判题完成，已保存新的判题版本。');
      await openAnswer(answerId); await loadBase();
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const startBatch = async () => {
    setBusy(true); setMessage('正在创建批量判题任务…');
    try {
      const result = await apiPost<AnyRecord>('/api/teacher/grade/batch', { class_id: selectedClass || undefined, limit: 20 });
      setJob(result.job); setMessage('批量判题任务已启动。');
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  useEffect(() => {
    if (!job?.id) return;
    const timer = window.setInterval(async () => {
      const data = await apiGet<{ job: AnyRecord }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
      setJob(data.job);
      if (data.job.status !== 'running') window.clearInterval(timer);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.id]);
  const saveManual = async () => {
    if (!detail?.grades?.[0]?.id) return;
    setBusy(true);
    try {
      await apiPost(`/api/teacher/grades/${encodeURIComponent(detail.grades[0].id)}/manual`, { score: Number(manualScore), feedback: '教师人工修正', note: manualNote });
      setMessage('人工修正已保存，AI 原判保留。'); await openAnswer(detail.answer.id);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };

  return (
    <div className="teacher-app">
      <aside className="teacher-side">
        <div className="teacher-brand"><span>学</span><div><strong>智慧学伴</strong><small>教师端</small></div></div>
        <nav>{TABS.map(item => <button key={item.id} className={tab === item.id ? 'on' : ''} onClick={() => setTab(item.id)}><span>{item.label}</span><small>{item.hint}</small></button>)}</nav>
        <div className="teacher-user"><div>{user.display_name?.slice(0, 1) || '师'}</div><span>{user.display_name || '教师'}</span><button onClick={onLogout}>退出</button></div>
      </aside>
      <main className="teacher-main">
        <header className="teacher-head"><div><p>TEACHER CONSOLE</p><h1>{TABS.find(item => item.id === tab)?.label}</h1></div><span className="teacher-live">真实数据库 · 原始作答不覆盖</span></header>
        {message && <div className="teacher-message">{message}</div>}
        {tab === 'workbench' && <Workbench summary={summary} onGo={setTab} />}
        {tab === 'import' && <ImportPanel onDone={async () => { await loadBase(); setMessage('导入完成'); }} />}
        {tab === 'students' && <Students classes={classes} selectedClass={selectedClass} students={students} student={student} answers={answers} onClass={loadStudents} onStudent={openStudent} onAnswer={openAnswer} />}
        {tab === 'grading' && <Grading classes={classes} selectedClass={selectedClass} onClass={(id) => { setSelectedClass(id); void loadAnswers(id).then(setAnswers); }} answers={answers} job={job} onBatch={startBatch} onGrade={gradeOne} onOpen={openAnswer} detail={detail} manualScore={manualScore} setManualScore={setManualScore} manualNote={manualNote} setManualNote={setManualNote} onManual={saveManual} busy={busy} />}
      </main>
    </div>
  );
}

function Workbench({ summary, onGo }: { summary: AnyRecord | null; onGo: (tab: Tab) => void }) {
  const cards = [
    ['学生', summary?.students ?? '—'], ['班级', summary?.classes ?? '—'], ['题目', summary?.questions ?? '—'],
    ['原始作答', summary?.answers ?? '—'], ['待判题', summary?.pending ?? '—'], ['判题记录', summary?.graded ?? '—'],
  ];
  return <section className="teacher-section">
    <div className="teacher-cards">{cards.map(([label, value]) => <div className="teacher-card" key={String(label)}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <div className="teacher-grid">
      <div className="teacher-panel"><h2>最近导入批次</h2>{(summary?.recent_batches || []).length === 0 ? <p className="teacher-muted">还没有导入批次。</p> : <table><thead><tr><th>批次</th><th>文件</th><th>状态</th><th>时间</th></tr></thead><tbody>{(summary?.recent_batches || []).map((batch: AnyRecord) => <tr key={batch.id}><td>{batch.id}</td><td>{batch.filename}</td><td>{batch.status}</td><td>{batch.created_at}</td></tr>)}</tbody></table>}</div>
      <div className="teacher-panel"><h2>下一步</h2><p>先导入赛题数据，或直接使用已导入的 40 名学生数据。</p><button onClick={() => onGo('import')}>查看导入</button><button onClick={() => onGo('grading')}>进入判题中心</button></div>
    </div>
  </section>;
}

function ImportPanel({ onDone }: { onDone: () => Promise<void> }) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [result, setResult] = useState<AnyRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const run = async () => {
    setBusy(true); setError('');
    try { const data = await uploadCsv(files); setResult(data); await onDone(); }
    catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  };
  return <section className="teacher-section"><div className="teacher-panel">
    <h2>批量导入赛题 CSV</h2>
    <p>支持同时选择学生、课程要求、测验节点、题目与材料、作答记录 CSV。原始字段会完整写入数据库。</p>
    <input type="file" multiple accept=".csv" onChange={event => setFiles(event.target.files)} />
    <div className="teacher-actions"><button disabled={busy} onClick={() => void run()}>{busy ? '导入中…' : '确认导入'}</button></div>
    {error && <p className="teacher-error">{error}</p>}
    {result && <div className="teacher-result"><h3>导入完成</h3><pre>{JSON.stringify(result.counts || result, null, 2)}</pre>{(result.errors || []).length > 0 && <p>异常记录：{result.errors.length} 条，已保留在批次中。</p>}</div>}
  </div></section>;
}

function Students({ classes, selectedClass, students, student, answers, onClass, onStudent, onAnswer }: { classes: AnyRecord[]; selectedClass: string; students: AnyRecord[]; student: AnyRecord | null; answers: AnyRecord[]; onClass: (id: string) => void; onStudent: (id: string) => void; onAnswer: (id: string) => void }) {
  return <section className="teacher-section teacher-split">
    <div className="teacher-panel"><h2>班级</h2>{classes.map(item => <button className={selectedClass === item.id ? 'teacher-row on' : 'teacher-row'} key={item.id} onClick={() => void onClass(item.id)}><span>{item.code} 班</span><small>{item.student_count} 人</small></button>)}
      {selectedClass && <><h2 style={{ marginTop: 24 }}>学生</h2>{students.map(item => <button className="teacher-row" key={item.id} onClick={() => void onStudent(item.id)}><span>{item.code}</span><small>{item.answers} 条 · 待判 {item.pending}</small></button>)}</>}
    </div>
    <div className="teacher-panel">{student ? <><h2>{student.code} 的原始作答</h2>{answers.map(answer => <button className="teacher-answer-row" key={answer.id} onClick={() => void onAnswer(answer.id)}><div><strong>{answer.question_id}</strong><span>{answer.event_id} · 版本 {answer.revision}</span></div><div><small>{answer.score_status}</small><span>{answer.original_score}/{answer.max_score}</span></div></button>)}</> : <p className="teacher-muted">选择班级和学生查看原始作答。</p>}</div>
  </section>;
}

async function loadAnswers(classId: string): Promise<AnyRecord[]> {
  const status = 'pending';
  const data = await apiGet<{ items: AnyRecord[] }>(`/api/teacher/answers?status=${status}${classId ? `&class_id=${encodeURIComponent(classId)}` : ''}&limit=100`);
  return data.items;
}

function Grading(props: { classes: AnyRecord[]; selectedClass: string; onClass: (id: string) => void; answers: AnyRecord[]; job: AnyRecord | null; onBatch: () => void; onGrade: (id: string) => void; onOpen: (id: string) => void; detail: AnyRecord | null; manualScore: string; setManualScore: (v: string) => void; manualNote: string; setManualNote: (v: string) => void; onManual: () => void; busy: boolean }) {
  return <section className="teacher-section teacher-split">
    <div className="teacher-panel"><h2>待判题</h2><select value={props.selectedClass} onChange={event => props.onClass(event.target.value)}><option value="">全部班级</option>{props.classes.map(item => <option value={item.id} key={item.id}>{item.code} 班</option>)}</select><div className="teacher-actions"><button disabled={props.busy} onClick={props.onBatch}>批量判题（最多20条）</button></div>{props.job && <div className="teacher-job"><strong>批量任务</strong><span>{props.job.status} · {props.job.processed}/{props.job.total} · 失败 {props.job.failed}</span><progress value={props.job.processed} max={Math.max(1, props.job.total)} /></div>}{props.answers.map(answer => <button className="teacher-answer-row" key={answer.id} onClick={() => void props.onOpen(answer.id)}><div><strong>{answer.question_id}</strong><span>{answer.student_code} · {answer.event_id} r{answer.revision}</span></div><small>待判</small></button>)}</div>
    <div className="teacher-panel">{props.detail ? <><h2>判题详情</h2><p><strong>{props.detail.question?.question_type}</strong> · {props.detail.question?.max_score} 分</p><div className="teacher-stem">{props.detail.question?.stem}</div><div className="teacher-answer-box"><strong>学生作答</strong><p>{props.detail.answer.original_answer || '未作答'}</p><pre>{props.detail.answer.solution_process || '无解答过程'}</pre></div><button disabled={props.busy} onClick={() => { if (props.detail) props.onGrade(props.detail.answer.id); }}>判题 / 重新判题</button>{props.detail?.grades?.[0] && <><div className="teacher-grade"><strong>{props.detail?.grades?.[0]?.score}/{props.detail?.grades?.[0]?.max_score}</strong><span>{props.detail?.grades?.[0]?.is_correct} · {props.detail?.grades?.[0]?.grading_model}</span><p>{props.detail?.grades?.[0]?.feedback}</p></div><h3>逐步骤</h3>{JSON.parse(props.detail?.grades?.[0]?.step_results_json || '[]').map((step: AnyRecord) => <div className={step.is_correct ? 'teacher-step ok' : 'teacher-step'} key={step.step_index}><strong>步骤 {step.step_index}</strong><p>{step.student_step}</p><small>{step.is_correct ? '正确' : '错误'} · {step.score}/{step.max_score} · {step.judgment_reason}</small></div>)}<h3>人工修正</h3><input value={props.manualScore} onChange={event => props.setManualScore(event.target.value)} placeholder="修正分数" /><input value={props.manualNote} onChange={event => props.setManualNote(event.target.value)} placeholder="修改说明" /><button onClick={props.onManual}>保存修正</button></>}</> : <p className="teacher-muted">选择一条待判题查看详情。</p>}</div>
  </section>;
}
