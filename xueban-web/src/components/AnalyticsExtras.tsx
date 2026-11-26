import { useEffect, useMemo, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../api';

type Row = Record<string, any>;

const box: React.CSSProperties = { border: '1px solid #e2e8f0', borderRadius: 10, padding: 14, marginTop: 12, background: '#fff' };
const actionButton: React.CSSProperties = { padding: '6px 10px', border: '1px solid #cbd5e1', background: '#fff', borderRadius: 6, cursor: 'pointer' };
const btn: React.CSSProperties = { ...actionButton, marginRight: 8, marginTop: 6 };
const muted: React.CSSProperties = { color: '#64748b', fontSize: 13 };

export function MappingPanel({ batchId, setMessage }: { batchId: string; setMessage: (value: string) => void }) {
  const [mapping, setMapping] = useState<Row | null>(null);
  const [points, setPoints] = useState<Row[]>([]);
  const [methods, setMethods] = useState<Row[]>([]);
  const [active, setActive] = useState<Row | null>(null);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [methodIds, setMethodIds] = useState<string[]>([]);
  const [methodForm, setMethodForm] = useState({ code: '', name: '', description: '' });

  const load = async () => {
    if (!batchId) return;
    try {
      const [m, k, me] = await Promise.all([
        apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batchId)}/mapping`),
        apiGet<{ items: Row[] }>('/api/teacher/knowledge-points'),
        apiGet<{ items: Row[] }>('/api/teacher/methods'),
      ]);
      setMapping(m); setPoints(k.items || []); setMethods(me.items || []);
    } catch (error) { setMessage(String(error)); }
  };
  useEffect(() => { void load(); }, [batchId]);

  const openPoint = (item: Row) => {
    setActive(item);
    setWeights(Object.fromEntries((item.knowledge_links || []).map((link: Row) => [link.knowledge_point_id, String(link.weight ?? 1)])));
    setMethodIds((item.method_links || []).map((link: Row) => link.method_id));
  };

  const saveLinks = async () => {
    if (!active) return;
    try {
      const links = Object.entries(weights).filter(([, value]) => Number(value) > 0).map(([knowledge_point_id, value]) => ({ knowledge_point_id, weight: Number(value) }));
      if (!links.length) { setMessage('至少勾选一个知识点'); return; }
      await apiPost(`/api/teacher/scoring-points/${encodeURIComponent(active.scoring_point_id)}/knowledge-links`, { links });
      setMessage(`已确认 ${active.code} 的知识点映射`);
      setActive(null); await load();
    } catch (error) { setMessage(String(error)); }
  };

  const saveMethods = async () => {
    if (!active) return;
    try {
      await apiPost(`/api/teacher/scoring-points/${encodeURIComponent(active.scoring_point_id)}/methods`, { links: methodIds.map(method_id => ({ method_id, weight: 1 })) });
      setMessage(`已保存 ${active.code} 的方法映射`);
      await load();
    } catch (error) { setMessage(String(error)); }
  };

  const createMethod = async () => {
    try {
      await apiPost('/api/teacher/methods', methodForm);
      setMethodForm({ code: '', name: '', description: '' });
      await load();
      setMessage('方法已创建');
    } catch (error) { setMessage(String(error)); }
  };

  const summary = mapping?.summary || {};
  return <section style={box}>
    <h2>评分点映射管理</h2>
    <p style={muted}>映射必须以教师确认（confirmed）为准；legacy 只是候选，不会自动变成人工确认。权重会按评分点归一化到 1。</p>
    <p>共 {summary.total || 0} 个评分点 · 已确认 {summary.confirmed || 0} · legacy {summary.legacy || 0} · 未映射 {summary.unmapped || 0}</p>
    <div style={{ maxHeight: 320, overflow: 'auto' }}>
      <table className="analytics-compare-table" style={{ width: '100%' }}>
        <thead><tr><th>评分点</th><th>状态</th><th>知识点</th><th>方法</th><th /></tr></thead>
        <tbody>{(mapping?.items || []).map((item: Row) => <tr key={item.scoring_point_id}>
          <td>{item.code}</td>
          <td>{item.mapping_status === 'confirmed' ? '已确认' : item.mapping_status === 'legacy' ? 'legacy 候选' : '未映射'}</td>
          <td>{(item.knowledge_links || []).map((link: Row) => `${link.knowledge_point_id}(${link.weight})`).join('、') || '—'}</td>
          <td>{(item.method_links || []).map((link: Row) => `${link.method_id}(${link.weight})`).join('、') || '—'}</td>
          <td><button onClick={() => openPoint(item)}>编辑</button></td>
        </tr>)}</tbody>
      </table>
    </div>
    {active && <div style={{ ...box, background: '#f8fafc' }}>
      <h3>编辑 {active.code} 的知识点映射</h3>
      {points.map(point => <label key={point.id} style={{ display: 'block', margin: '4px 0' }}>
        <input type="checkbox" checked={weights[point.id] !== undefined} onChange={event => setWeights(previous => {
          const next = { ...previous };
          if (event.target.checked) next[point.id] = next[point.id] ?? '1'; else delete next[point.id];
          return next;
        })} /> {point.code} {point.name}（{point.topic_title}）
        {weights[point.id] !== undefined && <input style={{ width: 64, marginLeft: 8 }} value={weights[point.id]} onChange={event => setWeights(previous => ({ ...previous, [point.id]: event.target.value }))} />}
      </label>)}
      <button style={btn} onClick={() => void saveLinks()}>保存并确认知识点映射</button>
      <button style={btn} onClick={() => setActive(null)}>取消</button>
      <h3 style={{ marginTop: 12 }}>解题方法（与知识点分开统计）</h3>
      {methods.length === 0 && <p style={muted}>还没有方法目录，请先在下方创建。</p>}
      {methods.map(method => <label key={method.id} style={{ display: 'block', margin: '4px 0' }}>
        <input type="checkbox" checked={methodIds.includes(method.id)} onChange={event => setMethodIds(previous => event.target.checked ? [...previous, method.id] : previous.filter(id => id !== method.id))} /> {method.code} {method.name}
      </label>)}
      <button style={btn} onClick={() => void saveMethods()}>保存方法映射</button>
      <div style={{ marginTop: 10, display: 'flex', gap: 6 }}>
        <input placeholder="方法编号 M1" value={methodForm.code} onChange={event => setMethodForm({ ...methodForm, code: event.target.value })} />
        <input placeholder="方法名称" value={methodForm.name} onChange={event => setMethodForm({ ...methodForm, name: event.target.value })} />
        <input placeholder="说明" value={methodForm.description} onChange={event => setMethodForm({ ...methodForm, description: event.target.value })} />
        <button onClick={() => void createMethod()}>新增方法</button>
      </div>
    </div>}
  </section>;
}

export function GraphPanel({ batchId, setMessage }: { batchId: string; setMessage: (value: string) => void }) {
  const [graph, setGraph] = useState<Row | null>(null);
  const [fromKp, setFromKp] = useState('');
  const [toKp, setToKp] = useState('');

  const load = async () => {
    if (!batchId) return;
    try { setGraph(await apiGet<Row>(`/api/teacher/knowledge-graph?batch_id=${encodeURIComponent(batchId)}`)); }
    catch (error) { setMessage(String(error)); }
  };
  useEffect(() => { void load(); }, [batchId]);

  const generate = async () => {
    try {
      const result = await apiPost<Row>('/api/teacher/knowledge-graph/candidates', { batch_id: batchId });
      setMessage(`已生成 ${result.created} 条候选依赖（来自主题顺序，需教师确认）`);
      await load();
    } catch (error) { setMessage(String(error)); }
  };
  const addEdge = async () => {
    try {
      await apiPost('/api/teacher/knowledge-graph/edges', { batch_id: batchId, from_kp: fromKp, to_kp: toKp });
      setFromKp(''); setToKp(''); await load(); setMessage('依赖已确认');
    } catch (error) { setMessage(String(error)); }
  };
  const removeEdge = async (id: string) => {
    try { await apiDelete(`/api/teacher/knowledge-graph/edges/${encodeURIComponent(id)}`); await load(); }
    catch (error) { setMessage(String(error)); }
  };

  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  const topics = useMemo(() => Array.from(new Set(nodes.map((node: Row) => node.topic_id || '未分组'))), [nodes]);
  const position = (node: Row) => {
    const topicIndex = topics.indexOf(node.topic_id || '未分组');
    const siblings = nodes.filter((item: Row) => (item.topic_id || '未分组') === (node.topic_id || '未分组'));
    const index = siblings.findIndex((item: Row) => item.knowledge_point_id === node.knowledge_point_id);
    return { x: 40 + topicIndex * 220, y: 30 + index * 60 };
  };

  return <section style={box}>
    <h2>知识依赖图</h2>
    <p style={muted}>候选依赖来自课程主题顺序，必须教师确认后才算正式依赖；系统会拒绝自环、重复边和循环依赖。</p>
    <p>节点 {graph?.summary?.nodes_in_batch || 0}/{graph?.summary?.nodes || 0} · 边 {graph?.summary?.edges || 0}（确认 {graph?.summary?.confirmed || 0} / 候选 {graph?.summary?.candidate || 0}）</p>
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      <button style={btn} onClick={() => void generate()}>生成候选依赖</button>
      <select value={fromKp} onChange={event => setFromKp(event.target.value)}><option value="">前置知识点</option>{nodes.map((node: Row) => <option key={node.knowledge_point_id} value={node.knowledge_point_id}>{node.name}</option>)}</select>
      <select value={toKp} onChange={event => setToKp(event.target.value)}><option value="">后继知识点</option>{nodes.map((node: Row) => <option key={node.knowledge_point_id} value={node.knowledge_point_id}>{node.name}</option>)}</select>
      <button style={btn} onClick={() => void addEdge()} disabled={!fromKp || !toKp}>确认依赖</button>
    </div>
    <svg width={Math.max(460, topics.length * 220)} height={Math.max(160, nodes.length * 20)} style={{ marginTop: 12, background: '#f8fafc', borderRadius: 8 }}>
      {edges.map((edge: Row) => {
        const from = nodes.find((node: Row) => node.knowledge_point_id === edge.from_kp);
        const to = nodes.find((node: Row) => node.knowledge_point_id === edge.to_kp);
        if (!from || !to) return null;
        const a = position(from); const b = position(to);
        return <line key={edge.id} x1={a.x + 120} y1={a.y + 14} x2={b.x} y2={b.y + 14} stroke={edge.status === 'confirmed' ? '#2563eb' : '#94a3b8'} strokeWidth={2} strokeDasharray={edge.status === 'confirmed' ? undefined : '4 4'} />;
      })}
      {nodes.filter((node: Row) => node.in_batch).map((node: Row) => {
        const point = position(node);
        return <g key={node.knowledge_point_id}>
          <rect x={point.x} y={point.y} width={120} height={30} rx={6} fill="#fff" stroke="#cbd5e1" />
          <text x={point.x + 8} y={point.y + 19} fontSize={12}>{node.code} {String(node.name).slice(0, 8)}</text>
        </g>;
      })}
    </svg>
    <div style={{ maxHeight: 220, overflow: 'auto', marginTop: 8 }}>
      <table style={{ width: '100%' }}><thead><tr><th>前置</th><th>后继</th><th>来源</th><th>状态</th><th /></tr></thead>
        <tbody>{edges.map((edge: Row) => <tr key={edge.id}>
          <td>{nodes.find((node: Row) => node.knowledge_point_id === edge.from_kp)?.name || edge.from_kp}</td>
          <td>{nodes.find((node: Row) => node.knowledge_point_id === edge.to_kp)?.name || edge.to_kp}</td>
          <td>{edge.source}</td><td>{edge.status}</td>
          <td><button onClick={() => void removeEdge(edge.id)}>删除</button></td>
        </tr>)}</tbody></table>
    </div>
  </section>;
}

export function ErrorLabelsPanel({ batchId, setMessage }: { batchId: string; setMessage: (value: string) => void }) {
  const [data, setData] = useState<Row | null>(null);
  const load = async () => {
    if (!batchId) return;
    try { setData(await apiGet<Row>(`/api/teacher/batches/${encodeURIComponent(batchId)}/error-labels`)); }
    catch (error) { setMessage(String(error)); }
  };
  useEffect(() => { void load(); }, [batchId]);
  const classify = async () => {
    try {
      const result = await apiPost<Row>(`/api/teacher/batches/${encodeURIComponent(batchId)}/error-labels/classify`, {});
      setMessage(`已分类 ${result.classified} 条作答（跳过 ${result.skipped} 条）`);
      await load();
    } catch (error) { setMessage(String(error)); }
  };
  return <section style={box}>
    <h2>错误类型分析（规则分类）</h2>
    <p style={muted}>分类只使用已保存的判题步骤证据（error_type / judgment_reason），不重新把作答发给模型；教师可覆盖。</p>
    <button style={btn} onClick={() => void classify()}>为已判题作答生成错误标签</button>
    <table style={{ width: '100%', marginTop: 8 }}><thead><tr><th>错误类型</th><th>作答数</th><th>学生数</th><th>已确认</th></tr></thead>
      <tbody>{(data?.items || []).map((item: Row) => <tr key={item.label}><td>{item.label}</td><td>{item.answer_count}</td><td>{item.student_count}</td><td>{item.confirmed_count}</td></tr>)}</tbody></table>
    <p style={muted}>分类体系：{(data?.taxonomy || []).join('、')}</p>
  </section>;
}

export function AnswerErrorLabels({ answerId, setMessage }: { answerId: string; setMessage?: (value: string) => void }) {
  const [data, setData] = useState<Row | null>(null);
  const load = async () => {
    try { setData(await apiGet<Row>(`/api/teacher/answers/${encodeURIComponent(answerId)}/error-labels`)); }
    catch (error) { setMessage?.(String(error)); }
  };
  useEffect(() => { void load(); }, [answerId]);
  const classify = async () => {
    try {
      await apiPost(`/api/teacher/answers/${encodeURIComponent(answerId)}/error-labels/classify`, {});
      await load();
      setMessage?.('已根据判题步骤生成错误标签');
    } catch (error) { setMessage?.(String(error)); }
  };
  const latest = (data?.items || [])[0];
  return <div style={{ marginTop: 8 }}>
    <h3>错误类型</h3>
    {latest ? <p>{latest.labels.map((item: Row) => item.label).join('、')} <small style={muted}>（来源 {latest.source} · 置信度 {latest.confidence}）</small></p> : <p style={muted}>还没有错误标签。</p>}
    <button onClick={() => void classify()}>依据判题步骤生成标签</button>
  </div>;
}

export function RecommendationsPanel({ studentId, batchId }: { studentId: string; batchId: string }) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!studentId || !batchId) return;
    apiGet<Row>(`/api/teacher/students/${encodeURIComponent(studentId)}/recommendations?batch_id=${encodeURIComponent(batchId)}`).then(setData).catch(err => setError(String(err)));
  }, [studentId, batchId]);
  if (error) return <p style={muted}>{error}</p>;
  return <section style={box}>
    <h2>个性化练习推荐</h2>
    <p style={muted}>{data?.note || '基于评分点证据、覆盖率、前置依赖和错误类型生成。'}</p>
    {(data?.items || []).length === 0 && <p style={muted}>暂无可推荐项。</p>}
    {(data?.items || []).map((item: Row, index: number) => <div key={index} style={{ borderTop: index ? '1px solid #e2e8f0' : undefined, paddingTop: index ? 8 : 0, marginTop: 8 }}>
      <strong>{item.type === 'knowledge_point' ? '知识点' : item.type === 'method' ? '方法' : item.type === 'error_pattern' ? '错误模式' : '前置依赖'} · 优先级 {item.priority}</strong>
      <p>{item.reason}</p>
      {(item.suggested_questions || []).length > 0 && <p style={muted}>建议题目：{(item.suggested_questions || []).join('、')}</p>}
    </div>)}
  </section>;
}

export function StudentKnowledgeDetail({ studentId, batchId, knowledgePointId }: { studentId: string; batchId: string; knowledgePointId: string }) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    apiGet<Row>(`/api/teacher/analysis/students/${encodeURIComponent(studentId)}/knowledge/${encodeURIComponent(knowledgePointId)}?batch_id=${encodeURIComponent(batchId)}`)
      .then(setData).catch(err => setError(String(err)));
  }, [studentId, batchId, knowledgePointId]);
  if (error) return <p style={muted}>{error}</p>;
  if (!data) return <p style={muted}>正在加载证据…</p>;
  return <div style={box}>
    <h3>{data.knowledge_point?.knowledge_point_name || knowledgePointId} · 证据明细</h3>
    <p style={muted}>证据 {data.evidence_count} 条 · 可追溯 {data.traceable ? '是' : '否'} · 方法：{(data.methods || []).join('、') || '未映射'}</p>
    <p style={muted}>相关题目：{(data.questions || []).map((question: Row) => question.question_id).join('、') || '—'}</p>
    {(data.evidence || []).map((item: Row, index: number) => <div key={index} style={{ borderTop: '1px solid #e2e8f0', paddingTop: 6, marginTop: 6 }}>
      <strong>{item.question_id} · {item.scoring_point_id} · {item.earned_score}/{item.full_score}</strong>
      <p>{item.evidence_text || '无原始作答片段'}</p>
      <small style={muted}>{item.error_type} · {item.judgment_source} · {item.error_reason || '无理由'}</small>
    </div>)}
  </div>;
}
