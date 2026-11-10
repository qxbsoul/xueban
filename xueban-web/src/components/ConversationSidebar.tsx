import { useWorkbench } from '../store';

interface ConversationSidebarProps {
  collapsed: boolean;
  onToggle: () => void;
}

function BoundaryArrow({ collapsed }: { collapsed: boolean }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className={collapsed ? 'boundary-arrow expand' : 'boundary-arrow'}>
      <path d={collapsed ? 'M6 3.5 10.5 8 6 12.5' : 'M10 3.5 5.5 8l4.5 4.5'} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="square" strokeLinejoin="miter" />
    </svg>
  );
}

export default function ConversationSidebar({ collapsed, onToggle }: ConversationSidebarProps) {
  const {
    conversations, activeConversationId, selectConversation, newConversation,
    switchConversationKind, isConversationTyping, openModal,
  } = useWorkbench();
  const activeKind = conversations.find(item => item.id === activeConversationId)?.kind ?? 'single';
  const visible = conversations.filter(item => (item.kind ?? 'single') === activeKind);

  return (
    <aside className={collapsed ? 'conversation-sidebar collapsed' : 'conversation-sidebar'} aria-label="对话列表">
      {!collapsed && (
        <div className="conversation-sidebar-content">
          <div className="conversation-head">
            <div className="chat-kind-tabs" aria-label="对话模式">
              <button type="button" className={activeKind === 'single' ? 'active' : ''} onClick={() => switchConversationKind('single')}>单聊</button>
              <button type="button" className={activeKind === 'group' ? 'active' : ''} onClick={() => switchConversationKind('group')}>群聊</button>
            </div>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => newConversation(activeKind)}>
              {activeKind === 'group' ? '新建群聊' : '新开对话'}
            </button>
          </div>
          <div className="conversation-list">
            {visible.map(item => {
              const scheduled = item.title.startsWith('学习计划｜');
              const description = scheduled ? '学习计划 · 自动执行' : item.kind === 'group' ? '教学主管与教研员协作' : '与教学主管对话';
              return (
                <div key={item.id} className={item.id === activeConversationId ? 'conversation-item active' : 'conversation-item'}>
                  <button type="button" className="conversation-open" onClick={() => selectConversation(item.id)}>
                    <span className="conversation-title">
                      {scheduled && <span className="conversation-tag">定时</span>}
                      {item.title.replace(/^学习计划｜/, '')}
                    </span>
                    <span className="conversation-meta">{isConversationTyping(item.id) ? `执行中 · ${description}` : description}</span>
                  </button>
                  <button type="button" className="conversation-delete" title="删除对话" aria-label={`删除${item.title}`} onClick={() => openModal({ kind: 'delete-conversation', conversationId: item.id })}>删除</button>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <button
        type="button"
        className="conversation-collapse"
        onClick={onToggle}
        aria-label={collapsed ? '展开对话列表' : '收起对话列表'}
        aria-expanded={!collapsed}
        title={collapsed ? '展开对话列表' : '收起对话列表'}
      >
        <span className="conversation-collapse-pill">
          <BoundaryArrow collapsed={collapsed} />
        </span>
      </button>    </aside>
  );
}
