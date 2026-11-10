/**
 * chat-strategy.ts — 群聊意图识别 + 策略矩阵（2026-11-07，GrokBot 群聊范式对齐）。
 *
 * 背景（用户反馈 + 知乎《Grok Bot 两周实测》/ grokbot-usage-guide-zh 两份参考）：
 * 群聊不该「一句话全员广播」——发个 hi 跑出一堆回复、任务消息人人来一遍。
 * 多 Bot 协作的正确形态：先**意图识别**，再按**策略矩阵**选协作策略，复杂任务
 * 按 **DAG 工单拆解**派给对口的执行者，最终只看到「对的人、对的回复」。
 *
 * 策略矩阵（意图 × 复杂度 → 协作策略）：
 *
 *   意图 \ 形态        单工种/单人            多工种/多人
 *   ─────────────────────────────────────────────────────────
 *   casual  寒暄/应答   solo 单人接话           —
 *   question 问答/咨询  expert 专家应答（1 人）  并行拆分（多问各自对口）
 *   discussion 讨论     roundtable 圆桌（2-3 人给观点）
 *   task    任务        expert 专家执行（1 人）  pipeline 流水线（有先后依赖）
 *                                              / parallel 并行分工（互相独立）
 *   （任务但面向全体的号召：大家/各位/全员 → all-hands 全员参与）
 *
 * 分层：LLM 编排者（orchestrator-parse.planWithLlm，BYOK 可用时）优先出
 * ChatPlan；不可用/失败 → 本模块 heuristicPlan 启发式兜底（纯函数、可单测）。
 * **任何路径都不再「用户原文分发给全体成员」**——无信号时也只派最可能的 1 人。
 *
 * 纯函数、零 IO：分类 / 打分 / 拆单全部可离线单测（同 intent-router 纪律）。
 */
import type { ParsedTask } from './orchestrator-parse';

// ─── 计划类型 ───────────────────────────────────────────────────────────────

export type ChatIntent = 'casual' | 'question' | 'discussion' | 'task';
export type ChatStrategy = 'solo' | 'expert' | 'pipeline' | 'parallel' | 'roundtable' | 'all-hands';

/** 参与路由的成员投影（id + 展示名 + persona 摘要，专长打分用 soul）。 */
export interface StrategyAgent {
  id: string;
  name: string;
  soul?: string;
}

export interface ChatPlan {
  intent: ChatIntent;
  strategy: ChatStrategy;
  /** 工单：执行者 + 每人自包含 prompt；顺序 = 串行执行/展示顺序。 */
  tasks: ParsedTask[];
  /** 计划来源：@点名（用户即调度者）/ 寒暄 / LLM 编排者拆解 / 策略矩阵启发式。 */
  source: 'mention' | 'casual' | 'llm' | 'heuristic';
  /** 编排模型显式返回的思考内容；不包含规划 JSON。 */
  thinking?: string;
}

export const INTENT_LABELS: Record<ChatIntent, string> = {
  casual: '寒暄',
  question: '问答',
  discussion: '讨论',
  task: '任务',
};

export const STRATEGY_LABELS: Record<ChatStrategy, string> = {
  solo: '单人应答',
  expert: '专家应答',
  pipeline: '流水线拆解',
  parallel: '并行拆分',
  roundtable: '圆桌讨论',
  'all-hands': '全员参与',
};

// ─── 意图识别 ───────────────────────────────────────────────────────────────

// 寒暄/应答（从 groups-chat.ts 迁入 —— 策略矩阵第一行）：整条消息就是一句
// 问候/感谢/收尾，≤24 字、单行、无 @ 点名。
const CASUAL_LINE_RE =
  /^(?:hi+|hiya|hey+|hello+|yo|howdy|sup|hola|hello\s+there|good\s+(?:morning|afternoon|evening)|早上好|早安|早啊|早|中午好|下午好|晚上好|晚安|你好|您好|哈喽|嗨+|在吗|在么|在不在|thanks?|thank\s+(?:you|u)|thx+|tysm|ty|谢谢(?:你|您)?|多谢|感谢(?:你|您)?|辛苦了|麻烦了|ok(?:ay)?|好的?|好嘞|好滴|好哒|收到|明白|了解|没问题|嗯+|哦+|啊+|行|可以|拜拜|再见|88|回见|回头见)(?:[呀啦哦呢嘛呗哇吖!?！。.~～,，]*)$/i;

/** 寒暄判定（纯函数）：整条消息是一句寒暄/应答 → true。 */
export function isCasualChat(text: string): boolean {
  const t = (text ?? '').trim();
  if (!t || t.length > 24) return false;
  if (/[\n@]/.test(t)) return false;
  return CASUAL_LINE_RE.test(t);
}

