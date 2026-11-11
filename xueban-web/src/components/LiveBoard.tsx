import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { useWorkbench } from '../store';
import type { ChatActivity, ChatWorkflowItem, ChatWorkflowRun } from '../types';
import ChatRichContent from './ChatRichContent';
import { activityCategory, activityCategoryLabel } from '../activity-category';
import EmployeeAvatar from './EmployeeAvatar';
import './live-board.css';

type BoardTab = 'process' | 'outputs' | 'errors';
type ActivityStatus = 'pending' | 'waiting_approval' | 'running' | 'done' | 'failed' | 'blocked' | 'rejected';

interface DagDefinition {
  id: string;
  agent_id: string;
  name: string;
  title: string;
  depends_on: string[];
  requires_approval: boolean;
  validation_error: string | null;
}

type LiveActivity = Omit<ChatActivity, 'kind' | 'status'> & {
  kind: 'tool' | 'dispatch' | 'model' | 'approval';
  status: ActivityStatus;
  depends_on?: string[];
  thinking?: string;
  approval_id?: string;
};

type LiveRun = ChatWorkflowRun & { dag?: DagDefinition[] };

interface BoardNode {
  id: string;
  agentId: string;
  name: string;
  title: string;
  dependsOn: string[];
  requiresApproval: boolean;
  validationError: string | null;
  status: ActivityStatus;
  item?: ChatWorkflowItem;
  activity?: LiveActivity;
}

interface BoardGraph {
  nodes: BoardNode[];
  layers: BoardNode[][];
  byId: Map<string, BoardNode>;
}

const STATUS_LABELS: Record<ActivityStatus, string> = {
  pending: '待执行',
  waiting_approval: '待审批',
  running: '执行中',
  done: '已完成',
  failed: '失败',
  blocked: '已阻塞',
  rejected: '已拒绝',
};

function formatDuration(ms?: number): string {
  if (ms === undefined) return '';
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
}

function StatusPill({ status }: { status: ActivityStatus }) {
  return <span className={`dag-status dag-status-${status}`}><i aria-hidden="true" />{STATUS_LABELS[status]}</span>;
}

function nodeContent(node: BoardNode): string {
  return node.item?.content || node.activity?.output || '';
}

function nodeThinking(node: BoardNode): string {
  return node.item?.thinking || node.activity?.thinking || '';
}

