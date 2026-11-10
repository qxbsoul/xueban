import { useEffect, useMemo, useRef, useState } from 'react';
import type { ViewKey } from '../types';
import { useWorkbench, VIEW_TITLES } from '../store';

interface Hit {
  k: string;
  t: string;
  v: ViewKey;
}

export default function Topbar() {
  const { view, setView, tasks, conversationDeliverables, knowledge, openModal, billing, authStatus } = useWorkbench();
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('click', onDoc);
    return () => document.removeEventListener('click', onDoc);
  }, []);

  const hits = useMemo<Hit[]>(() => {
    const query = q.trim().toLowerCase();
    if (!query) return [];
    const out: Hit[] = [];
    tasks.forEach(t => {
      if (t.title.toLowerCase().includes(query)) out.push({ k: '学习计划', t: t.title, v: 'tasks' });
    });
    conversationDeliverables.forEach(d => {
      if (d.title.toLowerCase().includes(query)) out.push({ k: '学情成果', t: d.title, v: 'deliverables' });
    });
    knowledge.forEach(k => {
      if ((k.title + k.desc).toLowerCase().includes(query)) out.push({ k: '课程资源', t: k.title, v: 'knowledge' });
    });
    return out.slice(0, 8);
  }, [q, tasks, conversationDeliverables, knowledge]);

  return (
    <header className="topbar">
      <div className="page-title">{VIEW_TITLES[view]}</div>
      <div className="search-wrap" ref={wrapRef}>
        <input
          className="search-box"
          placeholder="搜索学习计划、学情成果、课程资源…"
          value={q}
          onChange={e => {
            setQ(e.target.value);
            setOpen(!!e.target.value.trim());
          }}
        />
        <div className={open ? 'search-drop show' : 'search-drop'}>
          {hits.length ? (
            hits.map((h, i) => (
              <div
                key={h.v + i}
                className="search-hit"
                onClick={() => {
                  setView(h.v);
                  setOpen(false);
                }}
              >
                <div className="k">{h.k}</div>
                <div className="t">{h.t}</div>
              </div>
            ))
          ) : (
            <div className="empty-tip" style={{ padding: '14px' }}>
              没有匹配的结果
            </div>
          )}
        </div>
      </div>
      <div className="topbar-right">
        {authStatus === 'authenticated' && billing && (
          <button
            type="button"
            className="credit-chip"
            onClick={() => setView('settings')}
            title={`${billing.plan_label} · 已用 ${billing.credits_used}/${billing.credits_total} 积分`}
          >
            <span className="credit-chip-bar"><i style={{ width: `${billing.percent_used}%` }} /></span>
            <span>积分已用 {billing.percent_used}%</span>
          </button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => setView('dashboard')}>
          学情看板
        </button>
        <button className="btn btn-primary btn-sm" onClick={() => openModal({ kind: 'new-task' })}>
          新建学习计划
        </button>
      </div>
    </header>
  );
}