// 讨论/征求意见信号（先于任务词判定：「帮我选 A 还是 B」是讨论不是派活）。
const DISCUSS_RE =
  /大家觉得|你们觉得|你怎么看|你们怎么看|大家怎么看|各位怎么看|帮我选|选哪个|哪个好|哪个更好|还是|讨论一下|商量|投票|发表(?:意见|看法)|给点(?:建议|意见|看法)|有什么(?:建议|意见|看法|推荐)|有何(?:建议|看法)|推荐一下|选择困难/;

// 强任务动词（祈使/委托口吻 → 派活）。注意不含裸「请」——「请问」走问答。
const STRONG_TASK_RE =
  /帮我|给我|请你|请帮|帮忙|麻烦你?|写[一十-龥个篇份条段]|生成|做[一十-龥个]|制作|实现|开发|设计|分析|整理|总结|翻译|优化|修复|改[一十-龥下]|查一下|找一下|出一份|起草|拟[一十-龥个]|规划|安排|检查|测试|评审|审查|调研|搭[一十-龥个]|建[一十-龥个]|跑[一十-龥下]|编[一十-龥个]|干活|开工|动手/;

// 问句信号。
const QUESTION_RE =
  /(什么|怎么|如何|为什么|为啥|为何|是不是|能不能|可不可以|有没有|请问|想问|多少|哪些|谁(?:来|能|会)|几(?:个|天|时|分钟)|讲讲|说说|解释)/;

// 全体号召信号（「大家好，开始干活」是群呼，该全员参与而非抢答）。
const COLLECTIVE_RE = /大家|各位|所有人|全员|大伙|你们(?:大家|所有人|几个)?|同学们|群里/;

// 先后依赖信号（pipeline vs parallel 的分界）。
const SEQUENCE_RE =
  /先[^。！？!?]{0,20}(?:然后|接着|再|之后|最后)|然后|接着|之后|最后|基于(?:这个|这些|上述|它|其)|据此|在此基础上|完成(?:后|之后)/;

export interface IntentClass {
  intent: ChatIntent;
  /** 命中的信号词（可解释性 / 测试断言用）。 */
  signal: string;
}

/**
 * 意图分类（启发式，优先级：寒暄 > 讨论 > 任务 > 问答 > 兜底任务）。
 * 误判问答/任务的代价很低（都路由到对口专家），误判广播的代价很高，
 * 所以默认保守：拿不准就当任务走「专家应答」单人路径。
 */
export function classifyChatIntent(text: string): IntentClass {
  const t = (text ?? '').trim();
  if (!t) return { intent: 'task', signal: 'empty' };
  if (isCasualChat(t)) return { intent: 'casual', signal: 'casual-line' };
  const discuss = DISCUSS_RE.exec(t);
  if (discuss) return { intent: 'discussion', signal: discuss[0] };
  const task = STRONG_TASK_RE.exec(t);
  if (task) return { intent: 'task', signal: task[0] };
  const q = QUESTION_RE.exec(t);
  if (q && (/[?？]\s*$/.test(t) || t.length <= 60)) return { intent: 'question', signal: q[0] };
  return { intent: 'task', signal: 'fallback' };
}

// ─── 专长匹配（agent fit 打分）──────────────────────────────────────────────

/** 领域词库：消息命中某组 + 成员简介命中同组 → 强信号（对口）。 */
const SPECIALTY_LEXICON: ReadonlyArray<{ kws: readonly string[] }> = [
  { kws: ['产品', '需求', 'prd', '用户故事', '路线图', '立项', '原型', '场景'] },
  { kws: ['设计', 'ui', 'ux', '视觉', '交互', '配色', '排版', 'logo', '图标', '界面', '样式', '海报'] },
  { kws: ['前端', '页面', '组件', 'react', 'vue', 'css', 'h5', '小程序', 'web', '浏览器'] },
  { kws: ['后端', '接口', 'api', '服务', '数据库', 'sql', '微服务', '架构', '部署', '服务器', '性能', '并发'] },
  { kws: ['代码', '开发', '实现', '编程', '重构', 'bug', '修复', '调试', '脚本', '编译', '仓库', 'git'] },
  { kws: ['测试', 'qa', '用例', '质量', '回归', '缺陷', '验收', '冒烟'] },
  { kws: ['文案', '文章', '写作', '推文', '内容', '标题', '润色', '翻译', '英文', '英语', '本地化', '文档', '校对'] },
  { kws: ['数据', '报表', '分析', '统计', '指标', '看板', 'bi', '可视化', '图表', '漏斗'] },
  { kws: ['学情', '诊断', '成绩', '错题', '知识点', '课程', '练习', '复习', '课堂', '作业'] },
  { kws: ['视频', '剪辑', '分镜', '配音', '动画', '字幕'] },
  { kws: ['法务', '合同', '合规', '协议', '知识产权', '条款'] },
  { kws: ['财务', '预算', '成本', '报价', '核算', '发票'] },
  { kws: ['调研', '研究', '文献', '综述', '竞品', '行业'] },
];

