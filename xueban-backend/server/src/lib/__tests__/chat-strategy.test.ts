/**
 * chat-strategy.test.ts — 群聊意图识别 + 策略矩阵纯函数单测（2026-11-07）。
 *
 * 覆盖（全部离线、零 IO）：
 *   - classifyChatIntent 优先级（寒暄 > 讨论 > 任务 > 问答 > 兜底任务）；
 *   - scoreAgentFit 专长打分 / topAgents 稳定排序；
 *   - splitTaskClauses 子句切分（句读切 + 逗号退化切、顿号不切）；
 *   - heuristicPlan 策略矩阵全矩阵（solo/expert/pipeline/parallel/
 *     roundtable/all-hands + 无信号不广播）；
 *   - mentionPlan / casualPlan / allHandsPlan 单独计划构造；
 *   - describePlan 派发卡 reason 文案。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/lib/__tests__/chat-strategy.test.ts
 */
import { describe, expect, it } from 'vitest';
import {
  allHandsPlan,
  casualPlan,
  classifyChatIntent,
  describePlan,
  heuristicPlan,
  mentionPlan,
  scoreAgentFit,
  splitTaskClauses,
  topAgents,
  type ChatPlan,
  type StrategyAgent,
} from '../chat-strategy';

/** 四工种花名册：产品 / 开发 / 设计 / 运营（专长词组互不重叠）。 */
const AGENTS: StrategyAgent[] = [
  { id: 'pm', name: '策划喵', soul: '产品 需求 用户故事 路线图' },
  { id: 'dev', name: '码农鸡', soul: '开发 代码 后端 接口 数据库 sql 性能 bug 修复' },
  { id: 'design', name: '设计鹅', soul: '设计 ui 界面 海报 配色' },
  { id: 'ops', name: '运营鹅', soul: '运营 发布 推广 增长' },
];
const nameOf = (id: string): string => AGENTS.find((a) => a.id === id)?.name ?? id;

// ── 意图分类 ────────────────────────────────────────────────────────────────

describe('classifyChatIntent — 意图优先级', () => {
  it('整句寒暄 → casual', () => {
    expect(classifyChatIntent('hi')).toMatchObject({ intent: 'casual', signal: 'casual-line' });
  });

  it('讨论信号优先于任务词（「帮我选」是征求意见不是派活）', () => {
    const r = classifyChatIntent('帮我选 A 还是 B');
    expect(r.intent).toBe('discussion');
    expect(r.signal).toBe('帮我选');
  });

  it('强任务动词 → task', () => {
    const r = classifyChatIntent('帮我写个脚本');
    expect(r.intent).toBe('task');
    expect(r.signal).toBe('帮我');
  });

  it('问句（带 ? 或短文本）→ question', () => {
    const r = classifyChatIntent('SQL 查询慢是什么原因？');
    expect(r.intent).toBe('question');
    expect(r.signal).toBe('什么');
  });

  it('无任何信号 → 兜底 task（signal=fallback）', () => {
    expect(classifyChatIntent('登录页白屏')).toMatchObject({ intent: 'task', signal: 'fallback' });
  });

  it('空文本 → task（signal=empty）', () => {
    expect(classifyChatIntent('')).toMatchObject({ intent: 'task', signal: 'empty' });
  });
});

// ── 专长打分 ────────────────────────────────────────────────────────────────

describe('scoreAgentFit / topAgents — 专长匹配', () => {
  it('领域词组同组命中 → 对口者得分更高', () => {
    const text = '帮我修复登录接口的 bug';
    expect(scoreAgentFit(text, AGENTS[1]!)).toBeGreaterThan(scoreAgentFit(text, AGENTS[2]!));
  });

  it('名字出现在消息 → 加分（≥4）', () => {
    expect(scoreAgentFit('让码农鸡看一下', AGENTS[1]!)).toBeGreaterThanOrEqual(4);
    expect(scoreAgentFit('让码农鸡看一下', AGENTS[1]!)).toBeGreaterThan(
      scoreAgentFit('让码农鸡看一下', AGENTS[0]!),
    );
  });

  it('空文本 / 空简介 → 0 分', () => {
    expect(scoreAgentFit('', AGENTS[0]!)).toBe(0);
    expect(scoreAgentFit('随便聊聊', { id: 'x', name: '某人' })).toBe(0);
  });

  it('全体 0 分时 topAgents 按花名册顺序（稳定排序）', () => {
    const picks = topAgents('登录页白屏', AGENTS, 2);
    expect(picks.map((a) => a.id)).toEqual(['pm', 'dev']);
  });
});