function buildGraph(run: ChatWorkflowRun): BoardGraph {
  const liveRun = run as LiveRun;
  // 只把真正开始执行的节点画进 DAG；计划里的“待执行”占位不提前展开。
  const employeeActivities = (run.activities as LiveActivity[]).filter(activity => activity.name === 'employee_task' && activity.status !== 'pending');
  const nodes = new Map<string, BoardNode>();
  const itemById = new Map(run.items.map(item => [item.id, item]));

  const findItem = (id: string, agentId: string, title: string) => itemById.get(id)
    ?? run.items.find(item => item.id === employeeActivities.find(activity => activity.task_id === id)?.id)
    // 同一任务重做后有多条产出，优先取 taskId 精确匹配，其次按成员取最新一条。
    ?? [...run.items].reverse().find(item => item.taskId === id)
    ?? [...run.items].reverse().find(item => item.agentId === agentId && (!title || item.taskTitle === title || item.taskTitle === '岗位工单'))
    ?? [...run.items].reverse().find(item => item.agentId === agentId);

  for (const definition of liveRun.dag ?? []) {
    const activity = [...employeeActivities].reverse().find(candidate => candidate.task_id === definition.id || candidate.id === definition.id);
    const item = findItem(definition.id, definition.agent_id, definition.title);
    nodes.set(definition.id, {
      id: definition.id,
      agentId: definition.agent_id,
      name: definition.name,
      title: definition.title,
      dependsOn: [...definition.depends_on],
      requiresApproval: definition.requires_approval,
      validationError: definition.validation_error,
      status: item?.status ?? activity?.status ?? 'pending',
      item,
      activity,
    });
  }

  for (const activity of employeeActivities) {
    const id = activity.task_id ?? activity.id;
    const existing = nodes.get(id);
    if (existing) {
      existing.activity = activity;
      existing.item = existing.item ?? itemById.get(activity.id) ?? run.items.find(item => item.agentId === activity.agent_id);
      existing.status = existing.item?.status ?? activity.status;
      if (!activity.depends_on?.length) continue;
      existing.dependsOn = [...activity.depends_on];
      continue;
    }
    const item = itemById.get(activity.id) ?? run.items.find(candidate => candidate.id === id || candidate.agentId === activity.agent_id);
    nodes.set(id, {
      id,
      agentId: activity.agent_id ?? item?.agentId ?? '',
      name: activity.actor || item?.name || activity.title,
      title: activity.title || item?.taskTitle || '岗位工单',
      dependsOn: [...(activity.depends_on ?? [])],
      requiresApproval: Boolean(activity.approval_id),
      validationError: null,
      status: item?.status ?? activity.status,
      item,
      activity,
    });
  }

  for (const item of run.items) {
    // 同一任务回滚重做会留下多条产出：只保留最新的一条挂在原 DAG 节点上，不能多出一个“并行同事”。
    if (item.taskId && nodes.has(item.taskId)) continue;
    if ([...nodes.values()].some(node => node.item?.id === item.id)) continue;
    const activity = employeeActivities.find(candidate => candidate.id === item.id || candidate.task_id === item.id);
    nodes.set(item.id, {
      id: item.id,
      agentId: item.agentId ?? activity?.agent_id ?? '',
      name: item.name,
      title: item.taskTitle,
      dependsOn: [...(activity?.depends_on ?? [])],
      requiresApproval: Boolean(activity?.approval_id),
      validationError: null,
      status: item.status,
      item,
      activity,
    });
  }

  const allNodes = [...nodes.values()];
  const knownIds = new Set(allNodes.map(node => node.id));
  const remaining = new Set(allNodes.map(node => node.id));
  const layers: BoardNode[][] = [];

  while (remaining.size) {
    const ready = allNodes.filter(node => remaining.has(node.id)
      && node.dependsOn.every(dependency => !knownIds.has(dependency) || !remaining.has(dependency)));
    if (!ready.length) {
      // 依赖环或无效图仍完整展示，不静默丢弃任何教研员节点。
      layers.push(allNodes.filter(node => remaining.has(node.id)));
      break;
    }
    layers.push(ready);
    ready.forEach(node => remaining.delete(node.id));
  }

  return { nodes: allNodes, layers, byId: nodes };
}

