import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api';
import './batch-grading.css';

type Row = Record<string, any>;
type Level = 'list' | 'batch' | 'class' | 'student' | 'group' | 'question';

export function BatchGrading({ onChanged, setMessage }: { onChanged: () => Promise<void>; setMessage: (v: string) => void }) {
  const [level, setLevel] = useState<Level>('list');
  const [batches, setBatches] = useState<Row[]>([]);
  const [batch, setBatch] = useState<Row | null>(null);
  const [classes, setClasses] = useState<Row[]>([]);
  const [jobs, setJobs] = useState<Row[]>([]);
  const [job, setJob] = useState<Row | null>(null);
  const [cls, setCls] = useState<Row | null>(null);
  const [students, setStudents] = useState<Row[]>([]);
  const [student, setStudent] = useState<Row | null>(null);
  const [groups, setGroups] = useState<Row[]>([]);
  const [group, setGroup] = useState<Row | null>(null);
  const [questions, setQuestions] = useState<Row[]>([]);
  const [detail, setDetail] = useState<Row | null>(null);
  const [textbooks, setTextbooks] = useState<Row[]>([]);
  const [textbookId, setTextbookId] = useState('');
  const [chapters, setChapters] = useState<Row[]>([]);
  const [chapterId, setChapterId] = useState('');
  const [chapterText, setChapterText] = useState('');
  const [busy, setBusy] = useState(false);
  const [studentSort, setStudentSort] = useState('code');

  useEffect(() => {
    apiGet<{ items: Row[] }>('/api/teacher/batches').then(data => setBatches(data.items || [])).catch(error => setMessage(String(error)));
    apiGet<Row[]>('/api/learning/courses').then(data => setTextbooks((Array.isArray(data) ? data : []).filter(item => item.status === 'ready'))).catch(() => setTextbooks([]));
  }, []);
  useEffect(() => {
    if (!textbookId) { setChapters([]); setChapterId(''); setChapterText(''); return; }
    apiGet<Row>(`/api/learning/courses/${encodeURIComponent(textbookId)}`).then(data => {
      setChapters((data.chapters || []).flatMap((chapter: Row) => (chapter.sections || []).map((section: Row) => ({ ...section, chapter_title: chapter.title }))));
      setChapterId(''); setChapterText('');
    }).catch(() => setChapters([]));
  }, [textbookId]);
  useEffect(() => {
    if (!job?.id) return;
    const timer = window.setInterval(async () => {
      const data = await apiGet<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
      setJob(data.job);
      if (!['running', 'queued', 'pausing'].includes(data.job.status)) window.clearInterval(timer);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.id]);

  const openBatch = async (id: string) => {
    setBusy(true);
    try {
      const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(id)}/overview`);
      setBatch(data.batch); setClasses(data.classes || []); setJobs(data.jobs || []); setJob(data.latest_job || null);
      setLevel('batch'); setCls(null); setStudent(null); setGroup(null); setDetail(null);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const openClass = async (item: Row) => {
    setBusy(true);
    try {
      const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batch?.id)}/classes/${encodeURIComponent(item.id)}/overview`);
      setCls(data.class); setStudents(data.students || []); setLevel('class');
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const openStudent = async (item: Row) => {
    setStudent(item);
    const data = await apiGet<{ items: Row[] }>(`/api/teacher/batches/${encodeURIComponent(batch?.id)}/classes/${encodeURIComponent(cls?.id)}/students/${encodeURIComponent(item.id)}/groups`);
    setGroups(data.items || []); setLevel('student');
  };
  const openGroup = async (item: Row) => {
    setGroup(item);
    const data = await apiGet<{ items: Row[] }>(`/api/teacher/batches/${encodeURIComponent(batch?.id)}/classes/${encodeURIComponent(cls?.id)}/students/${encodeURIComponent(student?.id)}/groups/${encodeURIComponent(item.id)}/questions`);
    setQuestions(data.items || []); setLevel('group');
  };
  const openQuestion = async (item: Row) => {
    const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batch?.id)}/answers/${encodeURIComponent(item.id)}`);
    setDetail(data); setLevel('question');
  };
  const start = async () => {
    if (!batch) return;
    setBusy(true); setMessage('正在创建批次判题任务…');
    try {
      const data = await apiPost<{ job: Row }>('/api/teacher/grade/batch', { batch_id: batch.id, class_id: cls?.id || '', textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterText });
      setJob(data.job); setMessage(`任务已创建：${data.job.total} 条待判题。`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const action = async (name: 'pause' | 'resume' | 'retry') => {
    if (!job?.id) return;
    try {
      const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/${name}`, {});
      setJob(data.job); await onChanged();
    } catch (error) { setMessage(String(error)); }
  };
  const correct = async (score: number, note: string) => {
    if (!detail?.grade?.id) return;
    try {
      await apiPost(`/api/teacher/grades/${encodeURIComponent(detail.grade.id)}/manual`, { score, note });
      setMessage('人工修正已保存，历史判题版本保留。');
      await openQuestion({ id: detail.answer.id });
      await onChanged();
    } catch (error) { setMessage(String(error)); }
  };

  const sortedStudents = [...students].sort((a, b) => {
    if (studentSort === 'score_desc' || studentSort === 'score_asc') {
      if (a.score_rate == null && b.score_rate == null) return String(a.code).localeCompare(String(b.code));
      if (a.score_rate == null) return 1;
      if (b.score_rate == null) return -1;
      return studentSort === 'score_desc' ? b.score_rate - a.score_rate : a.score_rate - b.score_rate;
    }
    return String(a.code).localeCompare(String(b.code));
  });

  const crumbs = [['判题中心', 'list'], [batch?.filename, 'batch'], [cls?.code, 'class'], [student?.code, 'student'], [group?.name, 'group'], [detail?.answer?.question_id, 'question']].filter(([label]) => label);

  const go = (target: Level) => {
    setLevel(target);
    if (target === 'list') { setBatch(null); setJob(null); }
    if (target === 'batch') { setCls(null); setStudent(null); setGroup(null); setDetail(null); }
    if (target === 'class') { setStudent(null); setGroup(null); setDetail(null); }
    if (target === 'student') { setGroup(null); setDetail(null); }
    if (target === 'group') setDetail(null);
  };

  return <section className="batch-section">
    <div className="batch-crumbs">{crumbs.map(([label, target], index) => <button key={`${label}-${index}`} onClick={() => go(target as Level)}>{index > 0 ? ' / ' : ''}{String(label)}</button>)}</div>

    {level === 'list' && <div className="batch-list">
      <div className="batch-title"><h1>导入批次</h1><p>先选择一个导入批次。未选择批次时不会查询或展示任何学生作答。</p></div>
      {batches.length === 0 && <div className="batch-empty">还没有导入批次。请先到数据管理上传五个 CSV。</div>}
      {batches.map(item => <div className="batch-row" key={item.id}>
        <div><strong>{item.filename}</strong><small>{item.id} · {item.created_at}</small></div>
        <div className="batch-metrics"><span>班级 {item.classes || 0}</span><span>学生 {item.students || 0}</span><span>题目 {item.questions || 0}</span><span>作答 {item.total || 0}</span><span>待判 {item.pending || 0}</span><span>已完成 {item.graded || 0}</span><span>待复核 {item.needs_review || 0}</span></div>
        <button disabled={busy} onClick={() => void openBatch(item.id)}>进入任务</button>
      </div>)}
    </div>}

    {level === 'batch' && batch && <>
      <div className="batch-head"><div><h1>{batch.filename}</h1><p>{batch.id} · 导入 {batch.created_at}</p></div><button onClick={() => go('list')}>返回批次列表</button></div>
      <div className="batch-progress-card">
        <div className="batch-progress-top"><strong>{job ? statusLabel(job.status) : '导入完成，待判题'}</strong><span>{percent(job)}%</span></div>
        <div className="batch-progress"><i style={{ width: `${percent(job)}%` }} /></div>
        <div className="batch-metrics"><span>总数 {job?.total ?? batch.total}</span><span>成功 {job?.completed ?? batch.graded}</span><span>失败 {job?.failed ?? batch.failed}</span><span>待处理 {job ? Math.max(0, Number(job.total || 0) - Number(job.completed || 0) - Number(job.failed || 0)) : batch.pending}</span><span>待复核 {job?.needs_review ?? batch.review}</span></div>
        <div className="batch-actions"><button disabled={busy || !batch.pending} onClick={() => void start()}>开始判题</button>{job && ['running', 'queued', 'pausing'].includes(job.status) && <button onClick={() => void action('pause')}>暂停</button>}{job && ['paused', 'partially_failed'].includes(job.status) && <button onClick={() => void action('resume')}>继续</button>}{job && Number(job.failed) > 0 && <button onClick={() => void action('retry')}>失败重试</button>}</div>
        {job && <p className="batch-muted">当前作答：{job.current_answer_id || '—'} · 最近检查点：{job.last_checkpoint_at || '—'}</p>}
        <div className="batch-context"><label>参考学案（可选）<select value={textbookId} onChange={event => setTextbookId(event.target.value)}><option value="">不使用学案</option>{textbooks.map(book => <option key={book.id} value={book.id}>{book.title}</option>)}</select></label>{textbookId && <label>章节<select value={chapterId} onChange={event => { setChapterId(event.target.value); const item = chapters.find(c => c.id === event.target.value); setChapterText(item?.lesson?.content || item?.summary || ''); }}><option value="">不指定章节</option>{chapters.map(item => <option key={item.id} value={item.id}>{item.chapter_title} / {item.title}</option>)}</select></label>}</div>
      </div>
      <div className="batch-title"><h2>班级概览</h2><p>成绩只统计当前批次；平均分仅按已完成学生计算。</p></div>
      <div className="class-grid">{classes.map(item => <button className="class-card" key={item.id} onClick={() => void openClass(item)}>
        <div className="class-card-head"><strong>{item.code}</strong><span>{item.completed_students}/{item.students} 完成</span></div>
        <div className="class-score"><b>{item.average_score ?? '—'}</b><small>平均分</small></div>
        <div className="class-card-stats"><span>平均得分率 {item.average_rate == null ? '—' : `${item.average_rate}%`}</span><span>最高 {item.highest ?? '—'} / 最低 {item.lowest ?? '—'}</span><span>作答 {item.answers} 条</span></div>
        <div className="class-progress"><i style={{ width: `${item.answers ? Math.round(item.completed_students / Math.max(1, item.students) * 100) : 0}%` }} /></div>
      </button>)}</div>
    </>}

    {level === 'class' && cls && <>
      <div className="batch-head"><div><h1>{cls.code}</h1><p>{batch?.filename} · 当前批次班级</p></div><button onClick={() => go('batch')}>返回批次</button></div>
      <div className="batch-title"><h2>学生概览</h2><p>个人得分来自当前批次的有效判题；未完成学生不按 0 分计算。</p></div>
      <div className="batch-list-toolbar"><span>共 {students.length} 名学生</span><select value={studentSort} onChange={event => setStudentSort(event.target.value)}><option value="code">按学生编号</option><option value="score_desc">总分从高到低</option><option value="score_asc">总分从低到高</option><option value="rate_desc">得分率从高到低</option></select></div>
      <div className="student-list">{sortedStudents.map(item => <button className="student-row" key={item.id} onClick={() => void openStudent(item)}>
        <div className="student-id"><strong>{item.code}</strong><span>{item.complete ? '已完成' : '未完成'}</span></div>
        <div className="student-score"><b>{item.score_rate == null ? '—' : item.score}</b><small>/ {item.max_score}</small></div>
        <div className="student-rate"><strong>{item.score_rate == null ? '未完成' : `${item.score_rate}%`}</strong><div className="mini-progress"><i style={{ width: `${item.total_questions ? Math.round(item.graded_questions / item.total_questions * 100) : 0}%` }} /></div><span>已判 {item.graded_questions}/{item.total_questions}</span></div>
        <div className="student-flags"><span>待判 {item.pending}</span><span>失败 {item.failed}</span><span>待复核 {item.review}</span></div>
        <span className="student-arrow">→</span>
      </button>)}</div>
    </>}

    {level === 'student' && student && <>
      <div className="batch-head"><div><h1>{student.code}</h1><p>{cls?.code} · 当前批次个人成绩</p></div><button onClick={() => go('class')}>返回班级</button></div>
      <div className="batch-title"><h2>题组概览</h2><p>当前 CSV 没有独立题组字段，按测验节点分组。</p></div>
      <div className="group-grid">{groups.map(item => <button className="group-card" key={item.id} onClick={() => void openGroup(item)}>
        <strong>{item.name}</strong><span>{item.questions} 题 · 已判 {item.graded}</span><span>{item.score}/{item.max_score} · {item.score_rate == null ? '—' : `${item.score_rate}%`}</span>
      </button>)}</div>
    </>}

    {level === 'group' && group && <>
      <div className="batch-head"><div><h1>{group.name}</h1><p>{student?.code} · 当前批次题组</p></div><button onClick={() => go('student')}>返回学生</button></div>
      <div className="question-list">{questions.map((item, index) => <button className="question-row" key={item.id} onClick={() => void openQuestion(item)}>
        <span className="question-index">{index + 1}</span><div><strong>{item.question_id}</strong><small>{item.question_type} · 满分 {item.question_max_score}</small></div><span>{item.score == null ? '未判' : `${item.score}/${item.question_max_score}`}</span><span className="status-pill">{statusLabel(item.grade_status)}</span>
      </button>)}</div>
    </>}

    {level === 'question' && detail && <QuestionDetail detail={detail} onCorrect={correct} onBack={() => go('group')} />}
  </section>;
}