// ── 子句切分 ────────────────────────────────────────────────────────────────

describe('splitTaskClauses — 工单粒度切分', () => {
  it('句读（。！？）切多段', () => {
    expect(splitTaskClauses('第一件事。第二件事！第三件事？')).toEqual([
      '第一件事',
      '第二件事',
      '第三件事',
    ]);
  });

  it('单句且 >16 字 → 退化逗号切（先…然后 → 2 段）', () => {
    expect(splitTaskClauses('先帮我整理需求文档，然后实现登录接口')).toEqual([
      '先帮我整理需求文档',
      '然后实现登录接口',
    ]);
  });

  it('短句逗号不切', () => {
    expect(splitTaskClauses('短句，不切')).toEqual(['短句，不切']);
  });

  it('顿号不切（并列宾语保持一张工单）', () => {
    expect(splitTaskClauses('帮我买苹果、香蕉、橙子等水果一共三斤吧')).toEqual([
      '帮我买苹果、香蕉、橙子等水果一共三斤吧',
    ]);
  });
});

// ── 策略矩阵（heuristicPlan 全矩阵）────────────────────────────────────────

describe('heuristicPlan — 策略矩阵', () => {
  it('寒暄 → solo：成员首位单人接话', () => {
    // 寒暄分支委托 casualPlan，source 继承 'casual'（describePlan 对 casual 不加来源注脚）。
    const plan = heuristicPlan('hi', AGENTS);
    expect(plan).toMatchObject({ intent: 'casual', strategy: 'solo', source: 'casual' });
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['pm']);
  });

  it('问答 → expert：最对口 1 人应答（不广播）', () => {
    const plan = heuristicPlan('SQL 查询慢是什么原因？', AGENTS);
    expect(plan).toMatchObject({ intent: 'question', strategy: 'expert' });
    expect(plan.tasks).toHaveLength(1);
    expect(plan.tasks[0]!.agentId).toBe('dev');
    expect(plan.tasks[0]!.title).toBe('应答');
  });

  it('多子句有先后词 → pipeline：链式 depends_on + 自包含 prompt', () => {
    const text = '先帮我整理需求文档，然后实现登录接口';
    const plan = heuristicPlan(text, AGENTS);
    expect(plan.strategy).toBe('pipeline');
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['pm', 'dev']);
    expect(plan.tasks[0]!.depends_on).toBeUndefined();
    expect(plan.tasks[1]!.depends_on).toEqual(['pm']);
    // 每人 prompt = 自己的子句 + 整体需求上下文（自包含，不写「见上文」）。
    expect(plan.tasks[0]!.prompt).toContain('先帮我整理需求文档');
    expect(plan.tasks[0]!.prompt).toContain(`整体需求：${text}`);
    expect(plan.tasks[1]!.prompt).toContain('然后实现登录接口');
    expect(plan.tasks[1]!.prompt).toContain(`整体需求：${text}`);
  });

  it('多子句无先后词 → parallel：互相独立无依赖', () => {
    // 句长 >16 字才触发逗号退化切分（splitTaskClauses 门槛），故用长句。
    const plan = heuristicPlan('帮我整理一下需求文档，帮我设计一张活动海报', AGENTS);
    expect(plan.strategy).toBe('parallel');
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['pm', 'design']);
    for (const t of plan.tasks) expect(t.depends_on).toBeUndefined();
  });

  it('讨论 → roundtable：2-3 人给观点（不是全员）', () => {
    const plan = heuristicPlan('帮我选 A 还是 B', AGENTS);
    expect(plan).toMatchObject({ intent: 'discussion', strategy: 'roundtable' });
    expect(plan.tasks).toHaveLength(3);
    // 全 0 分稳定取花名册前三；运营鹅（第 4 位）不参与。
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['pm', 'dev', 'design']);
    expect(plan.tasks[0]!.prompt).toContain('观点');
  });

  it('全体号召（大家/各位/干活）→ all-hands：全员参与', () => {
    const plan = heuristicPlan('大家好，开始干活', AGENTS);
    expect(plan).toMatchObject({ intent: 'task', strategy: 'all-hands' });
    expect(plan.tasks).toHaveLength(AGENTS.length);
  });

  it('无任何信号 → expert 单人（花名册首位），绝不广播', () => {
    const plan = heuristicPlan('登录页白屏', AGENTS);
    expect(plan.strategy).toBe('expert');
    expect(plan.tasks).toHaveLength(1);
    expect(plan.tasks[0]!.agentId).toBe('pm');
  });

  it('单人群（1 个 agent）任何消息都走单人', () => {
    const plan = heuristicPlan('帮我做个海报再写段文案', AGENTS.slice(3));
    expect(plan.strategy).toBe('expert');
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['ops']);
  });

  it('空成员 → 空工单（调用方防尞回落）', () => {
    const plan = heuristicPlan('hi', []);
    expect(plan.tasks).toEqual([]);
    expect(plan.strategy).toBe('expert');
  });
});