function DagCanvas({ layers, selectedId, onSelect }: {
  layers: BoardNode[][];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const { chatApprovals, org } = useWorkbench();
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef(new Map<string, HTMLButtonElement>());
  const [paths, setPaths] = useState<Array<{ id: string; d: string; active: boolean }>>([]);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const zoomRef = useRef(1);
  const fitZoomRef = useRef(0.2);
  const [fitZoom, setFitZoom] = useState(0.2);
  const [baseSize, setBaseSize] = useState({ width: 0, height: 0 });
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const panPosRef = useRef({ x: 0, y: 0 });
  const baseRef = useRef({ width: 0, height: 0 });
  const centeredRef = useRef(false);
  const [panning, setPanning] = useState(false);
  const dragRef = useRef<{ x: number; y: number; startX: number; startY: number; moved: boolean } | null>(null);
  const suppressClickRef = useRef(false);
  zoomRef.current = zoom;
  panPosRef.current = pan;

  // 限制平移范围：画布始终至少有一部分留在视野里，不会拖到完全看不见。
  const clampPan = (nextZoom: number, target: { x: number; y: number }) => {
    const view = scrollRef.current;
    const base = baseRef.current;
    if (!view || !base.width || !base.height) return target;
    const width = base.width * nextZoom;
    const height = base.height * nextZoom;
    const keep = 80;
    const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max));
    return {
      x: clamp(target.x, keep - width, view.clientWidth - keep),
      y: clamp(target.y, keep - height, view.clientHeight - keep),
    };
  };
  const widestLayer = Math.max(1, ...layers.map(layer => layer.length));

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const canvasRect = canvas.getBoundingClientRect();
      // zoom 会同时放大 getBoundingClientRect 与 scrollWidth，统一除以 zoom 回到画布本地坐标。
      const scale = zoomRef.current || 1;
      const nextPaths: Array<{ id: string; d: string; active: boolean }> = [];
      for (const layer of layers) {
        for (const node of layer) {
          const target = nodeRefs.current.get(node.id);
          if (!target) continue;
          const targetRect = target.getBoundingClientRect();
          for (const dependency of node.dependsOn) {
            const source = nodeRefs.current.get(dependency);
            if (!source) continue;
            const sourceRect = source.getBoundingClientRect();
            const x1 = (sourceRect.left - canvasRect.left + sourceRect.width / 2 + canvas.scrollLeft) / scale;
            const y1 = (sourceRect.bottom - canvasRect.top + canvas.scrollTop) / scale;
            const x2 = (targetRect.left - canvasRect.left + targetRect.width / 2 + canvas.scrollLeft) / scale;
            const y2 = (targetRect.top - canvasRect.top + canvas.scrollTop) / scale;
            const bend = Math.max(18, (y2 - y1) / 2);
            nextPaths.push({
              id: `${dependency}->${node.id}`,
              d: `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`,
              active: node.status === 'running' || node.id === selectedId || dependency === selectedId,
            });
          }
        }
      }
      setPaths(nextPaths);
      // transform 不改变布局尺寸：offsetWidth/Height 就是画布原始尺寸。
      const baseWidth = canvas.offsetWidth;
      const baseHeight = canvas.offsetHeight;
      setCanvasSize({ width: baseWidth, height: baseHeight });
      setBaseSize(previous => previous.width === baseWidth && previous.height === baseHeight
        ? previous
        : { width: baseWidth, height: baseHeight });
      baseRef.current = { width: baseWidth, height: baseHeight };
      // 最小缩放 = 全部节点刚好放进可视区；最大 100%。
      const viewport = scrollRef.current;
      if (viewport) {
        const fit = Math.min(1, (viewport.clientWidth - 10) / Math.max(1, baseWidth), (viewport.clientHeight - 10) / Math.max(1, baseHeight));
        const clamped = Math.max(0.2, fit);
        fitZoomRef.current = clamped;
        setFitZoom(clamped);
        if (zoomRef.current < clamped) {
          zoomRef.current = clamped;
          setZoom(clamped);
        }
        if (!centeredRef.current) {
          centeredRef.current = true;
          const width = baseWidth * zoomRef.current;
          const height = baseHeight * zoomRef.current;
          const centered = {
            x: width <= viewport.clientWidth ? (viewport.clientWidth - width) / 2 : 0,
            y: height <= viewport.clientHeight ? (viewport.clientHeight - height) / 2 : 0,
          };
          setPan(clampPan(zoomRef.current, centered));
        } else {
          setPan(previous => {
            const next = clampPan(zoomRef.current, previous);
            return Math.abs(next.x - previous.x) < 0.5 && Math.abs(next.y - previous.y) < 0.5 ? previous : next;
          });
        }
      }
    };
    const frame = window.requestAnimationFrame(draw);
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    nodeRefs.current.forEach(node => observer.observe(node));
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [layers, selectedId]);

  // 拖动画布：按住鼠标自由平移；不拖动时点击节点仍然正常选中。
  const startPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    dragRef.current = { x: event.clientX, y: event.clientY, startX: panPosRef.current.x, startY: panPosRef.current.y, moved: false };
    suppressClickRef.current = false;
    setPanning(true);
  };
  const movePan = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const scroll = scrollRef.current;
    if (!drag || !scroll) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) {
      drag.moved = true;
      scroll.setPointerCapture(event.pointerId);
    }
    if (!drag.moved) return;
    suppressClickRef.current = true;
    setPan(clampPan(zoomRef.current, { x: drag.startX + dx, y: drag.startY + dy }));
  };
  const endPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    const scroll = scrollRef.current;
    const drag = dragRef.current;
    dragRef.current = null;
    setPanning(false);
    if (scroll?.hasPointerCapture(event.pointerId)) scroll.releasePointerCapture(event.pointerId);
    if (drag?.moved) window.setTimeout(() => { suppressClickRef.current = false; }, 0);
  };
  const guardClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!suppressClickRef.current) return;
    event.stopPropagation();
    event.preventDefault();
  };

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      event.preventDefault();
      const previous = zoomRef.current;
      const next = Math.min(1, Math.max(fitZoomRef.current, previous * (event.deltaY > 0 ? 0.9 : 1.1)));
      if (Math.abs(next - previous) < 0.001) return;
      // 以鼠标位置为锚点缩放：光标下的内容点保持不动。
      const rect = scroll.getBoundingClientRect();
      const pointX = event.clientX - rect.left;
      const pointY = event.clientY - rect.top;
      const currentPan = panPosRef.current;
      const localX = (pointX - currentPan.x) / previous;
      const localY = (pointY - currentPan.y) / previous;
      zoomRef.current = next;
      const nextPan = clampPan(next, { x: pointX - localX * next, y: pointY - localY * next });
      setZoom(next);
      setPan(nextPan);
      panPosRef.current = nextPan;
    };
    scroll.addEventListener('wheel', onWheel, { passive: false });
    return () => scroll.removeEventListener('wheel', onWheel);
  }, []);

  return (
    <div className="dag-wrap">
      <div
        className={panning ? 'dag-scroll panning' : 'dag-scroll'}
        ref={scrollRef}
        onPointerDown={startPan}
        onPointerMove={movePan}
        onPointerUp={endPan}
        onPointerCancel={endPan}
        onClickCapture={guardClick}
      >
      <div
        className="dag-canvas"
        ref={canvasRef}
        style={{
          width: baseSize.width || undefined,
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: 'top left',
          minWidth: `${Math.max(100, widestLayer * 156)}px`,
        }}
      >
        <svg className="dag-edges" width={canvasSize.width} height={canvasSize.height} aria-hidden="true">
          <defs>
            <marker id="dag-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" />
            </marker>
          </defs>
          {paths.map(path => <path key={path.id} className={path.active ? 'dag-edge active' : 'dag-edge'} d={path.d} markerEnd="url(#dag-arrow)" />)}
        </svg>
        {layers.map((layer, layerIndex) => (
          <section className="dag-layer" key={layer.map(node => node.id).join('|')} aria-label={`拓扑第 ${layerIndex + 1} 层`}>
            <div className="dag-layer-label"><span>第 {layerIndex + 1} 层</span><span>{layer.length > 1 ? `${layer.length} 项并行` : '单节点'}</span></div>
            <div className="dag-layer-nodes" style={{ gridTemplateColumns: `repeat(${layer.length}, minmax(140px, 1fr))` }}>
              {layer.map(node => {
                const approval = chatApprovals.find(item => item.task_id === node.id);
                const staff = org.staff.find(item => item.id === node.agentId)
                  ?? org.staff.find(item => item.name === node.name || node.name.startsWith(item.name));
                const displayName = staff?.name ?? node.name;
                const position = staff ? org.positions.find(item => item.id === staff.positionId) : undefined;
                const department = position?.deptId ? org.departments.find(item => item.id === position.deptId) : undefined;
                return (
                <button
                  type="button"
                  className={`dag-node dag-node-${node.status}${selectedId === node.id ? ' selected' : ''}`}
                  key={node.id}
                  ref={element => {
                    if (element) nodeRefs.current.set(node.id, element);
                    else nodeRefs.current.delete(node.id);
                  }}
                  onClick={() => onSelect(node.id)}
                  aria-label={`查看${displayName}：${node.title}，${STATUS_LABELS[node.status]}`}
                >
                  <span className="dag-node-top">
                    <EmployeeAvatar avatar={staff?.avatar} name={displayName} size={22} />
                    <StatusPill status={node.status} />
                  </span>
                  <strong>{displayName}</strong>
                  {(department || position) && (
                    <span className="dag-node-org">
                      {department && <span>{department.name}</span>}
                      {position && <span>{position.name}</span>}
                    </span>
                  )}
                  <span className="dag-node-title">{node.title}</span>
                  <span className="dag-node-meta">
                    <span>{node.dependsOn.length ? `${node.dependsOn.length} 个上游` : '无上游依赖'}</span>
                    <span>{formatDuration(node.item?.durationMs ?? node.activity?.duration_ms) || '详情 ›'}</span>
                  </span>
                  {node.requiresApproval && (
                    <span className={`dag-node-approval${approval && approval.status !== 'pending' ? ' done' : ''}`}>
                      {approval
                        ? approval.status === 'pending'
                          ? '待审批'
                          : approval.decision === 'approve' || approval.outcome === 'continue' ? '已批准' : '已驳回'
                        : '需人工审批'}
                    </span>
                  )}
                  {node.validationError && <span className="dag-node-validation">{node.validationError}</span>}
                </button>
              );})}
            </div>
          </section>
        ))}
      </div>
      </div>
      <button
        type="button"
        className="dag-zoom-hint"
        title={`滚轮缩放 · 最小 ${Math.round(fitZoom * 100)}% · 点击回到 100%`}
        onClick={() => { zoomRef.current = 1; setZoom(1); setPan(clampPan(1, { x: 0, y: 0 })); }}
      >{Math.round(zoom * 100)}%</button>
    </div>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => setCopied(false), [value]);
  const copy = async () => {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return <button type="button" className="inspector-copy" disabled={!value} onClick={() => void copy()}>{copied ? '已复制' : `复制${label}`}</button>;
}

