import { useEffect, useMemo, useRef, useState } from 'react';
import { useWorkbench } from '../store';
import { loadCustomModels, PROVIDER_PRESETS } from '../settings-storage';
import LiveBoard from './LiveBoard';
import ConversationSidebar from './ConversationSidebar';
import ChatRichContent from './ChatRichContent';
import { activityCategory, toolDisplayName } from '../activity-category';
import { STAGES } from '../student-profile';
import ChatApprovalCard from './ChatApprovalCard';
import EmployeeAvatar from './EmployeeAvatar';

const QUICK_CHIPS = [
  { label: '诊断本周班级学情', text: '生成本周班级学情诊断，说明数据缺口和需要巩固的内容' },
  { label: '生成学情周报', text: '按周生成学情简报，包含个体与群体两个层面' },
  { label: '复盘上周练习', text: '复盘上周练习表现，指出共性问题' },
  { label: '为薄弱学生排计划', text: '为学情数据里表现薄弱的学生安排两周学习计划' },
];

const ACCEPTED_FILES = '.png,.jpg,.jpeg,.webp,.gif,.mp4,.webm,.txt,.md,.markdown,.log,.csv,.json,.jsonl,.html,.htm,.pdf,.docx,.pptx,.xlsx,.xlsm';
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const VIDEO_TYPES = new Set(['video/mp4', 'video/webm']);
const TEXT_TYPES = new Set(['txt', 'md', 'markdown', 'log', 'csv', 'json', 'jsonl', 'html', 'htm']);
const DOC_TYPES = new Set(['pdf', 'docx', 'pptx', 'xlsx', 'xlsm']);

/** 用户这句话是否要求把附件存进课程资源库。 */
const KNOWLEDGE_INTENT = /(存入|存进|放进|放入|加入|添加到|导入|收录|归档|入库)/;
const KNOWLEDGE_TARGET = /(知识库|资源库|资料库|课程资料)/;

type SpeechResultEvent = {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
};
type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
};

function speechRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const win = window as unknown as {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  };
  return win.SpeechRecognition ?? win.webkitSpeechRecognition ?? null;
}