/** CJK 连续段切二元组（bag-of-bigrams 相似度，封顶防刷分）。 */
function cjkBigrams(s: string): Set<string> {
  const out = new Set<string>();
  for (const run of s.match(/[一-龥]{2,}/g) ?? []) {
    for (let i = 0; i + 1 < run.length && out.size < 48; i++) out.add(run.slice(i, i + 2));
  }
  return out;
}

/**
 * 成员对一段文本的对口度（纯函数）：领域词组命中 +3/组（消息与简介同组），
 * 名字出现在消息 +4，CJK 二元组重叠 +1/个（封顶 4）。0 = 无信号。
 */
export function scoreAgentFit(text: string, agent: StrategyAgent): number {
  const g = (text ?? '').toLowerCase();
  const bag = `${agent.name ?? ''} ${agent.soul ?? ''}`.toLowerCase();
  if (!g.trim() || !bag.trim()) return 0;
  let score = 0;
  for (const { kws } of SPECIALTY_LEXICON) {
    if (!kws.some((k) => g.includes(k))) continue;
    if (kws.some((k) => bag.includes(k))) score += 3;
  }
  const name = (agent.name ?? '').trim();
  if (name.length >= 2 && g.includes(name.toLowerCase())) score += 4;
  let overlap = 0;
  for (const gram of cjkBigrams(g)) {
    if (bag.includes(gram)) overlap++;
  }
  score += Math.min(4, overlap);
  return score;
}

/** 按对口度取前 n 人（稳定排序：同分按花名册顺序；全体 0 分 → 花名册顺序）。 */
export function topAgents(text: string, agents: StrategyAgent[], n: number): StrategyAgent[] {
  const scored = agents.map((a, i) => ({ a, s: scoreAgentFit(text, a), i }));
  scored.sort((x, y) => y.s - x.s || x.i - y.i);
  return scored.slice(0, Math.max(1, n)).map((x) => x.a);
}

// ─── 子任务切分 ─────────────────────────────────────────────────────────────

/**
 * 把消息切成子句（工单粒度）：先按句读切；整条只有一句且较长时退化到逗号切
 * （「先帮我整理需求文档，然后实现登录接口」→ 2 段）。「、」顿号不切（同一
 * 诉求里的并列宾语不拆）。
 */
