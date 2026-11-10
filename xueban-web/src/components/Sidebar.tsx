import type { ViewKey } from '../types';
import { useWorkbench, VIEW_TITLES } from '../store';

const NAV_GROUPS: { label: string; items: ViewKey[] }[] = [
  { label: '教 学', items: ['workbench', 'approvals', 'logs', 'tasks', 'deliverables'] },
  { label: '教 研', items: ['org', 'dashboard', 'knowledge'] },
];

export default function Sidebar() {
  const { view, setView, tasks, chatApprovals, authStatus, authUser, currentCompany, logout } = useWorkbench();
  const taskBadge = tasks.filter(t => t.enabled).length;
  const approvalBadge = chatApprovals.filter(item => item.status === 'pending').length;

  return (
    <aside className="sidebar">
      <button type="button" className="brand" onClick={() => window.location.assign('/')} title="返回介绍页">
        <div className="brand-mark">学</div>
        <div className="brand-name">智慧学伴</div>
      </button>

      {NAV_GROUPS.map(group => (
        <div key={group.label}>
          <div className="nav-label">{group.label}</div>
          {group.items.map(item => (
            <button key={item} className={view === item ? 'nav-item active' : 'nav-item'} onClick={() => setView(item)} disabled={authStatus !== 'authenticated'}>
              <span className="nav-text">{VIEW_TITLES[item]}</span>
              {item === 'tasks' && authStatus === 'authenticated' && <span className="badge">{taskBadge}</span>}
              {item === 'approvals' && authStatus === 'authenticated' && approvalBadge > 0 && <span className="badge">{approvalBadge}</span>}
            </button>
          ))}
        </div>
      ))}

      <div className="sidebar-foot">
        {authStatus === 'authenticated' && authUser && currentCompany ? (
          <>
            <div className="user-card">
              <div className="user-avatar">{authUser.display_name.slice(0, 1)}</div>
              <div className="user-info"><div className="user-name">{authUser.display_name}</div><div className="user-role">{currentCompany.name}</div></div>
              <button type="button" className={view === 'settings' ? 'settings-entry active' : 'settings-entry'} onClick={() => setView('settings')} aria-label="打开设置" aria-pressed={view === 'settings'}>设置</button>
            </div>
            <button type="button" className="account-action" onClick={() => void logout()}>退出当前账号</button>
          </>
        ) : (
          <div className="account-panel">
            <div className="user-card"><div className="user-avatar">访</div><div className="user-info"><div className="user-name">{authStatus === 'loading' ? '正在确认身份' : '尚未登录'}</div></div></div>
          </div>
        )}
      </div>
    </aside>
  );
}
