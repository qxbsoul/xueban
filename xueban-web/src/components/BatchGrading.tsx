import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost, apiFetch } from '../api';
import { AnswerErrorLabels } from './AnalyticsExtras';
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
  const [jobItems, setJobItems] = useState<Row[]>([]);
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
  const [preprocess, setPreprocess] = useState<Row | null>(null);
  const [jobData, setJobData] = useState<Row | null>(null);
  const [records, setRecords] = useState<Row | null>(null);
  const [recordState, setRecordState] = useState('anomaly');
  const [issueStatus, setIssueStatus] = useState('all');
  const [openRecord, setOpenRecord] = useState('');
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
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const data = await apiGet<Row>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
        setJob(data.job); setJobItems(data.items || []); setJobData(data);
        // 暂停也要继续轮询：用户点击“继续”后无需刷新页面即可恢复进度。
        if (['completed', 'failed'].includes(data.job.status)) stopped = true;
      } catch { /* 轮询失败不打断页面，下一轮继续 */ }
    };
    void tick();
    const timer = window.setInterval(() => { void tick(); }, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [job?.id]);

  useEffect(() => {
    if (level !== 'batch' || !batch?.id) return;
    if (!['ready', 'confirmed'].includes(String(preprocess?.status || batch.preprocess_status || ''))) return;
    void loadRecords(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level, batch?.id, recordState, issueStatus, preprocess?.status]);

  // 预处理期间自动轮询，完成后自动开放判题入口。
  useEffect(() => {
    if (level !== 'batch' || !batch?.id || batch.preprocess_status !== 'preprocessing') return;
    const timer = window.setInterval(async () => {
      try {
        const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batch.id)}/overview`);
        setBatch(data.batch); setClasses(data.classes || []); setJobs(data.jobs || []); setJob(data.latest_job || null);
        setPreprocess(await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batch.id)}/preprocessing`).catch(() => null));
      } catch { /* 下一轮继续 */ }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [level, batch?.id, batch?.preprocess_status]);

  const loadRecords = async (page = 1, append = false) => {
    if (!batch?.id) return;
    try {
      const query = `/api/teacher/batches/${encodeURIComponent(batch.id)}/preprocess-records?state=${recordState}&issue_status=${issueStatus}&page=${page}&page_size=20`;
      const data = await apiGet<Row>(query);
      setRecords(current => append && current
        ? { ...data, records: [...(current.records || []), ...(data.records || [])] }
        : data);
    } catch (error) { setMessage(String(error)); }
  };

  const deleteBatch = async (item: Row) => {
    const ok = window.confirm(`确定删除导入任务「${item.filename}」？该任务的全部作答、判题任务、判题结果和人工修正都会删除，此操作不可恢复。`);
    if (!ok) return;
    try {
      await apiDelete(`/api/teacher/batches/${encodeURIComponent(item.id)}`);
      setLevel('list'); setBatch(null); setJob(null); setClasses([]); setBatches((await apiGet<{ items: Row[] }>('/api/teacher/batches')).items || []);
      setMessage(''); window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (error) { setMessage(String(error)); }
  };
  const openBatch = async (id: string) => {
    setBusy(true);
    try {
      const data = await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(id)}/overview`);
      setBatch(data.batch); setClasses(data.classes || []); setJobs(data.jobs || []); setJob(data.latest_job || null);
      setPreprocess(await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(id)}/preprocessing`).catch(() => null));
      setRecords(null); setOpenRecord(''); setRecordState('anomaly'); setIssueStatus('all');
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
  const confirmPreprocess = async () => {
    if (!batch) return;
    try { await apiPost(`/api/teacher/batches/${encodeURIComponent(batch.id)}/preprocessing/confirm`, {}); setPreprocess(await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batch.id)}/preprocessing`)); setMessage('数据预处理已确认，可以开始判题。'); }
    catch (error) { setMessage(String(error)); }
  };
  const start = async () => {
    if (!batch) return;
    setBusy(true); setMessage('正在创建判题任务…');
    try {
      const data = await apiPost<{ job: Row }>('/api/teacher/grade/batch', { batch_id: batch.id, class_id: cls?.id || '', textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterText });
      setJob(data.job); setMessage(`任务已创建：${data.job.total} 条待判题。`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const action = async (name: 'pause' | 'resume' | 'retry') => {
    if (!job?.id) return;
    try {
      const data = await apiPost<Row>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/${name}`, {});
      setJob(data.job); setJobData(data); await onChanged();
      if (data.note) setMessage(String(data.note));
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

  const ready = ['ready', 'confirmed'].includes(String(preprocess?.status || batch?.preprocess_status || ''));
  const preprocessing = String(batch?.preprocess_status || preprocess?.status || '') === 'preprocessing';
  const counts = jobData?.counts || {};
  const totalCount = Number(counts.total ?? job?.total ?? batch?.total ?? 0);
  const successCount = Number(counts.success ?? job?.success ?? job?.completed ?? batch?.graded ?? 0);
  const reviewCount = Number(counts.review ?? job?.review ?? job?.needs_review ?? batch?.review ?? 0);
  const failedCount = Number(counts.failed ?? job?.failed ?? batch?.failed ?? 0);
  const pendingCount = Number(counts.pending ?? Math.max(0, totalCount - successCount - reviewCount - failedCount));
  const completedCount = Number(counts.completed ?? (successCount + reviewCount + failedCount));
  const progress = Number(jobData?.progress ?? (totalCount ? Math.round(completedCount / totalCount * 1000) / 10 : 0));
  const retryProgress = jobData?.retry_progress || null;
  const activeStatus = String(job?.status || '');

  return <section className="batch-section">
    {level !== 'list' && <div className="batch-crumbs">{crumbs.map(([label, target], index) => <button key={`${label}-${index}`} onClick={() => go(target as Level)}>{index > 0 ? ' / ' : ''}{String(label)}</button>)}</div>}

    {level === 'list' && <div className="batch-list">
      <div className="batch-title"><h1>导入任务</h1><p>先选择一个导入任务。未选择任务时不会查询或展示任何学生作答。</p></div>
      {batches.length === 0 && <div className="batch-empty">还没有导入任务。请先到数据管理上传五个 CSV。</div>}
      {batches.map(item => <div className="batch-row" key={item.id}>
        <div><strong>{item.filename}</strong><small>{item.id} · {item.created_at}</small></div>
        <div className="batch-metrics"><span>班级 {item.classes || 0}</span><span>学生 {item.students || 0}</span><span>题目 {item.questions || 0}</span><span>作答 {item.total || 0}</span><span>待判 {item.pending || 0}</span><span>已完成 {item.graded || 0}</span><span>待复核 {item.needs_review || 0}</span></div>
        <div className="batch-row-actions"><button disabled={busy} onClick={() => void openBatch(item.id)}>进入任务</button><button className="danger-button" disabled={busy} onClick={() => void deleteBatch(item)}>删除任务</button></div>
      </div>)}
    </div>}

    {level === 'batch' && batch && <>
      <div className="batch-head"><div><h1>{batch.filename}</h1><p>{batch.id} · 导入 {batch.created_at}</p></div><div className="batch-head-actions"><button onClick={() => go('list')}>返回任务列表</button></div></div>
      {preprocess && <div className="preprocess-card">
        <div className="preprocess-head">
          <div><h2>数据预处理</h2><p>{preprocessing ? '正在预处理，完成后自动开放判题入口…' : (ready ? '预处理完成，可开始判题' : '预处理状态未知，请刷新任务')}</p></div>
          <span className={`preprocess-badge ${preprocessing ? 'running' : 'ready'}`}>{preprocessing ? '正在预处理' : '预处理完成'}</span>
        </div>
        <div className="preprocess-stats">
          <div><b>{preprocess.report?.raw_records ?? 0}</b><span>原始记录</span></div>
          <div><b>{preprocess.report?.valid_records ?? 0}</b><span>有效记录</span></div>
          <div><b>{preprocess.report?.anomaly_records ?? 0}</b><span>异常记录</span></div>
          <div><b>{preprocess.report?.pending_records ?? preprocess.report?.pending_confirmation ?? 0}</b><span>需人工核实</span></div>
        </div>
        <div className="preprocess-quality">
          <span>异常作答记录 {preprocess.report?.anomaly_records ?? 0} 条</span>
          <span>需人工核实记录 {preprocess.report?.pending_records ?? 0} 条</span>
          <span>待处理问题记录 {preprocess.report?.pending_issues ?? 0} 条（与异常作答记录不是同一单位）</span>
          <span>缺少过程提醒 {preprocess.report?.process_reminders ?? 0}</span>
          <span>空答案 {preprocess.report?.empty_answers ?? 0}</span>
        </div>
        <div className="preprocess-filters">
          {[['anomaly', '异常记录'], ['pending_confirmation', '需人工核实'], ['valid', '有效记录'], ['all', '全部记录']].map(([value, label]) => <button key={value} className={recordState === value ? 'on' : ''} onClick={() => { setRecordState(value); setOpenRecord(''); }}>{label}</button>)}
          <select value={issueStatus} onChange={event => { setIssueStatus(event.target.value); setOpenRecord(''); }}>
            <option value="all">全部问题状态</option>
            <option value="detected">待处理</option>
            <option value="pending_review">待复核</option>
            <option value="accepted">已接受</option>
            <option value="auto_normalized">已自动规范化</option>
            <option value="resolved">已解决</option>
            <option value="excluded">已排除</option>
          </select>
        </div>
        {records && <div className="preprocess-records">
          <p className="batch-muted">当前筛选 {records.total} 条作答记录；问题关联 {records.counts?.issue_total ?? 0} 条（含已接受/已排除提醒），其中待处理 {records.counts?.pending_issue_count ?? 0} 条。一条作答可有多个问题，不重复计入异常作答数。</p>
          {records.records?.map((item: Row) => <div className={`preprocess-record ${item.state}`} key={item.answer_id}>
            <button className="preprocess-record-head" onClick={() => setOpenRecord(openRecord === item.answer_id ? '' : item.answer_id)}>
              <strong>{item.student_code}</strong><span>{item.class_code || '—'}</span><span>{item.question_id}</span><span>r{item.revision} · {item.event_id || '—'}</span><span className="preprocess-record-state">{item.state_label}</span><span>问题 {item.issue_count}</span>
            </button>
            {item.flags?.length > 0 && <p className="batch-muted">{item.flags.join('；')}</p>}
            {openRecord === item.answer_id && <div className="preprocess-issues">
              {item.issues?.length ? item.issues.map((issue: Row) => <div className="preprocess-issue" key={issue.id}>
                <div><strong>{issue.issue_type}</strong><span>{issue.severity}</span><span>{issue.handling_status}</span></div>
                <p>字段：{issue.field || '—'} · 原始值：{issue.raw_value ?? '—'} · 期望值：{issue.expected_value ?? '—'}</p>
                <p>原因：{issue.reason || '—'}</p>
                <p>检测规则：{issue.detection_rule || '—'} · 处理结果：{issue.handling_result || '尚无'}</p>
              </div>) : <p className="batch-muted">该记录没有问题明细。</p>}
            </div>}
          </div>)}
          {records.has_more && <button className="issue-more" onClick={() => void loadRecords(records.page + 1, true)}>加载更多（{records.page * records.page_size}/{records.total}）</button>}
        </div>}
        <div className="preprocess-classes">{preprocess.report?.classes?.map((item: Row) => <div key={item.code}><strong>{item.code}</strong><span>{item.students} 名学生 · {item.answers} 条作答 · 有效 {item.valid} · 异常 {item.anomaly} · 需人工核实 {item.pending_confirmation}</span></div>)}</div>
      </div>}
      <div className="batch-progress-card">
        <div className="batch-progress-top"><strong>{job ? statusLabel(job.status) : (preprocessing ? '正在预处理' : '待开始判题')}</strong><span>{progress}%</span></div>
        <div className="batch-progress"><i style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} /></div>
        <div className="batch-metrics">
          <span>整体 {completedCount}/{totalCount}</span>
          <span>成功 {successCount}</span>
          <span>待复核 {reviewCount}</span>
          <span>失败 {failedCount}</span>
          <span>执行中 {Number(counts.running ?? job?.running_count ?? 0)}</span>
          <span>待处理 {pendingCount}</span>
        </div>
        {retryProgress && <div className="batch-retry">
          <strong>失败重判 {retryProgress.finished}/{retryProgress.total}</strong>
          <div className="batch-progress"><i style={{ width: `${retryProgress.percent}%` }} /></div>
          <span>成功 {retryProgress.success} · 待复核 {retryProgress.review ?? 0} · 失败 {retryProgress.failed}</span>
        </div>}
        <div className="batch-actions">
          {!job && <button disabled={busy || !batch.pending || !ready} onClick={() => void start()}>{preprocessing ? '预处理完成后可判题' : '开始判题'}</button>}
          {job && ['running', 'queued', 'retrying'].includes(activeStatus) && <button disabled={busy} onClick={() => void action('pause')}>暂停判题</button>}
          {job && activeStatus === 'pausing' && <button disabled>正在暂停…</button>}
          {job && activeStatus === 'paused' && <button disabled={busy} onClick={() => void action('resume')}>继续判题</button>}
          {job && ['completed', 'failed'].includes(activeStatus) && failedCount > 0 && <button disabled={busy} onClick={() => void action('retry')}>仅重试失败项（{failedCount} 条）</button>}
          {job && activeStatus === 'completed' && failedCount === 0 && <span className="batch-done">全部完成</span>}
          {job && activeStatus === 'retrying' && <span className="batch-done">正在重判失败记录</span>}
        </div>
        {job && <p className="batch-muted">当前作答：{job.current_answer_id || '—'} · 最近检查点：{job.last_checkpoint_at || '—'} · 状态：{statusLabel(job.status)}</p>}
        {job && failedCount > 0 && <FailurePanel jobId={String(job.id)} expected={failedCount} active={['running', 'queued', 'pausing', 'retrying'].includes(activeStatus)} />}
      <div className="batch-context"><label>参考学案（可选）<select value={textbookId} onChange={event => setTextbookId(event.target.value)}><option value="">不使用学案</option>{textbooks.map(book => <option key={book.id} value={book.id}>{book.title}</option>)}</select></label>{textbookId && <label>章节<select value={chapterId} onChange={event => { setChapterId(event.target.value); const item = chapters.find(c => c.id === event.target.value); setChapterText(item?.lesson?.content || item?.summary || ''); }}><option value="">不指定章节</option>{chapters.map(item => <option key={item.id} value={item.id}>{item.chapter_title} / {item.title}</option>)}</select></label>}</div>
      </div>
      <div className="batch-title"><h2>班级概览</h2><p>成绩只统计当前任务；平均分仅按已完成学生计算。</p></div>
      <div className="class-grid">{classes.map(item => <button className="class-card" key={item.id} onClick={() => void openClass(item)}>
        <div className="class-card-head"><strong>{item.code}</strong><span>{item.completed_students}/{item.students} 完成</span></div>
        <div className="class-score"><b>{item.average_score ?? '—'}</b><small>平均分</small></div>
        <div className="class-card-stats"><span>平均得分率 {item.average_rate == null ? '—' : `${item.average_rate}%`}</span><span>最高 {item.highest ?? '—'} / 最低 {item.lowest ?? '—'}</span><span>作答 {item.answers} 条</span></div>
        <div className="class-progress"><i style={{ width: `${item.answers ? Math.round(item.completed_students / Math.max(1, item.students) * 100) : 0}%` }} /></div>
      </button>)}</div>
    </>}

    {level === 'class' && cls && <>
      <div className="batch-head"><div><h1>{cls.code}</h1><p>{batch?.filename} · 当前任务班级</p></div><button onClick={() => go('batch')}>返回任务</button></div>
      <div className="batch-title"><h2>学生概览</h2><p>个人得分来自当前任务的有效判题；未完成学生不按 0 分计算。</p></div>
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
      <div className="batch-head"><div><h1>{student.code}</h1><p>{cls?.code} · 当前任务个人成绩</p></div><button onClick={() => go('class')}>返回班级</button></div>
      <div className="batch-title"><h2>题组概览</h2><p>当前 CSV 没有独立题组字段，按测验节点分组。</p></div>
      <div className="group-grid">{groups.map(item => <button className="group-card" key={item.id} onClick={() => void openGroup(item)}>
        <strong>{item.name}</strong><span>{item.questions} 题 · 已判 {item.graded}</span><span>{item.score}/{item.max_score} · {item.score_rate == null ? '—' : `${item.score_rate}%`}</span>
      </button>)}</div>
    </>}

    {level === 'group' && group && <>
      <div className="batch-head"><div><h1>{group.name}</h1><p>{student?.code} · 当前任务题组</p></div><button onClick={() => go('student')}>返回学生</button></div>
      <div className="question-list">{questions.map((item, index) => <button className="question-row" key={item.id} onClick={() => void openQuestion(item)}>
        <span className="question-index">{index + 1}</span><div><strong>{item.question_id}</strong><small>{item.question_type} · 满分 {item.question_max_score} · {String(item.stem || '').slice(0, 70)}</small></div><div className="question-score"><span>{item.score == null ? '未判' : `${item.score}/${item.question_max_score}`}</span><div className="mini-progress"><i style={{ width: `${item.score == null ? 0 : Math.round(Number(item.score) / Math.max(1, Number(item.question_max_score)) * 100)}%` }} /></div></div><span className="status-pill">{statusLabel(item.grade_status)}</span>
      </button>)}</div>
    </>}

    {level === 'question' && detail && <QuestionDetail detail={detail} onCorrect={correct} onBack={() => go('group')} onRegrade={async () => {
      try {
        await apiPost(`/api/teacher/answers/${encodeURIComponent(detail.answer.id)}/grade`, {});
        await openQuestion({ id: detail.answer.id });
        setMessage('已完成单题重新判题，历史判题版本保留。');
      } catch (error) { setMessage(String(error)); }
    }} />}
  </section>;
}

