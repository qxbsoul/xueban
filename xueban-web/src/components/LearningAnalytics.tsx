import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api';
import { ErrorLabelsPanel, GraphPanel, MappingPanel, StudentKnowledgeDetail } from './AnalyticsExtras';
import './learning-analytics.css';

type Row = Record<string, any>;

function statusLabel(value: unknown): string {
  const map: Record<string, string> = { graded: '已判题', needs_review: '待复核', failed: '判题失败', pending: '待判题', manual_corrected: '已修正', manual_confirmed: '已确认' };
  return map[String(value || '')] || String(value || '—');
}

export function LearningAnalytics({ setMessage }: { setMessage: (value: string) => void }) {
  const [batches, setBatches] = useState<Row[]>([]);
  const [batchId, setBatchId] = useState('');
  const [classes, setClasses] = useState<Row[]>([]);
  const [classId, setClassId] = useState('');
  const [klass, setKlass] = useState<Row | null>(null);
  const [student, setStudent] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [compare, setCompare] = useState<Row | null>(null);
  const [selectedCompare, setSelectedCompare] = useState<string[]>([]);
  const [questionDetail, setQuestionDetail] = useState<Row | null>(null);
  const [longitudinal, setLongitudinal] = useState<Row | null>(null);
  const [panel, setPanel] = useState<'classes' | 'mapping' | 'graph' | 'errors'>('classes');

  useEffect(() => {
    apiGet<{ items: Row[] }>('/api/teacher/batches').then(data => {
      setBatches(data.items || []);
      if (data.items?.length) setBatchId(data.items[0].id);
    }).catch(error => setMessage(String(error)));
  }, []);
  useEffect(() => {
    if (!batchId) return;
    apiGet<{ classes: Row[] }>(`/api/teacher/analysis/classes?batch_id=${encodeURIComponent(batchId)}`).then(data => {
      setClasses(data.classes || []); setSelectedCompare([]); setCompare(null); setKlass(null); setStudent(null); setClassId('');
    }).catch(error => setMessage(String(error)));
  }, [batchId]);
  const loadCompare = async () => {
    if (selectedCompare.length < 2) { setMessage('至少选择两个班级再对比。'); return; }
    try { setCompare(await apiGet<Row>(`/api/teacher/analysis/compare?batch_id=${encodeURIComponent(batchId)}&class_ids=${encodeURIComponent(selectedCompare.join(','))}`)); }
    catch (error) { setMessage(String(error)); }
  };
  const toggleCompare = (id: string) => setSelectedCompare(selectedCompare.includes(id) ? selectedCompare.filter(item => item !== id) : [...selectedCompare, id]);
  const openClass = async (id: string) => {
    setBusy(true); setStudent(null);
    try {
      setKlass(await apiGet<Row>(`/api/teacher/analysis/classes/${encodeURIComponent(id)}?batch_id=${encodeURIComponent(batchId)}`));
      setClassId(id);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const openTask = async (questionId: string) => {
    try { setQuestionDetail(await apiGet<Row>(`/api/teacher/analysis/classes/${encodeURIComponent(classId)}/questions/${encodeURIComponent(questionId)}?batch_id=${encodeURIComponent(batchId)}`)); setLongitudinal(await apiGet<Row>(`/api/teacher/analysis/classes/${encodeURIComponent(classId)}/questions/${encodeURIComponent(questionId)}/longitudinal?batch_id=${encodeURIComponent(batchId)}`).catch(() => null)); }
    catch (error) { setMessage(String(error)); }
  };
  const openStudent = async (id: string) => {
    setBusy(true);
    try { setStudent(await apiGet<Row>(`/api/teacher/analysis/students/${encodeURIComponent(id)}?batch_id=${encodeURIComponent(batchId)}`)); }
    catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  };
  const currentBatch = batches.find(item => item.id === batchId);
  const compareRows = compare?.rows || [];
  const compareClasses = compare?.classes || [];

  return <section className="analytics-section">
    <header className="analytics-head">
      <div><h1>班级学情分析</h1><p>以评分点为证据、知识点为组织单位。没有证据的知识点不会被当作 0 分。</p></div>
      <select value={batchId} onChange={event => setBatchId(event.target.value)}>{batches.map(item => <option key={item.id} value={item.id}>{item.filename}</option>)}</select>
    </header>
    {!batchId && <div className="analytics-empty">还没有可分析任务。请先导入任务数据。</div>}
    {batchId && !klass && <div className="analytics-compare-picker" style={{ gap: 8 }}>
      <button className={panel === 'classes' ? 'on' : ''} onClick={() => setPanel('classes')}>班级学情</button>
      <button className={panel === 'mapping' ? 'on' : ''} onClick={() => setPanel('mapping')}>评分点映射</button>
      <button className={panel === 'graph' ? 'on' : ''} onClick={() => setPanel('graph')}>知识依赖</button>
      <button className={panel === 'errors' ? 'on' : ''} onClick={() => setPanel('errors')}>错误类型</button>
    </div>}
    {batchId && !klass && panel === 'mapping' && <MappingPanel batchId={batchId} setMessage={setMessage} />}
    {batchId && !klass && panel === 'graph' && <GraphPanel batchId={batchId} setMessage={setMessage} />}
    {batchId && !klass && panel === 'errors' && <ErrorLabelsPanel batchId={batchId} setMessage={setMessage} />}
    {batchId && !klass && panel === 'classes' && <div className="analytics-compare-picker"><strong>选择班级进行对比</strong>{classes.map((item: Row) => <label key={item.id}><input type="checkbox" checked={selectedCompare.includes(item.id)} onChange={() => toggleCompare(item.id)} /> {item.code}</label>)}<button onClick={() => void loadCompare()}>对比所选班级</button></div>}
    {batchId && !klass && panel === 'classes' && <div className="analytics-classes">{classes.map(item => <button className="analytics-class" key={item.id} onClick={() => void openClass(item.id)}><strong>{item.code}</strong><span>进入班级学情 →</span></button>)}</div>}
    {batchId && !klass && panel === 'classes' && compareRows.length > 0 && <div className="analytics-compare"><h2>班级对照（同一任务、同一算法参数）</h2><table><thead><tr><th>知识点</th>{compareClasses.map((cls: Row) => <th key={cls.id}>{cls.code} 均值 / 有效人数</th>)}<th>差值</th></tr></thead><tbody>{compareRows.map((item: Row) => <tr key={item.knowledge_point_id}><td>{item.name}</td>{item.values?.map((value: Row) => <td key={value.class_id}>{value.mean == null ? '证据不足' : `${Math.round(value.mean * 100)}%`} / {value.valid_students}</td>)}<td>{item.difference == null ? '—' : `${Math.round(item.difference * 100)}pp`}</td></tr>)}</tbody></table><p>样本不足的知识点不做显著性解释，差值只用于教学观察。</p></div>}
    {klass && <>
      <div className="analytics-crumbs"><button onClick={() => { setKlass(null); setStudent(null); }}>班级学情</button> / <strong>{klass.class.code}</strong>{student && <> / <strong>{student.student.code}</strong></>}</div>
      <div className="analytics-summary">
        <div><b>{klass.student_count}</b><span>班级人数</span></div><div><b>{klass.valid_student_count}</b><span>有有效估计</span></div><div><b>{klass.knowledge_points?.length || 0}</b><span>可分析知识点</span></div><div><b>{klass.overall_mastery == null ? '—' : `${Math.round(klass.overall_mastery * 100)}%`}</b><span>班级整体掌握</span></div><div><b>{klass.weak_student_count || 0}</b><span>薄弱学生</span></div><div><b>{klass.insufficient_student_count || 0}</b><span>证据不足</span></div>
      </div>
      {!student && <>
        <div className="analytics-title"><h2>知识点掌握分布</h2><p>仅统计证据达到阈值的有效学生；有效人数不足的知识点标为证据不足。</p></div>
        <div className="analytics-kps">{klass.knowledge_points?.map((item: Row) => <div className="analytics-kp" key={item.knowledge_point_id}><div className="analytics-kp-head"><strong>{item.name}</strong><span>{Math.round(item.mean_mastery * 100)}%</span></div><div className="analytics-bar"><i style={{ width: `${item.mean_mastery * 100}%` }} /></div><small>有效 {item.valid_students}/{item.total_students} 人 · 覆盖率 {Math.round(item.coverage * 100)}% · 标准差 {item.std_dev.toFixed(3)}</small></div>)}</div>
        <div className="analytics-title"><h2>任务分析</h2><p>当前批次、当前班级的真实题目作答与判题结果；未判题不按 0 分。</p></div>
        {!questionDetail && <div className="analytics-compare"><table><thead><tr><th>任务</th><th>题型</th><th>满分</th><th>作答</th><th>有效判题</th><th>待复核</th><th>失败</th><th>平均分</th><th>得分率</th></tr></thead><tbody>{klass.questions?.map((item: Row) => <tr key={item.question_id} onClick={() => void openTask(item.question_id)}><td>{item.question_id}</td><td>{item.question_type}</td><td>{item.max_score}</td><td>{item.answered_students}</td><td>{item.valid_judgement_students}</td><td>{item.review_students}</td><td>{item.failed_students}</td><td>{item.average_score ?? '—'}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td></tr>)}</tbody></table></div>}
        {questionDetail && <TaskDetail detail={questionDetail} longitudinal={longitudinal} onBack={() => { setQuestionDetail(null); setLongitudinal(null); }} onStudent={(id) => void openStudent(id)} />}
        <div className="analytics-title"><h2>测验趋势</h2><p>按测验节点真实日期排序，不把同一次测验的多道题当成多个时间点。</p></div>
        {klass.trends?.length > 1 ? <div className="analytics-compare"><table><thead><tr><th>测验</th><th>日期</th><th>得分率</th><th>EWMA</th><th>有效学生</th><th>可比性</th><th>原因</th></tr></thead><tbody>{klass.trends.map((item: Row) => <tr key={item.exam_node_id}><td>{item.exam_node_id}</td><td>{item.exam_date}</td><td>{item.score_rate == null ? '证据不足' : `${item.score_rate}%`}</td><td>{item.ewma_score_rate == null ? '—' : `${item.ewma_score_rate}%`}</td><td>{item.valid_judgement_students ?? item.valid_students ?? '—'}</td><td>{item.comparable_status || '—'}</td><td>{item.comparable_reason || '—'}</td></tr>)}</tbody></table><p>EWMA 仅用于观察趋势，原始得分率保留；不可比的时间点不会被强行连线。</p></div> : <div className="analytics-empty">暂无趋势数据（有效测验节点不足）。</div>}
        <div className="analytics-title"><h2>共性错误</h2></div>
        <div className="analytics-compare"><table><thead><tr><th>错误类型</th><th>加权次数</th><th>知识点</th></tr></thead><tbody>{klass.error_types?.map((item: Row) => <tr key={item.type}><td>{item.type}</td><td>{item.weighted_count}</td><td>{item.knowledge_points?.join('、') || '—'}</td></tr>)}</tbody></table></div>
        <div className="analytics-title"><h2>学生概览</h2><p>点击学生查看评分点证据与错误类型。</p></div>
        <div className="analytics-students">{klass.students?.map((profile: Row) => {
          const points = profile.mastery || [];
          const valid = points.filter((item: Row) => item.status !== '证据不足');
          const avg = valid.length ? valid.reduce((sum: number, item: Row) => sum + item.mastery_estimate, 0) / valid.length : null;
          const weak = profile.weak_points?.[0]?.knowledge_point_name || '证据不足';
          return <button className="analytics-student" key={profile.student.id} onClick={() => void openStudent(profile.student.id)}><div><strong>{profile.student.code}</strong><span>{profile.coverage?.evidence_coverage != null ? `证据覆盖 ${Math.round(profile.coverage.evidence_coverage * 100)}%` : '覆盖未知'}</span></div><div className="analytics-student-score"><b>{avg == null ? '—' : `${Math.round(avg * 100)}%`}</b><small>整体掌握</small></div><div className="analytics-student-weak">主要薄弱：{weak}</div><span className="student-arrow">→</span></button>;
        })}</div>
      </>}
      {student && <StudentAnalysis profile={student} batchId={batchId} />}
    </>}
  </section>;
}

function TaskDetail({ detail, longitudinal, onBack, onStudent }: { detail: Row; longitudinal: Row | null; onBack: () => void; onStudent: (id: string) => void }) {
  const q = detail.question; const o = detail.overall; const lonPoints = longitudinal?.points || [];
  const [confirmGrade, setConfirmGrade] = useState('');
  return <div className="task-detail">
    <div className="batch-head"><div><h1>{q.question_id}</h1><p>{q.question_type} · 满分 {q.max_score} · 当前批次单题任务</p></div><button onClick={onBack}>返回任务列表</button></div>
    <div className="analytics-summary"><div><b>{o.answered_students}</b><span>作答学生</span></div><div><b>{o.valid_judgement_students}</b><span>有效判题学生</span></div><div><b>{o.answered_events}</b><span>独立作答事件</span></div><div><b>{o.student_average_score ?? '—'}</b><span>学生等权平均</span></div><div><b>{o.review_students}</b><span>待复核</span></div><div><b>{o.failed_students}</b><span>判题失败</span></div><div><b>{o.average_score ?? '—'}</b><span>平均得分</span></div><div><b>{o.score_rate == null ? '—' : `${o.score_rate}%`}</b><span>得分率</span></div></div>
    {(o.manual_correction_without_scoring_sync || 0) > 0 && <div className="analytics-empty">有 {o.manual_correction_without_scoring_sync} 条人工修正只改了整题总分，旧评分点明细未同步，评分点统计已排除。</div>}
    <div className="analytics-compare"><h2>题目原文</h2><p className="long-text">{q.stem}</p><h2>参考答案</h2><p className="long-text">{q.reference_answer}</p><h2>参考解析</h2><p className="long-text">{q.reference_solution}</p></div>
    <div className="analytics-title"><h2>得分分布</h2></div><div className="analytics-compare"><table><thead><tr><th>0–59%</th><th>60–79%</th><th>80–100%</th></tr></thead><tbody><tr><td>{detail.score_distribution?.['0-59']}</td><td>{detail.score_distribution?.['60-79']}</td><td>{detail.score_distribution?.['80-100']}</td></tr></tbody></table></div>
    <div className="analytics-title"><h2>评分点分析</h2><p>平均得分提供“学生等权平均”和“事件平均”两种口径；累计得分不是平均分。</p></div><div className="analytics-compare"><table><thead><tr><th>评分点</th><th>单人满分</th><th>累计得分</th><th>学生等权平均</th><th>事件平均</th><th>有效事件</th><th>有效学生</th><th>未映射</th><th>异常分</th><th>缺评分点</th></tr></thead><tbody>{detail.scoring_points?.map((item: Row) => <tr key={item.scoring_point_id}><td>{item.scoring_point_id}</td><td>{item.full_score_consistent ? item.full_score : '满分不一致'}</td><td>{item.total_earned_score}</td><td>{item.student_average_score ?? '—'}</td><td>{item.event_average_score ?? '—'}</td><td>{item.valid_events}</td><td>{item.valid_students}</td><td>{item.unmapped_knowledge_count}</td><td>{item.invalid_score_count}</td><td>{item.missing_scoring_point_result_count}</td></tr>)}</tbody></table></div>
    <div className="analytics-title"><h2>学生表现</h2><p>同一 event 只取最新 revision；点击学生进入个人学情。</p></div><div className="analytics-compare"><table><thead><tr><th>学生</th><th>状态</th><th>得分</th><th>得分率</th><th>错误</th><th>理由</th><th>操作</th></tr></thead><tbody>{detail.students?.map((item: Row) => <tr key={item.answer_id} onClick={() => onStudent(item.student_id)}><td>{item.student_code}</td><td>{statusLabel(item.status)}</td><td>{item.score ?? '—'}/{item.max_score}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td><td>{item.error_type}</td><td>{item.judgment_reason || '—'}</td><td>{item.grade_id && <button onClick={(event) => { event.stopPropagation(); setConfirmGrade(item.grade_id); }}>评分点确认</button>}</td></tr>)}</tbody></table></div>
    {confirmGrade && <ScoringPointConfirm gradeId={confirmGrade} onClose={() => setConfirmGrade('')} />}
    <div className="analytics-title"><h2>纵向数据</h2><p>{longitudinal?.status || 'unavailable'} · {longitudinal?.reason || '暂无纵向数据'}</p></div>
    {lonPoints.length > 0 && <div className="analytics-compare"><table><thead><tr><th>时间点</th><th>日期</th><th>作答学生</th><th>有效判题</th><th>得分率</th></tr></thead><tbody>{lonPoints.map((item: Row) => <tr key={item.exam_node_id}><td>{item.exam_node_id}</td><td>{item.exam_date}</td><td>{item.answered_students}</td><td>{item.valid_judgement_students}</td><td>{item.score_rate == null ? '—' : `${item.score_rate}%`}</td></tr>)}</tbody></table></div>}
  </div>;
}

function ScoringPointConfirm({ gradeId, onClose }: { gradeId: string; onClose: () => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState('');
  useEffect(() => { apiGet<{ items: Row[] }>(`/api/teacher/grades/${encodeURIComponent(gradeId)}/scoring-points`).then(data => setRows(data.items || [])).catch(err => setError(String(err))); }, [gradeId]);
  const setScore = (id: string, value: string) => setRows(rows.map(row => row.scoring_point_id === id ? { ...row, editScore: value } : row));
  const save = async () => {
    try {
      const items = rows.map(row => ({ scoring_point_id: row.scoring_point_id, score: Number(row.editScore ?? row.confirmation?.confirmed_score ?? row.original_ai_score ?? 0), note: '教师逐评分点确认' }));
      await apiPost(`/api/teacher/grades/${encodeURIComponent(gradeId)}/scoring-points`, { items });
      onClose();
    } catch (err) { setError(String(err)); }
  };
  return <div className="analytics-compare"><h2>逐评分点确认</h2>{error && <p className="analytics-empty">{error}</p>}<table><thead><tr><th>评分点</th><th>满分</th><th>AI 原分</th><th>确认分</th></tr></thead><tbody>{rows.map((row: Row) => <tr key={row.scoring_point_id}><td>{row.scoring_point_id}</td><td>{row.max_score}</td><td>{row.original_ai_score}</td><td><input value={row.editScore ?? row.confirmation?.confirmed_score ?? row.original_ai_score ?? 0} onChange={event => setScore(row.scoring_point_id, event.target.value)} /></td></tr>)}</tbody></table><button onClick={() => void save()}>保存评分点确认</button><button onClick={onClose}>取消</button></div>;
}

function StudentAnalysis({ profile, batchId }: { profile: Row; batchId: string }) {
  const [openKp, setOpenKp] = useState<string>('');
  const [detailKp, setDetailKp] = useState<string>('');
  return <div className="student-analysis">
    <div className="analytics-title"><h2>{profile.student.code} 个人学情</h2><p>证据范围：{profile.task_id}；算法 {profile.algorithm_version}</p></div>
    <div className="analytics-summary"><div><b>{profile.coverage?.answered_question_count}/{profile.coverage?.expected_question_count ?? '未知'}</b><span>覆盖题目</span></div><div><b>{profile.data_quality?.evidence_count || 0}</b><span>评分点证据</span></div><div><b>{profile.data_quality?.no_valid_grade || 0}</b><span>无有效判题</span></div><div><b>{profile.data_quality?.unmapped || 0}</b><span>未映射评分点</span></div></div>
    <div className="analytics-kps">{profile.mastery?.map((item: Row) => <button className={`analytics-kp ${openKp === item.knowledge_point_id ? 'selected' : ''}`} key={item.knowledge_point_id} onClick={() => setOpenKp(openKp === item.knowledge_point_id ? '' : item.knowledge_point_id)}>
      <div className="analytics-kp-head"><strong>{item.knowledge_point_name}</strong><span>{Math.round(item.mastery_estimate * 100)}%</span></div>
      <div className="analytics-bar"><i style={{ width: `${item.mastery_estimate * 100}%` }} /></div>
      <small>{item.status} · 区间 {Math.round(item.credible_interval_lower * 100)}–{Math.round(item.credible_interval_upper * 100)}% · 证据 {item.evidence_count} · 覆盖 {item.covered_question_count}/{item.opportunity_question_count}</small>
      {openKp === item.knowledge_point_id && <div className="analytics-evidence">{profile.evidence?.filter((e: Row) => e.knowledge_point_id === item.knowledge_point_id).map((e: Row) => <div key={`${e.answer_id}-${e.scoring_point_id}`}><strong>{e.question_id} · {e.scoring_point_id} · {e.earned_score}/{e.full_score}</strong><p>{e.evidence_text}</p><small>{e.error_type} · {e.judgment_source} · {e.error_reason || '无理由'}</small></div>)}
        <button onClick={(event) => { event.stopPropagation(); setDetailKp(item.knowledge_point_id); }}>查看完整证据链</button>
      </div>}
    </button>)}</div>
    {detailKp && profile.student?.id && <StudentKnowledgeDetail studentId={profile.student.id} batchId={batchId || profile.task_id || ''} knowledgePointId={detailKp} />}
    {(profile.methods || []).length > 0 && <><div className="analytics-title"><h2>解题方法</h2><p>方法与知识点分开统计，避免把策略问题混进概念掌握。</p></div>
      <div className="analytics-compare"><table><thead><tr><th>方法</th><th>状态</th><th>估计</th><th>证据</th><th>覆盖题目</th></tr></thead><tbody>{profile.methods.map((method: Row) => <tr key={method.method_id}><td>{method.method_name}</td><td>{method.status}</td><td>{Math.round(method.mastery_estimate * 100)}%</td><td>{method.evidence_count}</td><td>{method.covered_question_count}</td></tr>)}</tbody></table></div></>}
    <div className="analytics-title"><h2>学习建议</h2></div><ul className="analytics-suggestions">{profile.suggestions?.map((text: string, index: number) => <li key={index}>{text}</li>)}</ul>
    {profile.unknowns?.length > 0 && <p className="analytics-empty">{profile.unknowns.join('；')}</p>}
  </div>;
}
