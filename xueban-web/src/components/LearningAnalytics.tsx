import { useEffect, useState } from 'react';
import { apiGet } from '../api';
import './learning-analytics.css';

type Row = Record<string, any>;

export function LearningAnalytics({ setMessage }: { setMessage: (value: string) => void }) {
  const [batches, setBatches] = useState<Row[]>([]);
  const [batchId, setBatchId] = useState('');
  const [classes, setClasses] = useState<Row[]>([]);
  const [classId, setClassId] = useState('');
  const [klass, setKlass] = useState<Row | null>(null);
  const [student, setStudent] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [compare, setCompare] = useState<Row | null>(null);

  useEffect(() => {
    apiGet<{ items: Row[] }>('/api/teacher/batches').then(data => {
      setBatches(data.items || []);
      if (data.items?.length) setBatchId(data.items[0].id);
    }).catch(error => setMessage(String(error)));
  }, []);
  useEffect(() => {
    if (!batchId) return;
    apiGet<{ classes: Row[] }>(`/api/teacher/analysis/classes?batch_id=${encodeURIComponent(batchId)}`).then(data => {
      setClasses(data.classes || []); setKlass(null); setStudent(null); setClassId('');
      apiGet<Row>(`/api/teacher/analysis/compare?batch_id=${encodeURIComponent(batchId)}`).then(setCompare).catch(() => setCompare(null));
    }).catch(error => setMessage(String(error)));
  }, [batchId]);
  const openClass = async (id: string) => {
    setBusy(true); setStudent(null);
    try {
      setKlass(await apiGet<Row>(`/api/teacher/analysis/classes/${encodeURIComponent(id)}?batch_id=${encodeURIComponent(batchId)}`));
      setClassId(id);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
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
    {batchId && !klass && <div className="analytics-classes">{classes.map(item => <button className="analytics-class" key={item.id} onClick={() => void openClass(item.id)}><strong>{item.code}</strong><span>进入班级学情 →</span></button>)}</div>}
    {batchId && !klass && compareRows.length > 0 && <div className="analytics-compare"><h2>班级对照（同一任务、同一算法参数）</h2><table><thead><tr><th>知识点</th><th>{compareClasses[0]?.code} 均值</th><th>{compareClasses[1]?.code} 均值</th><th>差值</th><th>有效人数</th></tr></thead><tbody>{compareRows.map((item: Row) => <tr key={item.knowledge_point_id}><td>{item.name}</td><td>{item.class_a_mean == null ? '证据不足' : `${Math.round(item.class_a_mean * 100)}%`}</td><td>{item.class_b_mean == null ? '证据不足' : `${Math.round(item.class_b_mean * 100)}%`}</td><td>{item.difference == null ? '—' : `${Math.round(item.difference * 100)}pp`}</td><td>{item.class_a_valid}/{item.class_b_valid}</td></tr>)}</tbody></table><p>样本不足的知识点不做显著性解释，差值只用于教学观察。</p></div>}
    {klass && <>
      <div className="analytics-crumbs"><button onClick={() => { setKlass(null); setStudent(null); }}>班级学情</button> / <strong>{klass.class.code}</strong>{student && <> / <strong>{student.student.code}</strong></>}</div>
      <div className="analytics-summary">
        <div><b>{klass.student_count}</b><span>班级人数</span></div><div><b>{klass.valid_student_count}</b><span>有有效估计</span></div><div><b>{klass.knowledge_points?.length || 0}</b><span>可分析知识点</span></div><div><b>{currentBatch?.filename || ''}</b><span>数据任务</span></div>
      </div>
      {!student && <>
        <div className="analytics-title"><h2>知识点掌握分布</h2><p>仅统计证据达到阈值的有效学生；有效人数不足的知识点标为证据不足。</p></div>
        <div className="analytics-kps">{klass.knowledge_points?.map((item: Row) => <div className="analytics-kp" key={item.knowledge_point_id}><div className="analytics-kp-head"><strong>{item.name}</strong><span>{Math.round(item.mean_mastery * 100)}%</span></div><div className="analytics-bar"><i style={{ width: `${item.mean_mastery * 100}%` }} /></div><small>有效 {item.valid_students}/{item.total_students} 人 · 覆盖率 {Math.round(item.coverage * 100)}% · 标准差 {item.std_dev.toFixed(3)}</small></div>)}</div>
        <div className="analytics-title"><h2>学生概览</h2><p>点击学生查看评分点证据与错误类型。</p></div>
        <div className="analytics-students">{klass.students?.map((profile: Row) => {
          const points = profile.mastery || [];
          const valid = points.filter((item: Row) => item.status !== '证据不足');
          const avg = valid.length ? valid.reduce((sum: number, item: Row) => sum + item.mastery_estimate, 0) / valid.length : null;
          const weak = profile.weak_points?.[0]?.knowledge_point_name || '证据不足';
          return <button className="analytics-student" key={profile.student.id} onClick={() => void openStudent(profile.student.id)}><div><strong>{profile.student.code}</strong><span>{profile.coverage?.data_coverage != null ? `覆盖 ${Math.round(profile.coverage.data_coverage * 100)}%` : '覆盖未知'}</span></div><div className="analytics-student-score"><b>{avg == null ? '—' : `${Math.round(avg * 100)}%`}</b><small>整体掌握</small></div><div className="analytics-student-weak">主要薄弱：{weak}</div><span className="student-arrow">→</span></button>;
        })}</div>
      </>}
      {student && <StudentAnalysis profile={student} />}
    </>}
  </section>;
}

function StudentAnalysis({ profile }: { profile: Row }) {
  const [openKp, setOpenKp] = useState<string>('');
  return <div className="student-analysis">
    <div className="analytics-title"><h2>{profile.student.code} 个人学情</h2><p>证据范围：{profile.task_id}；算法 {profile.algorithm_version}</p></div>
    <div className="analytics-summary"><div><b>{profile.coverage?.covered_questions}/{profile.coverage?.total_questions}</b><span>覆盖题目</span></div><div><b>{profile.data_quality?.evidence || 0}</b><span>评分点证据</span></div><div><b>{profile.data_quality?.no_grade || 0}</b><span>无有效判题</span></div><div><b>{profile.data_quality?.unmapped || 0}</b><span>未映射评分点</span></div></div>
    <div className="analytics-kps">{profile.mastery?.map((item: Row) => <button className={`analytics-kp ${openKp === item.knowledge_point_id ? 'selected' : ''}`} key={item.knowledge_point_id} onClick={() => setOpenKp(openKp === item.knowledge_point_id ? '' : item.knowledge_point_id)}>
      <div className="analytics-kp-head"><strong>{item.knowledge_point_name}</strong><span>{Math.round(item.mastery_estimate * 100)}%</span></div>
      <div className="analytics-bar"><i style={{ width: `${item.mastery_estimate * 100}%` }} /></div>
      <small>{item.status} · 区间 {Math.round(item.credible_interval_lower * 100)}–{Math.round(item.credible_interval_upper * 100)}% · 证据 {item.evidence_count}</small>
      {openKp === item.knowledge_point_id && <div className="analytics-evidence">{profile.evidence?.filter((e: Row) => e.knowledge_point_id === item.knowledge_point_id).map((e: Row) => <div key={`${e.answer_id}-${e.scoring_point_id}`}><strong>{e.question_id} · {e.scoring_point_id} · {e.earned_score}/{e.full_score}</strong><p>{e.evidence_text}</p><small>{e.error_type} · {e.judgment_source} · {e.judgment_reason || '无理由'}</small></div>)}</div>}
    </button>)}</div>
    <div className="analytics-title"><h2>学习建议</h2></div><ul className="analytics-suggestions">{profile.suggestions?.map((text: string, index: number) => <li key={index}>{text}</li>)}</ul>
    {profile.unknowns?.length > 0 && <p className="analytics-empty">{profile.unknowns.join('；')}</p>}
  </div>;
}