function AgentInspector({ node, graph, onClose, onSelect, onApprovals }: {
  node: BoardNode;
  graph: BoardGraph;
  onClose: () => void;
  onSelect: (id: string) => void;
  onApprovals: () => void;
}) {
  const [thinkingOpen, setThinkingOpen] = useState(node.status === 'running');
  const content = nodeContent(node);
  const thinking = nodeThinking(node);

  useEffect(() => {
    setThinkingOpen(node.status === 'running');
  }, [node.id, node.status]);

  return (
    <div className="inspector-shell">
      <div className="inspector-head">
        <button type="button" className="inspector-back" onClick={onClose} aria-label="返回 DAG" title="返回 DAG">
          <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M10 3 5 8l5 5M5 8h8" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
        <span className="inspector-avatar" aria-hidden="true">{node.name.slice(0, 1)}</span>
        <div className="inspector-title"><strong>{node.name}</strong><span>教研员工单详情</span></div>
        <StatusPill status={node.status} />
      </div>
      <div className="inspector-stream">
        <section className="inspector-section">
          <div className="inspector-section-label">岗位工单</div>
          <h4>{node.title}</h4>
          <div className="inspector-meta"><span>节点 {node.id}</span>{node.requiresApproval && <span>完成后需审批</span>}</div>
        </section>

        <section className="inspector-section">
          <div className="inspector-section-label">真实上游依赖 · {node.dependsOn.length}</div>
          {node.dependsOn.length ? (
            <div className="inspector-upstreams">
              {node.dependsOn.map(dependency => {
                const upstream = graph.byId.get(dependency);
                return upstream ? (
                  <button type="button" key={dependency} onClick={() => onSelect(dependency)}>
                    <span><strong>{upstream.name}</strong><small>{upstream.title}</small></span>
                    <StatusPill status={upstream.status} />
                  </button>
                ) : <div className="inspector-missing" key={dependency}>上游节点 {dependency} 尚未出现在本轮数据中</div>;
              })}
            </div>
          ) : <p className="inspector-muted">无上游依赖，可在规划后直接执行。</p>}
        </section>

        {node.validationError && <section className="inspector-section inspector-alert"><strong>依赖校验异常</strong><p>{node.validationError}</p></section>}
        {(node.status === 'waiting_approval' || node.activity?.approval_id) && (
          <section className="inspector-section inspector-approval">
            <div><strong>人工审批</strong><p>{node.status === 'waiting_approval' ? '该节点正在等待审批，审批前不会继续交付。' : '该节点关联人工审批记录。'}</p></div>
            <button type="button" onClick={onApprovals}>前往审批</button>
          </section>
        )}

        <section className="inspector-section">
          <button type="button" className="inspector-thinking-toggle" onClick={() => setThinkingOpen(value => !value)} aria-expanded={thinkingOpen} disabled={!thinking}>
            <span>{node.status === 'running' ? '正在思考与执行' : '教研员思考与执行过程'}</span>
            <span>{thinking ? (thinkingOpen ? '收起' : '展开') : '未返回思考文本'}</span>
          </button>
          {thinking && <div className="inspector-copy-row"><CopyButton value={thinking} label="思考" /></div>}
          {thinkingOpen && thinking && <pre className="inspector-thinking">{thinking}</pre>}
        </section>

        <section className="inspector-section inspector-output-section">
          <div className="inspector-output-head">
            <span>{node.status === 'failed' || node.status === 'rejected' ? '失败 / 拒绝原因' : '教研员完整产出'}</span>
            <CopyButton value={content} label="产出" />
          </div>
          {content ? <ChatRichContent content={content} className="inspector-output" /> : <p className="inspector-muted">{node.status === 'pending' ? '等待上游完成后开始生成。' : '正在生成本岗位成果…'}</p>}
        </section>
      </div>
    </div>
  );
}

