import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api';
import { RecommendationsPanel } from './AnalyticsExtras';
import { useWorkbench } from '../store';
import './learning-analytics.css';

type Row = Record<string, any>;

export function StudentAnalytics() {
  const store = useWorkbench();
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState('');
  const [trend, setTrend] = useState<Row | null>(null);
  const [user, setUser] = useState<Row | null>(null);
  const [passwords, setPasswords] = useState({ current: '', next: '' });
  const [passwordMessage, setPasswordMessage] = useState('');

  useEffect(() => {
    apiGet<Row>('/api/teacher/analysis/me').then(setData).catch(err => setError(String(err)));
    apiGet<Row>('/api/xueban/auth/me').then(result => setUser(result.user as Row)).catch(() => null);
  }, []);

  useEffect(() => {
    const studentId = data?.student?.id;
    if (!studentId) return;
    apiGet<Row>(`/api/teacher/analysis/trend/student/${encodeURIComponent(studentId)}`).then(setTrend).catch(() => setTrend(null));
  }, [data?.student?.id]);

  const changePassword = async () => {
    try {
      await apiPost('/api/xueban/auth/password', { current_password: passwords.current, new_password: passwords.next });
      setPasswordMessage('密码已修改，请用新密码重新登录。');
      setPasswords({ current: '', next: '' });
      window.setTimeout(() => window.location.reload(), 1200);
    } catch (err) { setPasswordMessage(String(err)); }
  };

  if (error) return <div className="analytics-empty">学情分析暂时不可用：{error}</div>;
  if (!data) return <div className="analytics-empty">正在计算学情分析…</div>;
  if (data.error) return <div className="analytics-empty">{data.error}</div>;
  return <div className="analytics-section">
    <header className="analytics-head"><div><h1>我的学情</h1><p>掌握程度按评分点证据计算；没有证据时不会显示为 0 分。</p></div></header>
    {user?.must_change_password && <div className="analytics-compare" style={{ borderColor: '#f59e0b' }}>
      <h2>首次登录请修改密码</h2>
      <p>初始密码由老师发放；修改前请勿关闭本页。</p>
      <input type="password" placeholder="当前密码" value={passwords.current} onChange={event => setPasswords({ ...passwords, current: event.target.value })} />
      <input type="password" placeholder="新密码（至少 8 位）" value={passwords.next} onChange={event => setPasswords({ ...passwords, next: event.target.value })} />
      <button onClick={() => void changePassword()}>修改密码</button>
      {passwordMessage && <p>{passwordMessage}</p>}
    </div>}
    <div className="analytics-summary"><div><b>{data.coverage?.answered_question_count}/{data.coverage?.expected_question_count ?? '未知'}</b><span>已作答 / 应完成</span></div><div><b>{data.data_quality?.evidence_count || 0}</b><span>评分点证据</span></div><div><b>{data.mastery?.length || 0}</b><span>可分析知识点</span></div><div><b>{data.data_quality?.unmapped || 0}</b><span>未映射证据</span></div></div>
    {data.unknowns?.length > 0 && <div className="analytics-empty">{data.unknowns.join('；')}</div>}
    <div className="analytics-kps">{data.mastery?.map((item: Row) => <button className={`analytics-kp ${open === item.knowledge_point_id ? 'selected' : ''}`} key={item.knowledge_point_id} onClick={() => setOpen(open === item.knowledge_point_id ? '' : item.knowledge_point_id)}>
      <div className="analytics-kp-head"><strong>{item.knowledge_point_name}</strong><span>{Math.round(item.mastery_estimate * 100)}%</span></div>
      <div className="analytics-bar"><i style={{ width: `${item.mastery_estimate * 100}%` }} /></div>
      <small>{item.status} · 可信区间 {Math.round(item.credible_interval_lower * 100)}–{Math.round(item.credible_interval_upper * 100)}% · 证据 {item.evidence_count} 条 · 覆盖题目 {item.covered_question_count}/{item.opportunity_question_count}</small>
      {open === item.knowledge_point_id && <div className="analytics-evidence">{data.evidence?.filter((e: Row) => e.knowledge_point_id === item.knowledge_point_id).map((e: Row) => <div key={`${e.answer_id}-${e.scoring_point_id}`}><strong>{e.question_id} · {e.scoring_point_id} · {e.earned_score}/{e.full_score}</strong><p>{e.evidence_text || '无原始作答片段'}</p><small>{e.error_type} · {e.judgment_source} · {e.error_reason || '无判题理由'}</small></div>)}</div>}
    </button>)}</div>
    {(data.methods || []).length > 0 && <><div className="analytics-title"><h2>解题方法</h2><p>方法与知识点分开统计。</p></div>
      <div className="analytics-compare"><table><thead><tr><th>方法</th><th>状态</th><th>估计</th><th>证据</th><th>机会</th><th>覆盖率</th></tr></thead><tbody>{data.methods.map((method: Row) => <tr key={method.method_id}><td>{method.method_name}</td><td>{method.status}</td><td>{method.mastery_estimate == null ? '—' : `${Math.round(method.mastery_estimate * 100)}%`}</td><td>{method.evidence_count}</td><td>{method.opportunity_question_count || 0}</td><td>{method.data_coverage == null ? '暂不可计算' : `${Math.round(method.data_coverage * 100)}%`}</td></tr>)}</tbody></table><p>方法覆盖率只统计独立机会数据；缺失时不会显示为 100%。</p></div></>}
    {trend && (trend.knowledge_points || []).length > 0 && <><div className="analytics-title"><h2>学习趋势</h2><p>证据不足的时间点不参与 EWMA，也不会被补成 0。</p></div>
      <div className="analytics-compare"><table><thead><tr><th>知识点</th>{trend.timeline.map((point: Row) => <th key={point.batch_id}>{point.label}</th>)}</tr></thead><tbody>
        {trend.knowledge_points.map((point: Row) => <tr key={point.knowledge_point_id}><td>{point.name}</td>{point.points.map((value: Row) => <td key={value.batch_id}>{value.status === '证据不足' ? '证据不足' : `${Math.round((value.ewma_mastery ?? value.mastery_estimate) * 100)}%`}</td>)}</tr>)}
      </tbody></table></div></>}
    {data.student?.id && <RecommendationsPanel studentId={data.student.id} batchId={data.task_id} />}
    <div className="analytics-title"><h2>学习建议</h2></div><ul className="analytics-suggestions">{data.suggestions?.map((item: string, index: number) => <li key={index}>{item}</li>)}</ul>
  </div>;
}