export function splitTaskClauses(text: string): string[] {
  const t = (text ?? '').trim();
  if (!t) return [];
  let parts = t
    .split(/[。！？!?\n；;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length <= 1 && /[，,]/.test(t) && t.length > 16) {
    parts = t
      .split(/[，,]+/)
      .map((s) => s.trim())
      .filter((s) => s.length >= 2);
  }
  return parts.length > 0 ? parts : [t];
}

// ─── 计划构造 ───────────────────────────────────────────────────────────────

function makeTask(
  agent: StrategyAgent,
  title: string,
  prompt: string,
  dependsOn?: string[],
): ParsedTask {
  return {
    agentId: agent.id,
    title: title.slice(0, 40),
    prompt,
    ...(dependsOn && dependsOn.length > 0 ? { depends_on: dependsOn } : {}),
  };
}

/** 寒暄计划：成员首位单人接话（像普通群聊，一人回一句）。 */
export function casualPlan(text: string, agents: StrategyAgent[]): ChatPlan {
  const host = agents[0];
  return {
    intent: 'casual',
    strategy: 'solo',
    source: 'casual',
    tasks: host ? [makeTask(host, '寒暄应答', text)] : [],
  };
}

/**
 * @ 点名计划：用户即调度者（GrokBot「单点 @ 串行交接」语义）——只派被点名
 * 者，prompt 用剔掉 @token 的清洗文本，跳过一切拆解。
 */
export function mentionPlan(
  userText: string,
  cleanText: string,
  agents: StrategyAgent[],
  mentionedIds: string[],
): ChatPlan {
  const text = (cleanText || userText).trim() || userText;
  const cls = classifyChatIntent(text);
  const valid = new Set(agents.map((a) => a.id));
  const ids = [...new Set(mentionedIds)].filter((id) => valid.has(id));
  const byId = new Map(agents.map((a) => [a.id, a]));
  return {
    // 点名即派活：即便文本是「在吗」也是定向应答，不按寒暄收缩。
    intent: cls.intent === 'casual' ? 'task' : cls.intent,
    strategy: ids.length > 1 ? 'parallel' : 'expert',
    source: 'mention',
    tasks: ids.map((id) => makeTask(byId.get(id) ?? { id, name: id }, '定向派活', text)),
  };
}

/** 全员参与计划（群呼号召 / @ 未命中回落）：人人参与，各按职责视角回应。 */
export function allHandsPlan(text: string, agents: StrategyAgent[]): ChatPlan {
  return {
    intent: 'task',
    strategy: 'all-hands',
    source: 'heuristic',
    tasks: agents.map((a) =>
      makeTask(a, '参与', `${text}\n\n请以「${a.name}」的职责视角回应，保持简短。`),
    ),
  };
}

/**
 * 启发式策略矩阵计划（LLM 不可用时的兜底主力）：
 *   寒暄 → solo；讨论 → roundtable（2-3 人观点）；任务/问答 →
 *   群呼 all-hands / 多子句多对口 → pipeline（有先后词）或 parallel /
 *   其余 → expert 单人（无信号也不广播）。
 */
export function heuristicPlan(text: string, agents: StrategyAgent[]): ChatPlan {
  const t = (text ?? '').trim();
  if (agents.length === 0) {
    return { intent: 'task', strategy: 'expert', source: 'heuristic', tasks: [] };
  }
  if (isCasualChat(t)) return casualPlan(t, agents);

  const cls = classifyChatIntent(t);
  const intent: ChatIntent = cls.intent === 'casual' ? 'task' : cls.intent;

  if (agents.length === 1) {
    return {
      intent,
      strategy: 'expert',
      source: 'heuristic',
      tasks: [makeTask(agents[0], intent === 'question' ? '应答' : '执行', t)],
    };
  }

  if (intent === 'discussion') {
    const picks = topAgents(t, agents, Math.min(3, agents.length));
    return {
      intent,
      strategy: 'roundtable',
      source: 'heuristic',
      tasks: picks.map((a) =>
        makeTask(
          a,
          '发表观点',
          `${t}\n\n请以「${a.name}」的角色给出简短观点（不超过 3 点），不要重复他人说过的角度。`,
        ),
      ),
    };
  }

  // 任务 / 问答
  if (COLLECTIVE_RE.test(t)) return allHandsPlan(t, agents);

  const clauses = splitTaskClauses(t);
  const picks = clauses.map((c) => topAgents(c, agents, 1)[0]);
  const distinct = [...new Set(picks.map((a) => a.id))];
  if (distinct.length >= 2) {
    // 多工种拆解：同一执行者的子句合并为一张工单；有先后词 → 流水线链式依赖。
    const seq = SEQUENCE_RE.test(t);
    const byAgent = new Map<string, string[]>();
    picks.forEach((a, i) => {
      byAgent.set(a.id, [...(byAgent.get(a.id) ?? []), clauses[i]]);
    });
    const tasks: ParsedTask[] = [];
    let prevId: string | null = null;
    for (const [id, cs] of byAgent) {
      const agent = agents.find((x) => x.id === id) ?? { id, name: id };
      tasks.push(
        makeTask(
          agent,
          cs[0].slice(0, 20),
          `${cs.join('。')}\n\n（这是整体需求拆分给你的部分。整体需求：${t}）`,
          seq && prevId ? [prevId] : undefined,
        ),
      );
      prevId = id;
    }
    return {
      intent,
      strategy: seq ? 'pipeline' : 'parallel',
      source: 'heuristic',
      tasks,
    };
  }

  const best = picks[0];
  return {
    intent,
    strategy: 'expert',
    source: 'heuristic',
    tasks: [makeTask(best, intent === 'question' ? '应答' : '执行', t)],
  };
}

// ─── 计划可解释性（派发卡 reason）───────────────────────────────────────────

/** 派发卡 reason：意图 · 策略 · 参与者 · 计划来源（用户可见的路由解释）。 */
export function describePlan(plan: ChatPlan, nameOf: (id: string) => string): string {
  const names = [...new Set(plan.tasks.map((t) => t.agentId))].map(nameOf);
  if (plan.source === 'mention') {
    return `用户点名 @${names.join('、')}，定向派发（跳过意图拆解）`;
  }
  const who =
    names.length > 1
      ? `${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 人` : ''}`
      : (names[0] ?? '');
  const sourceNote =
    plan.source === 'llm' ? '· 编排者拆解' : plan.source === 'heuristic' ? '· 策略矩阵路由' : '';
  return `意图：${INTENT_LABELS[plan.intent]} · 策略：${STRATEGY_LABELS[plan.strategy]}（${who}）${sourceNote}`;
}