/** 右栏只投影本地真实对话与项目数据；教研员过程不进入单聊聊天区。 */
export default function LiveBoard() {
  const { chatWorkflow, errors, retryError, redoCheckpoint, retryLastTurn, messages, setView, activeConversationId } = useWorkbench();
  const [tab, setTab] = useState<BoardTab>('process');
  const [selectedRunId, setSelectedRunId] = useState('');
  const [inspectedItemId, setInspectedItemId] = useState('');
  const runs = chatWorkflow.runs;
  const selectedRun = useMemo(() => runs.find(run => run.id === selectedRunId) ?? runs[runs.length - 1], [runs, selectedRunId]);
  const graph = useMemo(() => selectedRun ? buildGraph(selectedRun) : { nodes: [], layers: [], byId: new Map<string, BoardNode>() }, [selectedRun]);
  const inspectedNode = inspectedItemId ? graph.byId.get(inspectedItemId) : undefined;
  const runningCount = graph.nodes.filter(node => node.status === 'running').length;
  const unresolved = errors.filter(error => !error.resolved).length;

  const checkpointByTask = useMemo(() => {
    const map = new Map<string, string>();
    for (const message of messages) {
      if (message.checkpoint?.task_id && message.checkpoint.id) map.set(message.checkpoint.task_id, message.checkpoint.id);
    }
    return map;
  }, [messages]);

  interface RunException {
    id: string;
    actor: string;
    stage: string;
    title: string;
    cause: string;
    impact: string;
    checkpointId?: string;
    resolved?: boolean;
  }

  const runExceptions = useMemo<RunException[]>(() => {
    const list: RunException[] = [];
    const latestByTask = new Map<string, ChatWorkflowItem>();
    const latestRun = runs[runs.length - 1];
    for (const item of latestRun?.items ?? []) {
      if (item.status === 'done') latestByTask.set(`${item.agentId ?? item.name}|${item.taskTitle}`, item);
    }
    for (const node of graph.nodes) {
      const failed = node.status === 'failed' || node.status === 'blocked' || node.status === 'rejected' || Boolean(node.validationError);
      if (!failed) continue;
      const key = `${node.agentId}|${node.title}`;
      const laterDone = latestByTask.has(key) && (node.item?.status !== 'done');
      list.push({
        id: `node-${node.id}`,
        actor: node.name,
        stage: node.validationError ? 'DAG 校验' : '教研员执行',
        title: `${node.title} · ${STATUS_LABELS[node.status]}`,
        cause: node.item?.content || node.activity?.detail || node.validationError || STATUS_LABELS[node.status],
        impact: node.validationError ? '依赖校验未通过，该节点未执行' : '该节点及其下游没有产出，需重跑后继续',
        checkpointId: node.activity?.checkpoint_id ?? checkpointByTask.get(node.id),
        resolved: laterDone,
      });
    }
    for (const activity of selectedRun?.activities ?? []) {
      if (activity.status !== 'failed' || activity.name === 'employee_task') continue;
      const stage = activity.name === 'manager_planning' ? '教学主管规划'
        : activity.name === 'manager_summary' ? '教学主管汇总'
          : activity.kind === 'tool' ? '工具调用' : '教学主管调度';
      list.push({
        id: `activity-${activity.id}`,
        actor: activity.actor || '教学主管',
        stage,
        title: activity.title,
        cause: activity.detail || activity.output || '执行失败',
        impact: stage === '教学主管规划' ? '本轮没有生成分工，教研员未执行' : '本轮汇总或后续步骤被中断',
        checkpointId: activity.checkpoint_id,
      });
    }
    const seen = new Set<string>();
    return list.filter(item => seen.has(item.id) ? false : (seen.add(item.id), true));
  }, [graph, selectedRun, runs, checkpointByTask, messages]);

  const badCount = runExceptions.filter(item => !item.resolved).length + unresolved;

  const outputRows = useMemo(() => {
    const latestId = runs[runs.length - 1]?.id;
    const rows = runs.flatMap(run => run.items.filter(item =>
      item.status === 'done' || item.superseded || item.status === 'failed' || item.status === 'blocked' || item.status === 'rejected',
    ).map(item => {
      const abnormal = item.status === 'failed' || item.status === 'blocked' || item.status === 'rejected';
      const rolledBack = Boolean(item.superseded);
      const history = !rolledBack && !abnormal && run.id !== latestId;
      return {
        key: `${run.id}-${item.id}`,
        item,
        runId: run.id,
        tone: (rolledBack ? 'history' : abnormal ? 'abnormal' : history ? 'history' : 'normal') as 'normal' | 'history' | 'abnormal',
        label: rolledBack ? '已被回滚' : abnormal ? '异常产物' : history ? '历史轮次' : '',
      };
    }));
    const order = { normal: 0, history: 1, abnormal: 2 } as const;
    return rows.sort((a, b) => order[a.tone] - order[b.tone] || a.item.startedAt - b.item.startedAt);
  }, [runs]);

  useEffect(() => {
    setSelectedRunId('');
    setInspectedItemId('');
    setTab('process');
  }, [activeConversationId]);

  useEffect(() => {
    if (selectedRunId && !runs.some(run => run.id === selectedRunId)) setSelectedRunId('');
    if (inspectedItemId && !graph.byId.has(inspectedItemId)) setInspectedItemId('');
  }, [runs, selectedRunId, inspectedItemId, graph]);

  return (
    <aside className="live-col">
      {selectedRun && inspectedNode ? (
        <AgentInspector node={inspectedNode} graph={graph} onClose={() => setInspectedItemId('')} onSelect={setInspectedItemId} onApprovals={() => setView('approvals')} />
      ) : (
        <>
          <div className="live-title"><span className="pulse" /><h3>协作现场</h3></div>
          <div className="live-tabs" role="tablist" aria-label="协作现场视图">
            <button type="button" role="tab" aria-selected={tab === 'process'} className={tab === 'process' ? 'active' : ''} onClick={() => setTab('process')}>过程</button>
            <button type="button" role="tab" aria-selected={tab === 'outputs'} className={tab === 'outputs' ? 'active' : ''} onClick={() => setTab('outputs')}>产物 <span>{outputRows.filter(row => row.tone !== 'history').length}</span></button>
            <button type="button" role="tab" aria-selected={tab === 'errors'} className={tab === 'errors' ? 'active' : ''} onClick={() => setTab('errors')}>异常 {badCount > 0 && <span>{badCount}</span>}</button>
          </div>
          <div className="live-stream">
            {tab === 'process' && (
              <>
                {runs.length > 0 && selectedRun && (
                  <>
                    <div className="run-switcher">
                      <select value={selectedRun.id} onChange={event => { setSelectedRunId(event.target.value); setInspectedItemId(''); }} aria-label="选择执行轮次">
                        {runs.map((run, index) => <option key={run.id} value={run.id}>第 {index + 1} 轮 · {run.goal.slice(0, 28)}</option>)}
                      </select>
                      <span>{runningCount ? `${runningCount} 项执行中` : `${graph.nodes.length} 个教研员节点`}</span>
                    </div>
                    <details className="workflow-plan" open={runningCount > 0}>
                      <summary><span>教学主管分工摘要</span><span>{selectedRun.strategy || '协作计划'}</span></summary>
                      <div className="workflow-plan-text">{selectedRun.plan || '正在理解目标并拆解任务…'}</div>
                    </details>
                    {graph.layers.length > 0 ? (
                      <DagCanvas layers={graph.layers} selectedId={inspectedItemId} onSelect={setInspectedItemId} />
                    ) : <div className="dag-empty">等待本轮 DAG 规划，所有教研员节点将在规划后一次性展示。</div>}
                    {selectedRun.activities.filter(activity => activity.name !== 'employee_task').length > 0 && (
                      <details className="dag-activity-log">
                        <summary>工具与调度记录 <span>{selectedRun.activities.filter(activity => activity.name !== 'employee_task').length}</span></summary>
                        <div className="workflow-activity-list" aria-label="本轮工具与调度记录">
                          {(selectedRun.activities as LiveActivity[])
                            .filter(activity => activity.name !== 'employee_task' && activity.kind !== 'dispatch')
                            .map(activity => {
                              const category = activityCategory(activity);
                              return (
                                <div className={`workflow-activity ${activity.status} cat-${category}`} key={activity.id}>
                                  <span className="workflow-activity-icon" aria-hidden="true">{activity.kind === 'tool' ? '⌕' : activity.kind === 'approval' ? '✓' : '◇'}</span>
                                  <div><strong>{activity.title}</strong><span><em className={`flow-tag ${category}`}>{activityCategoryLabel(category)}</em>{activity.detail}</span></div>
                                  <StatusPill status={activity.status} />
                                </div>
                              );
                            })}
                        </div>
                      </details>
                    )}
                    {graph.nodes.length > 0 && graph.nodes.every(node => node.status === 'done') && <div className="workflow-complete">教研员成果已回传教学主管，最终答复在对话区。</div>}
                  </>
                )}
                {runs.length === 0 && <div className="empty-tip">当前没有执行记录，先给教学主管提一个学情需求</div>}
              </>
            )}
            {tab === 'outputs' && (
              <div className="outputs-list">
                {outputRows.map((row, index) => {
                  const previous = outputRows[index - 1];
                  const divider = row.tone !== 'normal' && (!previous || previous.tone === 'normal');
                  const node = graph.nodes.find(candidate => candidate.item?.id === row.item.id || candidate.id === row.item.id);
                  return (
                    <div key={row.key}>
                      {divider && <div className="output-divider">{row.tone === 'history' ? '历史版本' : '异常产物'}</div>}
                      <button type="button" className={`output-item output-${row.tone}`} onClick={() => {
                        if (node) { setSelectedRunId(row.runId); setInspectedItemId(node.id); setTab('process'); }
                      }}>
                        <span className="dl-ico">{row.item.name.slice(0, 1)}</span>
                        <span className="output-main">
                          <strong>{row.item.taskTitle}</strong>
                          <span>{row.item.name} · {row.item.content ? `${row.item.content.length} 字` : STATUS_LABELS[row.item.status]} {row.label && <em className="output-tag">{row.label}</em>}</span>
                        </span>
                        <StatusPill status={row.item.status} />
                      </button>
                    </div>
                  );
                })}
                {!outputRows.length && <div className="empty-tip">还没有学情成果</div>}
              </div>
            )}
            {tab === 'errors' && (
              <div>
                {runExceptions.map(item => (
                  <div className={item.resolved ? 'err-item resolved' : 'err-item'} key={item.id}>
                    <div className="err-title"><span className={`pill ${item.resolved ? 'pill-ok' : 'pill-err'}`}>{item.resolved ? '已处理' : '异常'}</span> {item.title}</div>
                    <div className="err-line"><b>对象：</b>{item.actor} · {item.stage}</div>
                    <div className="err-line"><b>问题：</b>{item.cause}</div>
                    <div className="err-line"><b>影响：</b>{item.impact}</div>
                    <div className="err-actions">
                      <span>{item.checkpointId ? '可从异常点回滚' : '该阶段没有检查点'}</span>
                      {!item.resolved && (item.checkpointId
                        ? <button type="button" className="btn btn-primary btn-sm" onClick={() => redoCheckpoint(item.checkpointId!)}>从异常点回滚并重答</button>
                        : <button type="button" className="btn btn-primary btn-sm" onClick={() => retryLastTurn()}>重试本轮</button>)}
                    </div>
                  </div>
                ))}
                {errors.map(error => (
                  <div className={error.resolved ? 'err-item resolved' : 'err-item'} key={error.id}>
                    <div className="err-title"><span className={`pill ${error.resolved ? 'pill-ok' : 'pill-err'}`}>{error.resolved ? '已处理' : '异常'}</span> {error.title}</div>
                    <div className="err-line"><b>原因：</b>{error.cause}</div>
                    <div className="err-line"><b>影响：</b>{error.impact}</div>
                    <div className="err-line"><b>处置：</b>{error.action}</div>
                    <div className="err-actions"><span>{error.time}</span>{!error.resolved && <button className="btn btn-ghost btn-sm" onClick={() => retryError(error.id)}>标记已处理</button>}</div>
                  </div>
                ))}
                {!runExceptions.length && !errors.length && <div className="empty-tip">未发现异常，一切正常</div>}
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  );
}
