import io
p=r"D:\capital lion\xueban\xueban-web\src\components\TeacherApp.tsx"
t=io.open(p,encoding="utf-8").read()
start=t.find('function Grading(')
end=t.find('function AnswerDetail(',start)
assert start>0 and end>start
new=r'''function Grading({ meta, onChanged, setMessage }: { meta: Row; onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [batches, setBatches] = useState<Row[]>([]);
  const [batchId, setBatchId] = useState('');
  const [batch, setBatch] = useState<Row | null>(null);
  const [classes, setClasses] = useState<Row[]>([]);
  const [students, setStudents] = useState<Row[]>([]);
  const [questions, setQuestions] = useState<Row[]>([]);
  const [classId, setClassId] = useState('');
  const [studentId, setStudentId] = useState('');
  const [detail, setDetail] = useState<Row | null>(null);
  const [textbookId, setTextbookId] = useState('');
  const [chapterId, setChapterId] = useState('');
  const [chapterContext, setChapterContext] = useState('');
  const [textbooks, setTextbooks] = useState<Row[]>([]);
  const [chapters, setChapters] = useState<Row[]>([]);
  const [job, setJob] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);

  const loadBatches = async () => setBatches((await apiGet<{ items: Row[] }>('/api/teacher/batches')).items || []);
  useEffect(() => { void loadBatches().catch(error => setMessage(String(error))); }, []);
  useEffect(() => { apiGet<Row[]>('/api/learning/courses').then(data => setTextbooks((Array.isArray(data) ? data : []).filter(course => course.status === 'ready'))).catch(() => setTextbooks([])); }, []);
  useEffect(() => {
    if (!textbookId) { setChapters([]); setChapterId(''); setChapterContext(''); return; }
    apiGet<Row>(`/api/learning/courses/${encodeURIComponent(textbookId)}`).then(data => {
      const flat = (data.chapters || []).flatMap((chapter: Row) => (chapter.sections || []).map((section: Row) => ({ ...section, chapter_title: chapter.title })));
      setChapters(flat); setChapterId(''); setChapterContext('');
    }).catch(() => setChapters([]));
  }, [textbookId]);
  const loadBatch = async (id: string) => {
    setBatchId(id); setStudents([]); setQuestions([]); setClassId(''); setStudentId('');
    const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(id)}`);
    setBatch(data.batch); setClasses(data.classes || []); setJob(data.latest_job || null);
  };
  useEffect(() => {
    if (!job?.id) return;
    const timer = window.setInterval(async () => {
      const data = await apiGet<Row>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
      setJob(data.job);
      if (!['running','queued','pausing'].includes(data.job.status)) window.clearInterval(timer);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.id]);
  const start = async () => {
    if (!batchId) { setMessage('请先选择导入批次。'); return; }
    setBusy(true); setMessage('正在创建批次判题任务…');
    try {
      const data = await apiPost<{ job: Row }>('/api/teacher/grade/batch', { batch_id: batchId, class_id: classId, textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterContext });
      setJob(data.job); setMessage(`任务已创建：${data.job.total} 条待判题。`); await loadBatch(batchId);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const pause = async () => { if (!job?.id) return; const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/pause`, {}); setJob(data.job); };
  const resume = async () => { if (!job?.id) return; const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/resume`, {}); setJob(data.job); };
  const retry = async () => { if (!job?.id) return; const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/retry`, {}); setJob(data.job); };
  const openClass = async (id: string) => { setClassId(id); setStudentId(''); setQuestions([]); setStudents((await apiGet<{ items: Row[] }>(`/api/teacher/batches/${encodeURIComponent(batchId)}/classes/${encodeURIComponent(id)}/students`)).items || []); };
  const openStudent = async (id: string) => { setStudentId(id); setQuestions((await apiGet<{ items: Row[] }>(`/api/teacher/batches/${encodeURIComponent(batchId)}/classes/${encodeURIComponent(classId)}/students/${encodeURIComponent(id)}/questions`)).items || []); };
  const openQuestion = async (id: string) => setDetail(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(id)}`));
  const currentClass = classes.find(item => item.id === classId);
  const currentStudent = students.find(item => item.id === studentId);

  return <section className="teacher-section">
    <div className="teacher-panel">
      <h2>选择导入批次</h2>
      <p className="teacher-muted">批次是判题任务的数据来源。上传 CSV 后在这里选择并启动判题，不会自动调用模型。</p>
      <select value={batchId} onChange={event => void loadBatch(event.target.value)}><option value="">选择批次</option>{batches.map(item => <option key={item.id} value={item.id}>{item.filename} · {item.id}</option>)}</select>
    </div>
    {batch && <>
      <div className="teacher-panel">
        <h2>{batch.filename}</h2>
        <p className="teacher-muted">批次 {batch.id} · 学生 {batch.students || 0} · 题目 {batch.questions || 0} · 作答 {batch.total || 0} · 待判 {batch.pending || 0} · 已判 {batch.graded || 0} · 失败 {batch.failed || 0}</p>
        <div className="teacher-scope"><label>参考学案（可选）<select value={textbookId} onChange={event => setTextbookId(event.target.value)}><option value="">不使用学案</option>{textbooks.map(book => <option key={book.id} value={book.id}>{book.title}</option>)}</select></label>{textbookId && <label>章节<select value={chapterId} onChange={event => { setChapterId(event.target.value); const chapter = chapters.find(item => item.id === event.target.value); setChapterContext(chapter?.lesson?.content || chapter?.summary || ''); }}><option value="">不指定章节</option>{chapters.map(chapter => <option key={chapter.id} value={chapter.id}>{chapter.chapter_title} / {chapter.title}</option>)}</select></label>}</div>
        <div className="teacher-actions"><button disabled={busy || !batch.pending} onClick={() => void start()}>开始判题</button>{job && ['running','queued','pausing'].includes(job.status) && <button onClick={() => void pause()}>暂停</button>}{job && ['paused','partially_failed'].includes(job.status) && <button onClick={() => void resume()}>继续</button>}{job && Number(job.failed) > 0 && <button onClick={() => void retry()}>失败重试</button>}</div>
      </div>
      {job && <div className="teacher-job"><strong>任务 {job.id} · {statusLabel(job.status)}</strong><span>进度 {job.completed || 0}/{job.total || 0} · 失败 {job.failed || 0} · 待复核 {job.needs_review || 0}</span><progress value={job.completed || 0} max={Math.max(1, job.total || 1)} /><small>当前作答：{job.current_answer_id || '—'}</small></div>}
      <div className="teacher-split">
        <Panel title="班级进度"><table className="teacher-table"><thead><tr><th>班级</th><th>学生</th><th>作答</th><th>待判</th><th>已判</th><th>失败</th></tr></thead><tbody>{classes.map(item => <tr key={item.id} onClick={() => void openClass(item.id)}><td>{item.code}</td><td>{item.students}</td><td>{item.answers}</td><td>{item.pending}</td><td>{item.graded}</td><td>{item.failed}</td></tr>)}</tbody></table></Panel>
        <Panel title={currentClass ? `${currentClass.code} 学生` : "选择班级"}>{students.length ? <table className="teacher-table"><thead><tr><th>学生</th><th>作答</th><th>待判</th><th>已判</th><th>失败</th></tr></thead><tbody>{students.map(item => <tr key={item.id} onClick={() => void openStudent(item.id)}><td>{item.code}</td><td>{item.total}</td><td>{item.pending}</td><td>{item.graded}</td><td>{item.failed}</td></tr>)}</tbody></table> : <p className="teacher-muted">选择班级查看学生。</p>}</Panel>
      </div>
      <div className="teacher-split">
        <Panel title={currentStudent ? `${currentStudent.code} 题目` : "选择学生"}>{questions.length ? <table className="teacher-table"><thead><tr><th>题目</th><th>题型</th><th>满分</th><th>得分</th><th>状态</th></tr></thead><tbody>{questions.map(item => <tr key={item.id} onClick={() => void openQuestion(item.id)}><td>{item.question_id}</td><td>{item.question_type}</td><td>{item.question_max_score}</td><td>{item.score == null ? '未判' : item.score}</td><td>{statusLabel(item.grade_status)}</td></tr>)}</tbody></table> : <p className="teacher-muted">选择学生查看题目。</p>}</Panel>
        <Panel title="题目判题详情">{detail ? <AnswerDetail detail={detail} onGrade={async () => { await apiPost(`/api/teacher/answers/${encodeURIComponent(detail.answer.id)}/grade`, { textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterContext }); setDetail(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(detail.answer.id)}`)); await onChanged(); }} busy={busy} /> : <p className="teacher-muted">选择一道题查看判题详情。</p>}</Panel>
      </div>
    </>}
  </section>;
}

'''
t=t[:start]+new+t[end:]
io.open(p,"w",encoding="utf-8",newline="").write(t)
print("batch grading UI")