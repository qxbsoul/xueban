import { useEffect, useState } from 'react';
import { apiGet } from '../api';
import './learning-analytics.css';

type Row = Record<string, any>;

export function StudentAnalytics() {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState('');
  useEffect(() => { apiGet<Row>('/api/teacher/analysis/me').then(setData).catch(err => setError(String(err))); }, []);
  if (error) return <div className="analytics-empty">学情分析暂时不可用：{error}</div>;
  if (!data) return <div className="analytics-empty">正在计算学情分析…</div>;
  if (data.error) return <div className="analytics-empty">{data.error}</div>;
  return <div className="analytics-section">
    <header className="analytics-head"><div><h1>我的学情</h1><p>掌握程度按评分点证据计算；没有证据时不会显示为 0 分。</p></div></header>
    <div className="analytics-summary"><div><b>{data.coverage?.covered_questions}/{data.coverage?.total_questions}</b><span>已覆盖题目</span></div><div><b>{data.data_quality?.evidence_count || 0}</b><span>评分点证据</span></div><div><b>{data.mastery?.length || 0}</b><span>可分析知识点</span></div><div><b>{data.data_quality?.unmapped || 0}</b><span>未映射证据</span></div></div>
    {data.unknowns?.length > 0 && <div className="analytics-empty">{data.unknowns.join('；')}</div>}
    <div className="analytics-kps">{data.mastery?.map((item: Row) => <button className={`analytics-kp ${open === item.knowledge_point_id ? 'selected' : ''}`} key={item.knowledge_point_id} onClick={() => setOpen(open === item.knowledge_point_id ? '' : item.knowledge_point_id)}>
      <div className="analytics-kp-head"><strong>{item.knowledge_point_name}</strong><span>{Math.round(item.mastery_estimate * 100)}%</span></div>
      <div className="analytics-bar"><i style={{ width: `${item.mastery_estimate * 100}%` }} /></div>
      <small>{item.status} · 可信区间 {Math.round(item.credible_interval_lower * 100)}–{Math.round(item.credible_interval_upper * 100)}% · 证据 {item.evidence_count} 条 · 覆盖题目 {item.covered_question_count}</small>
      {open === item.knowledge_point_id && <div className="analytics-evidence">{data.evidence?.filter((e: Row) => e.knowledge_point_id === item.knowledge_point_id).map((e: Row) => <div key={`${e.answer_id}-${e.scoring_point_id}`}><strong>{e.question_id} · {e.scoring_point_id} · {e.earned_score}/{e.full_score}</strong><p>{e.evidence_text || '无原始作答片段'}</p><small>{e.error_type} · {e.judgment_reason || '无判题理由'}</small></div>)}</div>}
    </button>)}</div>
    <div className="analytics-title"><h2>学习建议</h2></div><ul className="analytics-suggestions">{data.suggestions?.map((item: string, index: number) => <li key={index}>{item}</li>)}</ul>
  </div>;
}
