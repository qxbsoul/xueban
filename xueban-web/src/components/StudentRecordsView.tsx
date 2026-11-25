import { useEffect, useState } from 'react';
import { apiGet } from '../api';

type Row = Record<string, any>;

export default function StudentRecordsView() {
  const [items, setItems] = useState<Row[]>([]);
  const [detail, setDetail] = useState<Row | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    apiGet<{ items: Row[] }>('/api/teacher/answers?limit=100').then(data => setItems(data.items || [])).catch(err => setError(String(err)));
  }, []);
  return <div className="teacher-section"><div className="teacher-panel"><h2>我的判题记录</h2><p className="teacher-muted">只能查看自己的原始作答和判题结果。</p>{error && <p className="teacher-error">{error}</p>}
    {items.map(item => <button className="teacher-answer-row" key={item.id} onClick={async () => setDetail(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(item.id)}`))}><div><strong>{item.question_id}</strong><span>{item.event_id} · 版本 {item.revision}</span></div><small>{item.score_status}</small></button>)}
    {detail && <div className="teacher-answer-box"><h3>{detail.question?.stem}</h3><p>{detail.answer.original_answer || '未作答'}</p><pre>{detail.answer.solution_process || ''}</pre>{detail.grades?.map((grade: Row) => <div className="teacher-grade" key={grade.id}><strong>{grade.score}/{grade.max_score}</strong><span>{grade.is_correct} · {grade.grading_model}</span><p>{grade.feedback}</p></div>)}</div>}
  </div></div>;
}