// ── 单独计划构造 ────────────────────────────────────────────────────────────

describe('mentionPlan / casualPlan / allHandsPlan', () => {
  it('mentionPlan 单人 → expert、多人 → parallel，prompt 用清洗文本', () => {
    const one = mentionPlan('@码农鸡 帮我看看', '帮我看看', AGENTS, ['dev']);
    expect(one).toMatchObject({ intent: 'task', strategy: 'expert', source: 'mention' });
    expect(one.tasks[0]!.prompt).toBe('帮我看看');

    const two = mentionPlan('@策划喵 @设计鹅 一起看', '一起看', AGENTS, ['pm', 'design']);
    expect(two.strategy).toBe('parallel');
    expect(two.tasks.map((t) => t.agentId)).toEqual(['pm', 'design']);
  });

  it('mentionPlan 点名即派活：寒暄文本也按 task 定向应答', () => {
    const plan = mentionPlan('@码农鸡 在吗', '在吗', AGENTS, ['dev']);
    expect(plan.intent).toBe('task');
    expect(plan.strategy).toBe('expert');
    expect(plan.tasks[0]!.prompt).toBe('在吗');
  });

  it('mentionPlan 过滤非花名册 id', () => {
    const plan = mentionPlan('@幽灵 你好', '你好', AGENTS, ['ghost']);
    expect(plan.tasks).toEqual([]);
  });

  it('casualPlan → 首位接话', () => {
    const plan = casualPlan('收到', AGENTS);
    expect(plan).toMatchObject({ intent: 'casual', strategy: 'solo', source: 'casual' });
    expect(plan.tasks.map((t) => t.agentId)).toEqual(['pm']);
  });

  it('allHandsPlan → 人人参与、各按职责视角', () => {
    const plan = allHandsPlan('开始干活', AGENTS);
    expect(plan.strategy).toBe('all-hands');
    expect(plan.tasks).toHaveLength(4);
    for (const t of plan.tasks) expect(t.prompt).toContain('职责视角');
  });
});

// ── 派发卡 reason（可解释性）────────────────────────────────────────────────

describe('describePlan — 路由解释文案', () => {
  it('点名来源 → 说明跳过拆解', () => {
    const plan = mentionPlan('@码农鸡 帮我看看', '帮我看看', AGENTS, ['dev']);
    expect(describePlan(plan, nameOf)).toBe('用户点名 @码农鸡，定向派发（跳过意图拆解）');
  });

  it('启发式来源 → 意图 · 策略 · 参与者 · 策略矩阵路由', () => {
    const plan = heuristicPlan('帮我修复登录页的前端 bug', AGENTS);
    expect(describePlan(plan, nameOf)).toBe(
      '意图：任务 · 策略：专家应答（码农鸡）· 策略矩阵路由',
    );
  });

  it('LLM 来源 → 标注编排者拆解；多人折叠显示', () => {
    const plan: ChatPlan = {
      intent: 'task',
      strategy: 'pipeline',
      source: 'llm',
      tasks: [
        { agentId: 'pm', title: 'a', prompt: 'p1' },
        { agentId: 'dev', title: 'b', prompt: 'p2' },
        { agentId: 'design', title: 'c', prompt: 'p3' },
      ],
    };
    const r = describePlan(plan, nameOf);
    expect(r).toBe('意图：任务 · 策略：流水线拆解（策划喵、码农鸡 等 3 人）· 编排者拆解');
  });
});
