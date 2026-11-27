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
  const [qualityGate, setQualityGate] = useState<Row | null>(null);
  const [forceReason, setForceReason] = useState('');
  const [showIssues, setShowIssues] = useState(false);
  const [issue, setIssue] = useState<Row | null>(null);
  const [issueItems, setIssueItems] = useState<Row[]>([]);
  const [issueTotal, setIssueTotal] = useState(0);
  const [issuePage, setIssuePage] = useState(1);
  const [issueHasMore, setIssueHasMore] = useState(false);
  const [studentSort, setStudentSort] = useState('code');
  const [reparsePreview, setReparsePreview] = useState<Row | null>(null);
  const [reparseBusy, setReparseBusy] = useState(false);

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
      const data = await apiGet<{ job: Row; items: Row[] }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}`);
      setJob(data.job); setJobItems(data.items || []);
      if (!['running', 'queued', 'pausing'].includes(data.job.status)) window.clearInterval(timer);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.id]);

  const loadIssues = async (page = 1, append = false) => {
    if (!batch?.id) return;
    const data = await apiGet<{ items: Row[]; total: number; has_more: boolean }>(`/api/teacher/batches/${encodeURIComponent(batch.id)}/issues?status=pending&page=${page}&page_size=50`);
    setIssueItems(append ? [...issueItems, ...(data.items || [])] : (data.items || [])); setIssueTotal(data.total || 0); setIssuePage(page); setIssueHasMore(Boolean(data.has_more)); setIssue(null);
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
      setQualityGate(await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(id)}/quality-gate`).catch(() => null));
      setIssueItems([]); setIssueTotal(0); setIssue(null); setShowIssues(false);
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
  const start = async (force = false) => {
    if (!batch) return;
    setBusy(true); setMessage(force ? '正在以强制模式创建判题任务（会记录留痕）…' : '正在创建判题任务…');
    try {
      const data = await apiPost<{ job: Row; quality_gate?: Row }>('/api/teacher/grade/batch', { batch_id: batch.id, class_id: cls?.id || '', textbook_course_id: textbookId, textbook_unit_id: chapterId, textbook_context: chapterText, force, force_reason: force ? forceReason : '' });
      setJob(data.job); setForceReason(''); setMessage(force ? `已在质量闸门阻断情况下强制创建任务：${data.job.total} 条待判题（已留痕）。` : `任务已创建：${data.job.total} 条待判题。`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const action = async (name: 'pause' | 'resume' | 'retry') => {
    if (!job?.id) return;
    try {
      const data = await apiPost<{ job: Row }>(`/api/teacher/grade/jobs/${encodeURIComponent(job.id)}/${name}`, {});
      setJob(data.job); await onChanged();
    } catch (error) { setMessage(String(error)); }
  };
  const reparse = async (dryRun: boolean) => {
    if (!batch?.id) return;
    setReparseBusy(true);
    try {
      const data = await apiPost<Row>(`/api/teacher/batches/${encodeURIComponent(batch.id)}/reparse-history`, { dry_run: dryRun });
      setReparsePreview(data);
      setMessage(dryRun
        ? `可重解析 ${data.candidates} 条：预计成功 ${data.reparsed}，需复核 ${data.needs_review}，仍失败 ${data.failed}（未写入）。`
        : `重解析完成：成功 ${data.reparsed}，需复核 ${data.needs_review}，失败 ${data.failed}，剩余候选 ${data.remaining_candidates}。旧判题记录已保留。`);
      if (!dryRun) { await onChanged(); }
    } catch (error) { setMessage(String(error)); } finally { setReparseBusy(false); }
  };
;
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
      {qualityGate && <div className={`quality-gate ${qualityGate.blocking ? 'blocking' : ''}`}><strong>判题质量闸门：{qualityGate.blocking ? '阻断' : '通过'}</strong><span>作答 {qualityGate.answer_count} 条 · 问题 {qualityGate.issues?.length || 0} 项</span>{qualityGate.issues?.map((item: Row) => <p key={item.type}>{item.type} · {item.count ?? ''} · {item.message}</p>)}
        {qualityGate.blocking && <div style={{ marginTop: 8 }}><p>服务端会拒绝在阻断状态下创建判题任务。如确需继续，必须填写理由，系统会记录教师、时间、问题与理由。</p><textarea value={forceReason} onChange={event => setForceReason(event.target.value)} placeholder="强制绕过理由（至少 5 个字）" style={{ width: '100%', minHeight: 52 }} /></div>}
      </div>}
      {preprocess && <div className="preprocess-card">
        <div className="preprocess-head"><div><h2>数据预处理报告</h2><p>{preprocess.status === 'confirmed' ? '已确认，可进入判题' : '需要教师确认后才能开始判题'}</p></div>{preprocess.status !== 'confirmed' && <button onClick={() => void confirmPreprocess()}>确认预处理，允许判题</button>}</div>
        <div className="preprocess-stats"><div><b>{preprocess.report.raw_records}</b><span>原始记录</span></div><div><b>{preprocess.report.valid_records}</b><span>有效记录</span></div><div><b>{preprocess.report.duplicate_groups}</b><span>重复组</span></div><div><b>{preprocess.report.anomalies}</b><span>异常</span></div><div><b>{preprocess.report.missing.solution_process}</b><span>无解题过程</span></div><div><b>{preprocess.report.pending_confirmation}</b><span>待确认</span></div></div>
        <div className="preprocess-actions"><button onClick={() => { const next = !showIssues; setShowIssues(next); setIssue(null); if (next) void loadIssues(1); }}>{showIssues ? '收起问题明细' : `查看问题明细（${preprocess.report.pending_issues || 0}）`}</button>{!preprocess.report.pending_issues && !showIssues && <span className="issue-none">没有待处理问题</span>}</div>
        {showIssues && <>
          <div className="issue-list">{issueItems.map((item: Row) => <button className={`issue-row ${item.severity}`} key={item.id} onClick={() => setIssue(item)}><span>{item.issue_type}</span><span>{item.severity}</span><span>{item.student_code || '—'} · {item.class_code || '—'}</span><span>{item.question_id || '—'}</span><span>{item.handling_status}</span></button>)}</div>
          {issueTotal > issueItems.length && <button className="issue-more" onClick={() => void loadIssues(issuePage + 1, true)}>加载更多（{issueItems.length}/{issueTotal}）</button>}
        </>}
        {issue && <div className="issue-detail"><strong>{issue.issue_type}</strong><p>学生 {issue.student_code || '—'} · 班级 {issue.class_code || '—'} · 题目 {issue.question_id || '—'}</p><p>原始值：{issue.raw_value || '—'}</p><p>期望值：{issue.expected_value || '—'}</p><p>原因：{issue.reason}</p><p>检测规则：{issue.detection_rule}</p><p>处理状态：{issue.handling_status} · {issue.handling_result || '尚无处理结果'}</p><button onClick={() => setIssue(null)}>关闭</button></div>}
        <div className="preprocess-classes">{preprocess.report.classes?.map((item: Row) => <div key={item.code}><strong>{item.code}</strong><span>{item.students} 名学生 · {item.answers} 条作答 · {item.valid} 条有效 · {item.anomalies} 异常</span></div>)}</div>
      </div>}
      <div className="batch-progress-card">
        <div className="batch-progress-top"><strong>{job ? statusLabel(job.status) : '导入完成，待判题'}</strong><span>{percent(job)}%</span></div>
        <div className="batch-progress"><i style={{ width: `${percent(job)}%` }} /></div>
        <div className="batch-metrics"><span>总数 {job?.total ?? batch.total}</span><span>成功 {job?.completed ?? batch.graded}</span><span>失败 {job?.failed ?? batch.failed}</span><span>待处理 {job ? Math.max(0, Number(job.total || 0) - Number(job.completed || 0) - Number(job.failed || 0)) : batch.pending}</span><span>待复核 {job?.needs_review ?? batch.review}</span></div>
        <div className="batch-actions">{!job && <button disabled={busy || !batch.pending || preprocess?.status !== 'confirmed' || Boolean(qualityGate?.blocking)} onClick={() => void start()}>开始判题</button>}{!job && qualityGate?.blocking && <button disabled={busy || forceReason.trim().length < 5} onClick={() => void start(true)}>强制开始判题（留痕）</button>}{job && ['running', 'queued', 'pausing'].includes(job.status) && <button onClick={() => void action('pause')}>暂停判题</button>}{job && ['paused', 'partially_failed'].includes(job.status) && <button onClick={() => void action('resume')}>继续判题</button>}{job && Number(job.failed) > 0 && <button onClick={() => void action('retry')}>仅重试失败项（{job.failed} 条）</button>}{job?.status === 'completed' && <span className="batch-done">全部完成</span>}</div>
        {job && <p className="batch-muted">当前作答：{job.current_answer_id || '—'} · 最近检查点：{job.last_checkpoint_at || '—'} · 状态：{job.state || statusLabel(job.status)}</p>}
        {job && Number(job.failed) > 0 && <FailurePanel jobId={String(job.id)} expected={Number(job.failed)} />}
        <div className="batch-reparse">
          <div><strong>历史结果修复</strong><span>把「模型已返回、但解析阶段丢了步骤」的历史判题重新解析成新版本：不调用模型、不改原始作答、不覆盖旧记录。</span></div>
          <div className="batch-actions">
            <button disabled={reparseBusy} onClick={() => void reparse(true)}>检查可重解析记录</button>
            {reparsePreview && Number(reparsePreview.candidates) > 0 && <button disabled={reparseBusy} onClick={() => void reparse(false)}>执行重解析（{reparsePreview.candidates} 条）</button>}
            {reparsePreview && <span className="batch-muted">预计成功 {reparsePreview.reparsed} · 需复核 {reparsePreview.needs_review} · 仍失败 {reparsePreview.failed} · 剩余候选 {reparsePreview.remaining_candidates}</span>}
          </div>
        </div>
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
  const map: Record<string, string> = { graded: '已判题', needs_review: '待复核', failed: '判题失败', pending: '待判题', running: '运行中', completed: '已完成', paused: '已暂停', pausing: '暂停中', queued: '排队中', partially_failed: '部分失败', manual_confirmed: '已确认', manual_corrected: '已人工修正' };
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

function FailurePanel({ jobId, expected }: { jobId: string; expected: number }) {
  const [data, setData] = useState<Row | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [open, setOpen] = useState('');
  const pageSize = 10;
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    (async () => {
      try {
        const response = await apiFetch(`/api/teacher/grade/jobs/${encodeURIComponent(jobId)}/failures?page=${page}&page_size=${pageSize}`, { signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
        if (active) { setData(body); setError(''); }
      } catch (err) {
        if (active && (err as Row)?.name !== 'AbortError') setError(String(err));
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [jobId, page]);
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