export default function WorkbenchView() {
  const { messages, typing, sendChat, editChatMessage, redoCheckpoint, conversations, activeConversationId, setConversationModel, setConversationEffort, conversationStartedAt, studentProfile, saveStudentProfile, authUser, toast, apiSettings, selectModel, org, uploadKnowledge } = useWorkbench();
  const [text, setText] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [listening, setListening] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [savedFiles, setSavedFiles] = useState<Set<string>>(new Set());
  const [trailToggles, setTrailToggles] = useState<Record<string, boolean>>({});
  const gradePromptKey = authUser ? `sf_grade_prompted:${authUser.id}:${new Date().toISOString().slice(0, 10)}` : '';
  const [gradeDismissed, setGradeDismissed] = useState(false);
  useEffect(() => {
    if (!gradePromptKey) return;
    try { setGradeDismissed(localStorage.getItem(gradePromptKey) === '1'); } catch { setGradeDismissed(false); }
  }, [gradePromptKey]);
  const [gradeDraft, setGradeDraft] = useState(() => ({
    stage: studentProfile?.stage ?? ('junior' as const),
    grade: studentProfile?.grade ?? 1,
  }));
  useEffect(() => {
    if (studentProfile) setGradeDraft({ stage: studentProfile.stage, grade: studentProfile.grade });
  }, [studentProfile]);
  const dismissGradePrompt = () => {
    setGradeDismissed(true);
    if (!gradePromptKey) return;
    try { localStorage.setItem(gradePromptKey, '1'); } catch { /* 忽略存储异常 */ }
  };
  // 学段年级存在用户表里：登录用户没填过才提示，填过就永远不再弹。
  const showGradePrompt = Boolean(authUser) && !studentProfile && !gradeDismissed;
  // 每轮数据流的首条位置 / 步数，以及正式回答是否已经出现。
  const trailInfo = useMemo(() => {
    const info = new Map<string, { first: number; count: number; last: number; start: number; end: number }>();
    messages.forEach((message, index) => {
      const activity = message.activity;
      // checkpoint 也属于本轮工作流，跟着一起折叠。
      const checkpointRun = !activity && message.checkpoint ? message.runId : undefined;
      const runId = activity?.run_id ?? checkpointRun;
      if (!runId) return;
      if (activity) {
        // 这些行不进入数据流：派发/汇总由结论覆盖，工具只在完成后显示一行，
        // 计划里的“等待上游”占位也不提前出现。
        if (activity.name === 'employee_dispatch' || activity.name === 'manager_summary') return;
        if (activity.status === 'pending') return;
        if (activity.kind === 'tool' && activity.status === 'running') return;
      }
      const ts = message.at ? Date.parse(message.at) : Number.NaN;
      const existing = info.get(runId);
      if (existing) {
        existing.count += 1;
        existing.last = index;
        if (!Number.isNaN(ts)) { existing.start = Math.min(existing.start, ts); existing.end = Math.max(existing.end, ts); }
      } else {
        info.set(runId, {
          first: index, count: 1, last: index,
          start: Number.isNaN(ts) ? Number.POSITIVE_INFINITY : ts,
          end: Number.isNaN(ts) ? 0 : ts,
        });
      }
    });
    const failed = new Set<string>();
    for (const message of messages) {
      const activity = message.activity;
      if (activity && (activity.status === 'failed' || activity.status === 'blocked' || activity.status === 'rejected')) {
        failed.add(activity.run_id);
      }
    }
    const answered = new Set<string>();
    const durations = new Map<string, number>();
    for (const [runId, meta] of info) {
      const answer = messages.slice(meta.last + 1).some(message =>
        !message.activity && message.from === 'ceo' && (message.streaming || Boolean(message.text)));
      if (!answer) continue;
      answered.add(runId);
      let end = meta.end;
      for (const message of messages) {
        if (message.runId !== runId || !message.at) continue;
        const ts = Date.parse(message.at);
        if (!Number.isNaN(ts)) end = Math.max(end, ts);
      }
      if (Number.isFinite(meta.start) && end > meta.start) durations.set(runId, end - meta.start);
    }
    return { info, answered, failed, durations };
  }, [messages]);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const voiceBaseRef = useRef('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // 对话页列出所有供应商的模型（自定义模型可并存多条），选中后自动切换。
  const modelGroups = Object.values(PROVIDER_PRESETS).map(preset => ({ key: preset.key, label: preset.label, models: preset.models }));
  const customEntries = loadCustomModels();
  const activeMeta = conversations.find(item => item.id === activeConversationId);
  const fallbackModel = apiSettings.preset === 'custom' && apiSettings.customId ? `custom:${apiSettings.customId}` : apiSettings.model;
  const selectValue = activeMeta?.model || fallbackModel;
  const effortValue = activeMeta?.thinkingEffort || 'low';
  const hasCurrentModel = modelGroups.some(group => group.models.some(model => model.id === selectValue))
    || customEntries.some(entry => `custom:${entry.id}` === selectValue);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(new Set());
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const formatDuration = (ms?: number) => {
    if (!ms || ms < 0) return '';
    const total = Math.round(ms / 1000);
    if (total < 60) return `${Math.max(1, total)} 秒`;
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (minutes < 60) return `${minutes} 分 ${seconds} 秒`;
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  };
  const bodyRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const activeConversation = conversations.find(item => item.id === activeConversationId);
  const projectLocked = Boolean(activeConversation?.projectId);
  const isGroup = activeConversation?.kind === 'group';
  const bossMessages = messages.filter(message => message.from === 'boss' && message.channel !== 'workflow' && message.id && !message.streaming);
  const latestBossMessageId = bossMessages[bossMessages.length - 1]?.id;

  useEffect(() => {
    const el = bodyRef.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, typing]);

  useEffect(() => {
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
    setEditingId(null);
  }, [activeConversationId]);

  useEffect(() => {
    if (!typing) {
      setElapsedSeconds(0);
      return;
    }
    // 用对话级的开始时间：切走再切回来继续累计，不会被重置。
    const startedAt = conversationStartedAt(activeConversationId) ?? Date.now();
    const tick = () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [typing, activeConversationId, conversationStartedAt]);

  useEffect(() => {
    return () => { recognitionRef.current?.abort(); };
  }, []);

  const sendAndFollow = (value: string) => {
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
    sendChat(value);
  };
  const send = () => {
    const value = text.trim();
    if ((!value && !files.length) || typing) return;
    if (listening) stopVoice();
    const pending = files;
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
    setText('');
    setFiles([]);
    setSavedFiles(new Set());
    sendChat(value, pending);
    // 用户明确要求「存进知识库/资源库」时，同一批附件同时入库（不阻塞对话）。
    if (pending.length && KNOWLEDGE_INTENT.test(value) && KNOWLEDGE_TARGET.test(value)) {
      void ingestFiles(pending);
    }
  };
  const addFiles = (incoming: File[]) => {
    const valid: File[] = [];
    for (const file of incoming) {
      const extension = file.name.toLowerCase().split('.').pop() || '';
      const image = IMAGE_TYPES.has(file.type) && ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension);
      const video = VIDEO_TYPES.has(file.type) && ['mp4', 'webm'].includes(extension);
      const text = TEXT_TYPES.has(extension);
      const doc = DOC_TYPES.has(extension);
      if (!image && !video && !text && !doc) { toast(`${file.name} 格式不支持`); continue; }
      const limit = image ? 10 * 1024 * 1024 : video ? 25 * 1024 * 1024 : text ? 2 * 1024 * 1024 : 25 * 1024 * 1024;
      if (file.size > limit) {
        toast(`${file.name} 超过大小限制（${Math.round(limit / 1024 / 1024)} MB）`);
        continue;
      }
      valid.push(file);
    }
    const seen = new Set(files.map(file => `${file.name}-${file.size}-${file.lastModified}`));
    const deduped = valid.filter(file => !seen.has(`${file.name}-${file.size}-${file.lastModified}`));
    const next = [...files, ...deduped];
    if (next.length > 4) { toast('每条消息最多添加 4 个文件'); return; }
    if (next.reduce((sum, file) => sum + file.size, 0) > 25 * 1024 * 1024) { toast('附件总大小不能超过 25 MB'); return; }
    setFiles(next);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };
  const stopVoice = () => {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setListening(false);
  };

  const toggleVoice = () => {
    if (listening) { stopVoice(); return; }
    const Recognition = speechRecognitionCtor();
    if (!Recognition) { toast('当前浏览器不支持语音输入，请使用 Edge 或 Chrome'); return; }
    const recognition = new Recognition();
    recognition.lang = 'zh-CN';
    recognition.continuous = true;
    recognition.interimResults = true;
    voiceBaseRef.current = text.trim() ? `${text.trim()} ` : '';
    recognition.onresult = event => {
      let interim = '';
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const transcript = result[0]?.transcript ?? '';
        if (result.isFinal) voiceBaseRef.current += transcript;
        else interim += transcript;
      }
      setText((voiceBaseRef.current + interim).slice(0, 4000));
    };
    recognition.onerror = event => {
      const reason = event.error === 'not-allowed' || event.error === 'service-not-allowed'
        ? '麦克风权限被拒绝，请在浏览器地址栏允许麦克风后重试'
        : event.error === 'no-speech'
          ? '没有识别到语音，请再说一遍'
          : event.error === 'network'
            ? '语音识别服务连接失败，请检查网络'
            : `语音输入失败：${event.error}`;
      toast(reason);
      setListening(false);
    };
    recognition.onend = () => {
      setListening(false);
      setText(value => value.replace(/\s+$/, ''));
    };
    recognitionRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
    } catch {
      toast('麦克风启动失败，请检查浏览器权限');
      setListening(false);
    }
  };

  const ingestFiles = async (targets: File[]) => {
    if (!targets.length || ingesting) return;
    const videos = targets.filter(file => VIDEO_TYPES.has(file.type));
    const uploadable = targets.filter(file => !VIDEO_TYPES.has(file.type));
    if (!uploadable.length) {
      toast('视频暂不支持入库，请上传文档、图片或文本');
      return;
    }
    setIngesting(true);
    try {
      const sources = await uploadKnowledge(uploadable);
      if (sources.length) {
        setSavedFiles(previous => new Set([...previous, ...uploadable.map(file => `${file.name}-${file.size}-${file.lastModified}`)]));
        toast(`已存入课程资源库：${sources.length} 份`);
      }
      if (videos.length) toast(`${videos.map(file => file.name).join('、')} 是视频，暂不支持入库`);
    } catch (error) {
      toast(error instanceof Error ? error.message : '存入课程资源库失败');
    } finally {
      setIngesting(false);
    }
  };

  const jumpToLatest = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    el.scrollTop = el.scrollHeight;
    setShowJumpToLatest(false);
  };
  const handleChatScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < 80;
    setShowJumpToLatest(distance > 120);
  };

  const copyMessage = async (value: string) => {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        copied = true;
      } catch {
        // 部分浏览器拒绝异步剪贴板时，仍在本次点击中尝试兼容复制。
      }
    }
    if (!copied) {
      const textarea = document.createElement('textarea');
      textarea.value = value;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      textarea.style.pointerEvents = 'none';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        copied = document.execCommand('copy');
      } catch {
        copied = false;
      } finally {
        textarea.remove();
      }
    }
    toast(copied ? '已复制' : '复制失败，请手动选择文本');
  };

  const toggleThinking = (id: string) => {
    setExpandedThinking(previous => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="workbench">
      {showGradePrompt && (
        <div className="modal-mask show" role="dialog" aria-modal="true" aria-labelledby="grade-prompt-title">
          <div className="modal profile-modal">
            <h3 id="grade-prompt-title">先告诉我你在读几年级</h3>
            <p>学习计划、例题和练习都会按你的学段年级来出。设置一次就行，之后每年 9 月会自动升一级。</p>
            <div className="profile-modal-row">
              <select className="form-select" value={gradeDraft.stage} onChange={event => {
                setGradeDraft({ stage: event.target.value as typeof gradeDraft.stage, grade: 1 });
              }}>
                {STAGES.map(stage => <option key={stage.key} value={stage.key}>{stage.label}</option>)}
              </select>
              <select className="form-select" value={gradeDraft.grade} onChange={event => setGradeDraft(previous => ({ ...previous, grade: Number(event.target.value) }))}>
                {Array.from({ length: STAGES.find(stage => stage.key === gradeDraft.stage)?.grades ?? 1 }, (_, index) => index + 1)
                  .map(grade => <option key={grade} value={grade}>{STAGES.find(stage => stage.key === gradeDraft.stage)?.gradeName(grade)}</option>)}
              </select>
            </div>
            <div className="profile-modal-actions">
              <button type="button" className="btn btn-primary" onClick={() => {
                void saveStudentProfile(gradeDraft.stage, gradeDraft.grade).then(next => {
                  if (!next) return;
                  dismissGradePrompt();
                  toast('已保存学段年级，之后的回答会按这个难度来');
                }).catch(() => toast('保存失败，请稍后重试'));
              }}>保存并开始</button>
              <button type="button" className="btn btn-ghost" onClick={dismissGradePrompt}>今天先跳过</button>
            </div>
          </div>
        </div>
      )}
      <ConversationSidebar collapsed={sidebarCollapsed} onToggle={() => setSidebarCollapsed(value => !value)} />
      <div className="chat-col">
        <div className="chat-head">
          <EmployeeAvatar
            avatar={org.positions.find(position => position.root)?.avatar}
            name={isGroup ? '学伴协作群' : '教学主管'}
            size={34}
            dot
          />
          <div className="name">{isGroup ? '学伴协作群' : '教学主管'}</div>
          <label className="model-chip" aria-label="当前模型">
            <span>模型</span>
            <select
              value={selectValue}
              disabled={typing}
              onChange={event => {
                const value = event.target.value;
                if (activeConversationId) setConversationModel(activeConversationId, value);
                selectModel(value);
              }}
              title="切换模型（只影响当前对话）"
            >
              {modelGroups.map(group => (
                <optgroup key={group.key} label={group.label}>
                  {group.models.map(model => <option key={model.id} value={model.id}>{model.label}</option>)}
                </optgroup>
              ))}
              {customEntries.length > 0 && (
                <optgroup label="自定义">
                  {customEntries.map(entry => <option key={entry.id} value={`custom:${entry.id}`}>{entry.label}（{entry.model}）</option>)}
                </optgroup>
              )}
              {!hasCurrentModel && <option value={selectValue}>{apiSettings.model || '未配置模型'}</option>}
            </select>
          </label>
          <label className="model-chip effort-chip" aria-label="思考强度">
            <span>思考</span>
            <select
              value={effortValue}
              disabled={typing}
              onChange={event => {
                const value = event.target.value as 'off' | 'low' | 'medium' | 'high';
                if (activeConversationId) setConversationEffort(activeConversationId, value);
                try { localStorage.setItem('sf_thinking_effort', value); } catch { /* 忽略存储异常 */ }
              }}
              title="思考强度（只影响当前对话）"
            >
              <option value="low">低</option>
              <option value="medium">中</option>
              <option value="high">高</option>
              <option value="off">关闭</option>
            </select>
          </label>
        </div>

        <div className="chat-scroll-wrap">
          <div className="chat-body" ref={bodyRef} onScroll={handleChatScroll}>
            {messages.map((m, i) => {
              const messageId = m.id ?? `message-${i}`;
              const showThinking = Boolean(m.thinking) && (!m.thinkingDone || expandedThinking.has(messageId));
              // 历史消息里可能残留「姓名（岗位）」写法；统一用教研员档案里的姓名展示。
              const senderStaff = m.from === 'ceo' || m.from === 'boss' ? undefined : (
                org.staff.find(item => item.id === m.agentId)
                ?? org.staff.find(item => item.name === m.senderName)
                ?? org.staff.find(item => Boolean(m.senderName?.startsWith(item.name)))
              );
              const senderLabel = senderStaff?.name ?? m.senderName ?? (m.from === 'ceo' ? '教学主管' : '系统');
              if (m.approval) return <ChatApprovalCard key={messageId} approval={m.approval} />;
              if (m.checkpoint) {
                const checkpointCard = (
                  <div className="chat-checkpoint">
                    <div className="chat-checkpoint-body">
                      <strong>Checkpoint</strong>
                      <small>{m.text || `执行 ${m.checkpoint.task_title} 之前`}</small>
                    </div>
                    <button type="button" disabled={typing || projectLocked} onClick={() => redoCheckpoint(m.checkpoint!.id)}>从这里重做</button>
                  </div>
                );
                const runId = m.runId;
                const trail = runId ? trailInfo.info.get(runId) : undefined;
                if (!trail || !runId) return <div key={messageId}>{checkpointCard}</div>;
                const open = trailToggles[runId] ?? !trailInfo.answered.has(runId);
                if (!open && trail.first !== i) return null;
                const header = trail.first === i ? (
                  <button type="button" className="run-trail-head" aria-expanded={open}
                    onClick={() => setTrailToggles(previous => ({ ...previous, [runId]: !open }))}>
                    <span className="run-trail-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
                    <span className="run-trail-title">本轮过程</span>
                    <span className="run-trail-count">{trail.count} 步</span>
                    {trailInfo.durations.has(runId) && <span className="run-trail-duration">用时 {formatDuration(trailInfo.durations.get(runId))}</span>}
                  </button>
                ) : null;
                return <div key={messageId}>{header}{open ? checkpointCard : null}</div>;
              }
              if (m.activity) {
                const activity = m.activity;
                // 派发与汇总不进入数据流；工具只在有结果后显示一行（调用/完成合并）。
                if (activity.name === 'employee_dispatch' || activity.name === 'manager_summary') return null;
                if (activity.status === 'pending') return null;
                if (activity.kind === 'tool' && activity.status === 'running') return null;
                const stateText = { pending: '等待上游', waiting_approval: '等待老师', running: '执行中', done: '完成', failed: '失败', blocked: '已阻塞', rejected: '已驳回' }[activity.status];
                const trail = trailInfo.info.get(activity.run_id);
                // 三角开关：用户手动开合优先；没手动过时，执行中展开、正式回答出现后自动收起。
                const trailOpen = trail ? (trailToggles[activity.run_id] ?? !trailInfo.answered.has(activity.run_id)) : true;
                const isFirst = trail ? trail.first === i : true;
                if (trail && !isFirst && !trailOpen) return null;
                const title = activity.kind === 'tool' ? toolDisplayName(activity.name) : activity.title;
                const header = trail && isFirst ? (
                  <button
                    type="button"
                    className="run-trail-head"
                    aria-expanded={trailOpen}
                    onClick={() => setTrailToggles(previous => ({ ...previous, [activity.run_id]: !trailOpen }))}
                  >
                    <span className="run-trail-caret" aria-hidden="true">{trailOpen ? '▾' : '▸'}</span>
                    <span className="run-trail-title">本轮过程</span>
                    <span className="run-trail-count">{trail.count} 步</span>
                    {trailInfo.durations.has(activity.run_id) && <span className="run-trail-duration">用时 {formatDuration(trailInfo.durations.get(activity.run_id))}</span>}
                  </button>
                ) : null;
                if (trail && isFirst && !trailOpen) return <div key={messageId}>{header}</div>;
                return (
                  <div key={messageId}>
                    {header}
                    <div className={`chat-activity ${activity.kind} ${activityCategory(activity)} ${activity.status}`}>
                      <div className="chat-activity-marker" aria-hidden="true">{activity.kind === 'tool' ? '⌕' : activity.kind === 'dispatch' ? '→' : activity.kind === 'approval' ? '✓' : '◇'}</div>
                      <div className="chat-activity-body">
                        <div className="chat-activity-head"><strong>{title}</strong><span>{stateText}{activity.duration_ms !== undefined ? ` · ${Math.max(1, Math.round(activity.duration_ms / 1000))} 秒` : ''}</span></div>
                        {activity.kind !== 'tool' && activity.detail && <div className="chat-activity-detail">{activity.detail}</div>}
                        {activity.thinking && (
                          <details className="chat-activity-thinking" open={activity.status === 'running'}>
                            <summary>教学主管思考过程</summary><div>{activity.thinking}</div>
                          </details>
                        )}
                        {activity.output && <details className="chat-activity-output"><summary>查看{activity.kind === 'model' ? '分工' : '结果'}</summary><ChatRichContent content={activity.output} /></details>}
                      </div>
                    </div>
                  </div>
                );
              }
              return (
                <div key={messageId} className={m.from === 'boss' ? 'msg-row boss' : `msg-row ${m.from}`}>
                  {m.from !== 'boss' && (
                    <EmployeeAvatar
                      avatar={m.from === 'ceo'
                        ? org.positions.find(position => position.root)?.avatar
                        : senderStaff?.avatar}
                      name={senderLabel}
                      size={28}
                    />
                  )}
                  <div>
                    {m.from !== 'boss' && (
                      <div className="msg-sender">
                        {senderLabel}
                        {m.from === 'ceo' && m.runId && trailInfo.failed.has(m.runId) && (
                          <span className="partial-badge" title="本轮有环节未完成，答复已基于已完成部分给出">部分环节未完成</span>
                        )}
                      </div>
                    )}
                    {editingId === m.id ? (
                      <div className="message-editor">
                        <textarea aria-label="编辑最近发言" value={editDraft} onChange={event => setEditDraft(event.target.value)} rows={4} maxLength={12000} autoFocus />
                        <div className="message-editor-actions">
                          <button type="button" onClick={() => setEditingId(null)}>取消</button>
                          <button type="button" disabled={!editDraft.trim()} onClick={() => { editChatMessage(m.id!, editDraft); setEditingId(null); }}>保存并重新生成</button>
                        </div>
                      </div>
                    ) : (
                      <div className="bubble">
                        {m.attachments?.length ? <div className="message-attachments">{m.attachments.map((attachment, index) => <div className={`message-attachment ${attachment.kind}`} key={`${attachment.name}-${index}`}>
                          {attachment.kind === 'image' && attachment.data_url && <img src={attachment.data_url} alt={attachment.name} />}
                          {attachment.kind === 'video' && attachment.data_url && <video src={attachment.data_url} controls preload="metadata" />}
                          <span>{attachment.kind === 'text' ? 'TXT · ' : ''}{attachment.name}</span>
                        </div>)}</div> : null}
                        {m.thinking && (
                          <div className={m.thinkingDone ? 'reasoning-block done' : 'reasoning-block'}>
                            <button type="button" className="reasoning-toggle" onClick={() => toggleThinking(messageId)} aria-expanded={showThinking}>
                              <span>{m.thinkingDone ? '思考过程' : '正在思考'}</span>
                              <span className={showThinking ? 'reasoning-chevron open' : 'reasoning-chevron'} aria-hidden="true">⌄</span>
                            </button>
                            {showThinking && <div className="reasoning-content">{m.thinking}</div>}
                          </div>
                        )}
                        <ChatRichContent content={m.text} className="answer-content" />
                        {m.card && (
                          <div className="biz-card">
                            <div className="bc-head">交付物</div>
                            <div className="bc-body">
                              <div className="bc-title">{m.card.title}</div>
                              <div className="bc-meta">{m.card.meta}</div>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    <div className="msg-meta">
                      <span className="msg-time">{m.time}</span>
                      {(m.from === 'boss' || m.from === 'ceo') && !m.streaming && m.channel !== 'workflow' && editingId !== m.id && (
                        <span className="msg-actions">
                          {m.from === 'boss' && m.id === latestBossMessageId && <button type="button" disabled={typing || projectLocked} onClick={() => { setEditingId(m.id!); setEditDraft(m.text); }}>编辑</button>}
                          <button type="button" onClick={() => void copyMessage(m.text)}>复制</button>
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
            {typing && (
              <div className="thinking-elapsed" role="status" aria-live="polite">
                <span className="thinking-pulse" aria-hidden="true" />
                已思考 {elapsedSeconds} 秒
              </div>
            )}
          </div>
          {showJumpToLatest && (
            <button type="button" className="jump-latest" onClick={jumpToLatest} aria-label="回到最新对话" title="回到最新对话">
              <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path d="M10 3v12M5 10l5 5 5-5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="square" /></svg>
            </button>
          )}
        </div>

        <div className="chat-foot">
          <div className="quick-chips">
            {QUICK_CHIPS.map(c => <button key={c.label} className="chip" disabled={typing || projectLocked} onClick={() => sendAndFollow(c.text)}>{c.label}</button>)}
          </div>
          {projectLocked && <div className="project-locked">此对话已绑定项目；有新需求请点击“新开对话”。</div>}
          {files.length > 0 && <div className="composer-files">{files.map((file, index) => <span className="composer-file" key={`${file.name}-${file.size}-${file.lastModified}`}>
            <span>{file.name}<small>{file.size < 1024 * 1024 ? `${Math.max(1, Math.round(file.size / 1024))} KB` : `${(file.size / 1024 / 1024).toFixed(2)} MB`}</small></span>
            <button type="button" className="composer-file-save" disabled={ingesting} onClick={() => void ingestFiles([file])} title="存入课程资源库">
              {savedFiles.has(`${file.name}-${file.size}-${file.lastModified}`) ? '已入库' : '入库'}
            </button>
            <button type="button" aria-label={`移除 ${file.name}`} onClick={() => setFiles(previous => previous.filter((_, itemIndex) => itemIndex !== index))}>×</button>
          </span>)}</div>}
          <div className={`input-row ${dragging ? 'dragging' : ''}`}
            onDragEnter={event => { event.preventDefault(); dragDepthRef.current += 1; setDragging(true); }}
            onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }}
            onDragLeave={event => { event.preventDefault(); dragDepthRef.current -= 1; if (dragDepthRef.current <= 0) { dragDepthRef.current = 0; setDragging(false); } }}
            onDrop={event => { event.preventDefault(); dragDepthRef.current = 0; setDragging(false); if (!typing && !projectLocked) addFiles(Array.from(event.dataTransfer.files)); }}>
            <input ref={fileInputRef} className="composer-file-input" type="file" multiple accept={ACCEPTED_FILES} onChange={event => addFiles(Array.from(event.target.files ?? []))} />
            <button type="button" className="composer-add" disabled={typing || projectLocked} onClick={() => fileInputRef.current?.click()} aria-label="添加图片、视频或 TXT 文件" title="添加图片、视频或 TXT 文件">＋</button>
            <textarea
              className="chat-input"
              rows={1}
              placeholder={projectLocked ? '此对话已绑定项目，请新开对话创建新项目' : '给教学主管交代任务，或拖入图片、文档、TXT'}
              value={text}
              disabled={projectLocked}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button type="button" className={`composer-mic${listening ? ' recording' : ''}`} disabled={typing || projectLocked} onClick={toggleVoice} aria-label={listening ? '停止语音输入' : '语音输入'} aria-pressed={listening} title={listening ? '停止语音输入' : '语音输入（自动转成文字）'}>
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor" />
                <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8.5 21h7" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
              </svg>
            </button>
            <button type="button" className="send-btn" onClick={send} disabled={typing || projectLocked || (!text.trim() && !files.length)} title="发送">发送</button>
            {dragging && <div className="composer-drop-hint">松开以上传图片、文档或 TXT</div>}
          </div>
        </div>
      </div>
      <LiveBoard />
    </div>
  );
}