function percent(job: Row | null): number {
  if (!job || !Number(job.total)) return 0;
  return Math.min(100, Math.round(Number(job.completed || 0) / Number(job.total) * 100));
}

function statusLabel(value: unknown): string {
  const map: Record<string, string> = { graded: '已判题', needs_review: '待复核', failed: '判题失败', pending: '待判题', running: '正在判题', completed: '已完成', paused: '已暂停', pausing: '正在暂停', queued: '排队中', retrying: '正在重判', preprocessing: '正在预处理', partially_failed: '已完成', manual_confirmed: '已确认', manual_corrected: '已人工修正' };
  return map[String(value || '')] || String(value || '—');
}

function resultTitle(grade: Row | null | undefined): string {
  if (!grade) return '判题结果';
  if (grade.status === 'failed') return '判题失败详情';
  if (grade.status === 'manual_corrected') return '人工修正结果';
  if (grade.status === 'manual_confirmed') return '已确认判题结果';
  if (grade.needs_review) return '待人工复核';
  return grade.grading_model === 'rule-assisted' ? '判题结果' : 'AI 判题结果';
}

function FailurePanel({ jobId, expected, active }: { jobId: string; expected: number; active: boolean }) {
  const [data, setData] = useState<Row | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [open, setOpen] = useState('');
  const pageSize = 10;
  useEffect(() => {
    let mounted = true;
    let controller = new AbortController();
    const load = async () => {
      controller.abort();
      controller = new AbortController();
      try {
        const response = await apiFetch(`/api/teacher/grade/jobs/${encodeURIComponent(jobId)}/failures?page=${page}&page_size=${pageSize}`, { signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
        if (mounted) { setData(body); setError(''); }
      } catch (err) {
        if (mounted && (err as Row)?.name !== 'AbortError') setError(String(err));
      }
    };
    void load();
    const timer = active ? window.setInterval(() => { void load(); }, 2500) : null;
    return () => { mounted = false; if (timer !== null) window.clearInterval(timer); controller.abort(); };
  }, [jobId, page, expected, active]);
  if (error) return <div className="job-failures"><strong>判题失败明细</strong><p>读取失败：{error}</p></div>;
  if (!data) return null;
  const items = data.items || [];
  return <div className="job-failures">
    <strong>判题失败明细</strong>
    <p className="batch-muted">失败作答 {data.total} 条 · 任务记录 {data.job?.failed ?? '—'} 条 · 执行尝试 {data.counts?.attempts ?? 0} 次 · 对账 {data.consistent ? '一致' : '不一致（请先重试同步）'}（传入期望 {expected}）</p>
    {items.map((item: Row) => <div className="failure-row" key={item.item_id}>
      <button onClick={() => setOpen(open === item.item_id ? '' : item.item_id)}>
        <span>{item.student_code || item.answer_id}</span>
        <span>{item.question_id} · {item.event_id} r{item.revision}</span>
        <span className="failure-label">{item.error_label}</span>
        <span>尝试 {item.attempt_count} 次{item.retryable ? ' · 可重试' : ' · 需人工处理'}</span>
      </button>
      {open === item.item_id && <div className="failure-detail">
        <p><b>说明：</b>{item.error_label}（{item.error_type} · {item.error_stage || '未知阶段'}）</p>
        {item.last_error && <p><b>最后一次错误：</b>{item.last_error}</p>}
        {item.error_detail && <details><summary>原始异常</summary><pre>{item.error_detail}</pre></details>}
        {(item.attempts || []).length > 0 && <details><summary>执行尝试（{item.attempts.length}）</summary><table><thead><tr><th>#</th><th>开始</th><th>耗时</th><th>阶段</th><th>类型</th><th>可重试</th></tr></thead><tbody>
          {item.attempts.map((attempt: Row) => <tr key={`${attempt.attempt_number}-${attempt.start_time}`}><td>{attempt.attempt_number}</td><td>{attempt.start_time}</td><td>{attempt.duration_ms} ms</td><td>{attempt.failure_stage || '成功'}</td><td>{attempt.error_type || '—'}</td><td>{attempt.retryable ? '是' : '否'}</td></tr>)}
        </tbody></table></details>}
      </div>}
    </div>)}
    <div className="failure-pager">
      <button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
      <span>第 {data.page} 页 · 每页 {data.page_size}</span>
      <button disabled={!data.has_more} onClick={() => setPage(page + 1)}>下一页</button>
    </div>
  </div>;
}

function QuestionDetail({ detail, onCorrect, onBack, onRegrade }: { detail: Row; onCorrect: (score: number, note: string) => Promise<void>; onBack: () => void; onRegrade: () => Promise<void> }) {
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
      <h2>逐步骤判定</h2>{steps.length ? steps.map((item: Row, index: number) => <div className="step-row" key={index}><div className="step-head"><strong>步骤 {item.step_index ?? index + 1} · {item.score}/{item.max_score}</strong><span>{item.is_correct === true ? '正确' : item.is_correct === false ? '错误' : '待复核'}</span></div><p className="step-original">{item.student_step}</p>{item.normalized_step && <p><b>规范化：</b>{item.normalized_step}</p>}{item.operation_type && <p><b>操作：</b>{item.operation_type} · {item.reasoning_purpose || ''}</p>}{item.expected_step && <p><b>参考：</b>{item.expected_step}</p>}{Array.isArray(item.scoring_point_links) && item.scoring_point_links.length > 0 && <p><b>评分点：</b>{item.scoring_point_links.map((link: Row) => `${link.scoring_point_id}（${link.awarded_score}/${link.max_score}）`).join('、')}</p>}{item.error_type && <p><b>错误类型：</b>{item.error_type}</p>}{Array.isArray(item.operation_units) && item.operation_units.length > 0 && <div className="operation-list">{item.operation_units.map((operation: Row) => <div className="operation-row" key={operation.operation_id}><strong>{operation.operation_id} · {operation.operation_type} · {operation.score}/{operation.max_score}</strong><p>{operation.student_fragment}</p><small>{operation.normalized_operation} {operation.reasoning_purpose ? `· ${operation.reasoning_purpose}` : ''}</small></div>)}</div>}<small>{item.judgment_reason}</small></div>) : <p className="batch-muted">{detail.answer?.solution_process ? '步骤分析未成功，待人工复核；原始过程已保留。' : '学生未提交解题过程。'}</p>}
      <AnswerErrorLabels answerId={detail.answer.id} />
    </div><aside className="detail-side">
      <h2>{resultTitle(detail.grade)}</h2><div className="big-score">{detail.grade?.score ?? '—'}<small>/{detail.question?.max_score}</small></div><p>{statusLabel(detail.grade?.status)}</p><p className="long-text">{detail.grade?.judgment_reason}</p>
      <h2>人工复核</h2><button onClick={() => void onRegrade()}>用当前模型重新判题</button><input value={score} onChange={event => setScore(event.target.value)} placeholder="修正分数" /><textarea value={note} onChange={event => setNote(event.target.value)} placeholder="必须填写修改原因" /><button onClick={() => void onCorrect(Number(score), note)}>保存修正</button>
      <h2>历史版本</h2>{detail.grades?.map((item: Row) => <div className="step-row" key={item.id}><strong>v{item.grading_version} · {item.score}/{item.max_score}</strong><small>{item.grading_model} · {statusLabel(item.status)} · {item.created_at}</small></div>)}
      <h2>修正记录</h2>{detail.corrections?.map((item: Row) => <p className="batch-muted" key={item.id}>{item.created_at} · {item.note}</p>)}
    </aside></div>
  </div>;
}