function percent(job: Row | null): number {
  if (!job || !Number(job.total)) return 0;
  return Math.min(100, Math.round(Number(job.completed || 0) / Number(job.total) * 100));
}

function statusLabel(value: unknown): string {
  const map: Record<string, string> = { graded: '已判题', needs_review: '待复核', failed: '判题失败', pending: '待判题', running: '运行中', completed: '已完成', paused: '已暂停', pausing: '暂停中', queued: '排队中', partially_failed: '部分失败', manual_confirmed: '已确认', manual_corrected: '已人工修正' };
  return map[String(value || '')] || String(value || '—');
}

function QuestionDetail({ detail, onCorrect, onBack }: { detail: Row; onCorrect: (score: number, note: string) => Promise<void>; onBack: () => void }) {
  const [score, setScore] = useState(String(detail.grade?.score ?? ''));
  const [note, setNote] = useState('');
  const steps = (() => { try { return JSON.parse(detail.grade?.step_results_json || '[]'); } catch { return []; } })();
  return <div className="question-detail">
    <div className="batch-head"><div><h1>{detail.answer.question_id}</h1><p>{detail.answer.student_code || ''} · {detail.answer.event_id} r{detail.answer.revision}</p></div><button onClick={onBack}>返回题组</button></div>
    <div className="detail-grid"><div className="detail-main">
      <h2>题目</h2><p className="long-text">{detail.question?.stem}</p>
      <h2>参考答案</h2><p className="long-text">{detail.question?.reference_answer}</p>
      <h2>参考解析</h2><p className="long-text">{detail.question?.reference_solution}</p>
      <h2>原始评分点</h2><p className="long-text">{detail.question?.scoring_points}</p>
      <h2>学生原始作答</h2><p className="long-text">{detail.answer.original_answer || '未作答'}</p>
      <h2>学生解题过程</h2><pre className="long-text">{detail.answer.solution_process || '无解题过程'}</pre>
      <h2>逐步骤判定</h2>{steps.length ? steps.map((item: Row, index: number) => <div className="step-row" key={index}><strong>步骤 {item.step_index ?? index + 1} · {item.score}/{item.max_score}</strong><p>{item.student_step}</p><small>{item.judgment_reason}</small></div>) : <p className="batch-muted">没有可用的步骤级结果，按整体判定。</p>}
    </div><aside className="detail-side">
      <h2>AI 判题</h2><div className="big-score">{detail.grade?.score ?? '—'}<small>/{detail.question?.max_score}</small></div><p>{statusLabel(detail.grade?.status)}</p><p className="long-text">{detail.grade?.judgment_reason}</p>
      <h2>人工复核</h2><input value={score} onChange={event => setScore(event.target.value)} placeholder="修正分数" /><textarea value={note} onChange={event => setNote(event.target.value)} placeholder="必须填写修改原因" /><button onClick={() => void onCorrect(Number(score), note)}>保存修正</button>
      <h2>历史版本</h2>{detail.grades?.map((item: Row) => <div className="step-row" key={item.id}><strong>v{item.grading_version} · {item.score}/{item.max_score}</strong><small>{item.grading_model} · {statusLabel(item.status)} · {item.created_at}</small></div>)}
      <h2>修正记录</h2>{detail.corrections?.map((item: Row) => <p className="batch-muted" key={item.id}>{item.created_at} · {item.note}</p>)}
    </aside></div>
  </div>;
}
