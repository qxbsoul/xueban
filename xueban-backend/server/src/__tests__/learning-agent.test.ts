/**
 * 教材 Agent 管线测试（2026-11-16）。
 * 使用模拟 Agent 接口，验证程序侧校验、分批、重试、失败处理；
 * 不声称这是真实模型效果验证。
 */
import { describe, expect, it } from 'vitest';
import {
  LearningAgentError,
  parseTextbookWithAgent,
  validateAgentGraph,
  chunkSourceBlocks,
  repairFallbackPages,
  validateLessonDesign,
  type AgentCall,
  type AgentModelConfig,
  type LessonDesignStats,
} from '../services/learning-agent';
import type { LearningSourceBlock } from '../storage/learning';

function block(id: string, text: string, line: number, type: LearningSourceBlock['block_type'] = 'paragraph', heading_level: LearningSourceBlock['heading_level'] = null): LearningSourceBlock {
  return {
    id, owner_id: 'u', course_id: 'c', material_id: 'm', block_type: type, ordinal: line,
    text, markdown: text, page: null, line_start: line, line_end: line, bbox_json: '{}',
    status: 'parsed', heading_level, created_at: 1,
  };
}

function idsIn(prompt: string): string[] {
  return [...new Set([...prompt.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]))];
}

const config: AgentModelConfig = {
  provider: 'deepseek', apiKey: 'test-key', model: 'test-model', maxAttempts: 2, timeoutMs: 100,
};

function outlineCall(sectionBlockIds: string[]): AgentCall {
  return async function* (_provider, input) {
    if (input.userMessage.includes('"chapters"')) {
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          course: { title: '数学', subject: '数学', grade: '七年级', book_info: '测试教材' },
          chapters: [{ title: '第一章', summary: '章概述', sections: [{ title: '1.1 小节', summary: '节概述', block_ids: sectionBlockIds }] }],
        }),
      };
      return;
    }
    if (input.userMessage.includes('"units"')) {
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          units: [{
            title: '概念 A', point_titles: ['概念 A'],
            objectives: ['理解概念 A'], introduction: '',
            explanation: '从具体例子出发，说明概念 A 的含义和成立条件，并归纳一般结论。该内容用于验证教学单元解析。',
            example: { problem: '概念 A 的例子。', thinking: '依据定义。', steps: [{ text: '完成计算。', reason: '依据定义。' }], answer: '答案', warning: '注意条件。' },
            misconceptions: ['忽略条件。'], checks: [{ question: '概念 A 的例子说明什么？', answer: '说明概念 A 的含义。' }],
            summary: '总结概念 A。', relations: { prerequisite: [], followup: [], related: [] },
          }],
        }),
      };
      return;
    }
    const ids = idsIn(input.userMessage);
    yield {
      type: 'text-delta',
      text: JSON.stringify({
        objectives: ['理解概念'],
        knowledge_points: [{
          title: '概念 A', explanation: '原文解释', concepts: ['概念'], formulas: ['a=b'], methods: [],
          prerequisites: [], block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false,
        }],
      }),
    };
  };
}

describe('Agent 教材解析管线（模拟模型）', () => {
  it('程序提供块 ID，Agent 返回章节/知识点后通过校验', async () => {
    const blocks = [block('11111111-1111-4111-8111-111111111111', '标题', 1, 'heading'), block('22222222-2222-4222-8222-222222222222', '正文概念', 2)];
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config, callImpl: outlineCall(blocks.map((item) => item.id)),
    });
    expect(result.chapters).toHaveLength(1);
    expect(result.chapters[0].sections[0].knowledge_points[0].title).toBe('概念 A');
    const sourceIds = result.chapters[0].sections[0].knowledge_points[0].block_ids;
    expect(sourceIds.length).toBeGreaterThan(0);
    expect(sourceIds.every((id) => blocks.some((item) => item.id === id))).toBe(true);
    expect(result.validation_errors).toEqual([]);
  });

  it('模型把思考包在 sf:thinking 中时仍能解析真正 JSON', async () => {
    const blocks = [block('12121212-1212-4212-8212-121212121212', '正文', 1)];
    const wrapped: AgentCall = async function* (provider, input) {
      const result = outlineCall(blocks.map((item) => item.id))(provider, input);
      for await (const chunk of result) {
        if (chunk.type === 'text-delta') {
          yield { type: 'text-delta', text: `<sf:thinking>示例 {bad}</sf:thinking>${chunk.text}` };
        } else {
          yield chunk;
        }
      }
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config, callImpl: wrapped,
    });
    expect(result.chapters.length).toBeGreaterThan(0);
  });

  it('模型首次返回非法 JSON，重试后成功', async () => {
    let calls = 0;
    const flaky: AgentCall = async function* (provider, input) {
      calls += 1;
      if (calls === 1) {
        yield { type: 'text-delta', text: 'not json' };
        return;
      }
      yield* outlineCall(idsIn(input.userMessage))(provider, input);
    };
    const blocks = [block('33333333-3333-4333-8333-333333333333', '正文', 1)];
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 2 }, callImpl: flaky,
    });
    expect(calls).toBeGreaterThanOrEqual(2);
    expect(result.chapters.length).toBeGreaterThan(0);
  });

  it('知识点引用不存在的块 ID 会被丢弃并记录错误', async () => {
    const blocks = [block('44444444-4444-4444-8444-444444444444', '正文', 1)];
    const badCall: AgentCall = async function* (_provider, input) {
      if (input.userMessage.includes('"chapters"')) {
        yield {
          type: 'text-delta',
          text: JSON.stringify({
            chapters: [{
              title: '章',
              sections: [
                { title: '节一', block_ids: [blocks[0].id] },
                { title: '节二', block_ids: [blocks[0].id] },
              ],
            }],
          }),
        };
        return;
      }
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          objectives: [],
          knowledge_points: [{
            title: '无依据知识点', explanation: '编造', block_ids: ['99999999-9999-4999-8999-999999999999'],
            origin: 'textbook', confidence: 0.9,
          }],
        }),
      };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config, callImpl: badCall,
    });
    expect(result.chapters[0].sections[0].knowledge_points).toHaveLength(0);
    expect(result.validation_errors.join(' ')).toContain('没有有效原文块引用');
  });

  it('重复知识点和循环前置关系会被标记', async () => {
    const blocks = [block('55555555-5555-4555-8555-555555555555', '正文', 1)];
    const cyclic: AgentCall = async function* (_provider, input) {
      if (input.userMessage.includes('"chapters"')) {
        yield {
          type: 'text-delta',
          text: JSON.stringify({
            chapters: [{ title: '章', sections: [{ title: '节', block_ids: [blocks[0].id] }] }],
          }),
        };
        return;
      }
      const point = (title: string, prerequisite: string) => ({
        title, explanation: '解释', prerequisites: [prerequisite], block_ids: [blocks[0].id],
        origin: 'textbook', confidence: 0.9, needs_review: false,
      });
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          objectives: [],
          knowledge_points: [point('A', 'B'), point('B', 'A'), point('A', 'B')],
        }),
      };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config, callImpl: cyclic,
    });
    expect(result.validation_errors.join(' ')).toContain('循环');
  });

  it('全局校验能识别重复知识点', () => {
    const point = (title: string) => ({
      title, explanation: '解释', concepts: [], formulas: [], methods: [], prerequisites: [],
      block_ids: ['b'], origin: 'textbook' as const, status: 'parsed' as const, confidence: 0.9,
    });
    const graph = validateAgentGraph([
      { title: '章', summary: '', sections: [
        { title: '节一', summary: '', block_ids: ['b'], objectives: [], knowledge_points: [point('重复概念')], status: 'parsed' },
        { title: '节二', summary: '', block_ids: ['b'], objectives: [], knowledge_points: [point('重复概念')], status: 'parsed' },
      ] },
    ]);
    expect(graph.duplicates).toContain('重复概念');
  });

  it('长教材会分批调用 Agent', async () => {
    const blocks = Array.from({ length: 20 }, (_, index) => (
      block(`66666666-6666-4666-8666-${String(index).padStart(12, '0')}`, '长文本'.repeat(2000), index + 1)
    ));
    let calls = 0;
    const counting: AgentCall = async function* (provider, input) {
      calls += 1;
      yield* outlineCall(blocks.map((item) => item.id))(provider, input);
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '长教材', subject: '数学', grade: '七年级', blocks,
      config, callImpl: counting,
    });
    expect(calls).toBeGreaterThan(2);
    expect(result.batch_count).toBeGreaterThan(2);
  });

  it('调用失败会抛出错误，不伪造解析成功', async () => {
    const blocks = [block('77777777-7777-4777-8777-777777777777', '正文', 1)];
    const failing: AgentCall = async function* () {
      throw new LearningAgentError('AGENT_FAILED', '模型不可用');
    };
    await expect(parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1 }, callImpl: failing,
    })).rejects.toBeInstanceOf(LearningAgentError);
  });

  it('模型超时会失败并带 TIMEOUT 错误', async () => {
    const blocks = [block('99999999-9999-4999-8999-999999999999', '正文', 1)];
    const hanging: AgentCall = async function* () {
      await new Promise(() => undefined);
      yield { type: 'end' };
    };
    await expect(parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1, timeoutMs: 10 }, callImpl: hanging,
    })).rejects.toMatchObject({ code: 'TIMEOUT' });
  });

  it('没有 API Key 时立即失败', async () => {
    const blocks = [block('88888888-8888-4888-8888-888888888888', '正文', 1)];
    await expect(parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config: { provider: 'deepseek', model: 'test' }, callImpl: outlineCall([blocks[0].id]),
    })).rejects.toMatchObject({ code: 'NO_API_KEY' });
  });
});


describe('教学学案结构校验', () => {
  const blockId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const allowed = new Set([blockId]);

  function rawDesign(overrides: Record<string, unknown> = {}) {
    return {
      design_status: 'complete',
      objectives: [
        { text: '理解相反数的意义', kind: 'knowledge' },
        { text: '能用一个例子说明相反数相加为零', kind: 'application' },
      ],
      preparation: [{ text: '回忆数轴上的正负方向', source: 'derived', block_ids: [blockId] }],
      introduction: { text: '从教材的温度计问题出发，观察相反方向的变化。', source: 'textbook', block_ids: [blockId] },
      teaching_flow: [{
        kind: 'concept', title: '相反数的含义',
        body: '在数轴上，位于原点两侧且到原点距离相等的两个数，互为相反数。',
        bullets: [], formulas: ['$a + (-a) = 0$'], source: 'textbook', block_ids: [blockId],
      }],
      examples: [{
        title: '教材例题', problem: '求 5 的相反数。', steps: ['5 的相反数是 -5。'],
        answer: '-5', source: 'textbook', block_ids: [blockId],
      }],
      misconceptions: [{ text: '不要漏写负号。', source: 'derived', block_ids: [blockId] }],
      activities: [{ prompt: '用自己的话解释相反数，并举出一个数轴上的例子。', purpose: '训练概念解释', source: 'derived', block_ids: [blockId] }],
      summary: { text: '相反数在数轴上关于原点对称。', source: 'textbook', block_ids: [blockId] },
      self_check: [{ question: '7 的相反数是多少？', answer_hint: '在数轴上找原点另一侧。', source: 'derived', block_ids: [blockId] }],
      ...overrides,
    };
  }

  it('数学概念与例题学案通过完整校验', () => {
    const result = validateLessonDesign(rawDesign(), allowed);
    expect(result.status).toBe('complete');
    expect(result.design.teaching_flow).toHaveLength(1);
    expect(result.design.examples[0].source).toBe('textbook');
  });

  it('只有知识点罗列、缺少核心学习时标记 needs_review', () => {
    const result = validateLessonDesign(rawDesign({ teaching_flow: [] }), allowed);
    expect(result.status).toBe('needs_review');
    expect(result.errors.some((error) => error.includes('核心知识'))).toBe(true);
  });

  it('例题没有来源或原文引用时不能算完整', () => {
    const result = validateLessonDesign(rawDesign({
      examples: [{ title: '补充例题', problem: '计算 -3 + 3。', steps: [], answer: '0', source: 'needs_review', block_ids: [] }],
    }), allowed);
    expect(result.status).not.toBe('complete');
    expect(result.errors.some((error) => error.includes('例题'))).toBe(true);
  });

  it('核心讲解较长时不会被固定字数截断', () => {
    const longBody = '在数轴上，位于原点两侧且到原点距离相等的两个数互为相反数。'.repeat(40);
    const result = validateLessonDesign(rawDesign({
      teaching_flow: [{
        kind: 'concept', title: '长讲解', body: longBody, bullets: [],
        formulas: [], source: 'textbook', block_ids: [blockId],
      }],
    }), allowed);
    expect(result.status).toBe('complete');
    expect(result.design.teaching_flow[0].body.length).toBeGreaterThan(180);
  });

  it('空泛学习活动会被识别', () => {
    const result = validateLessonDesign(rawDesign({
      activities: [{ prompt: '认真阅读', purpose: '', source: 'derived', block_ids: [blockId] }],
    }), allowed);
    expect(result.status).not.toBe('complete');
    expect(result.errors.some((error) => error.includes('空泛'))).toBe(true);
  });
});


describe('输出截断恢复', () => {
  it('首次 JSON 被截断时拆分原文片段重试', async () => {
    const blocks = [block('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '正文内容', 1)];
    let sectionCalls = 0;
    const call: AgentCall = async function* (_provider, input) {
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: blocks.map((item) => item.id) }] }] }) };
        return;
      }
      sectionCalls += 1;
      if (sectionCalls === 1) {
        yield { type: 'text-delta', text: '{"objectives":["理解内容"],"knowledge_points":[{"title":"截断' };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: ['理解内容'],
        knowledge_points: [{ title: '内容', explanation: '正文内容', concepts: [], formulas: [], methods: [], prerequisites: [], block_ids: [blocks[0].id], origin: 'textbook', confidence: 0.9, needs_review: false }],
      }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks, config: { ...config, maxAttempts: 2 }, callImpl: call,
    });
    expect(result.chapters[0].sections[0].knowledge_points[0].title).toBe('内容');
    expect(sectionCalls).toBeGreaterThanOrEqual(3);
  });
});


describe('长原文与分段任务', () => {
  it('超长原文块按 offset 覆盖全部字符，没有未处理区间', () => {
    const text = '甲'.repeat(15_000);
    const chunks = chunkSourceBlocks([block('cccccccc-cccc-4ccc-8ccc-cccccccccccc', text, 1)], 6_000);
    const segments = chunks.flatMap((chunk) => chunk.segments);
    expect(segments[0].start).toBe(0);
    expect(segments[segments.length - 1].end).toBe(text.length);
    for (let index = 0; index < segments.length - 1; index += 1) {
      expect(segments[index + 1].start).toBeLessThanOrEqual(segments[index].end);
    }
    const covered = new Set<number>();
    segments.forEach((segment) => {
      for (let offset = segment.start; offset < segment.end; offset += 1) covered.add(offset);
    });
    expect(covered.size).toBe(text.length);
  });

  it('长小节拆成多个片段后知识点合并不丢失', async () => {
    const text = '正文'.repeat(6_000);
    const blocks = [block('abababab-abab-4bab-8bab-abababababab', text, 1)];
    const fragmentCall: AgentCall = async function* (_provider, input) {
      const ids = idsIn(input.userMessage);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      const range = /字符(\d+)-\d+/.exec(input.userMessage);
      const start = range ? range[1] : '0';
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: ['目标'],
        knowledge_points: [{
          title: `片段知识点 ${start}`, explanation: '解释', concepts: [], formulas: [], methods: [],
          prerequisites: [], block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false,
        }],
      }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1, sectionConcurrency: 1 }, callImpl: fragmentCall,
    });
    expect(result.chapters[0].sections[0].knowledge_points.length).toBeGreaterThanOrEqual(2);
    expect(result.coverage?.points).toBeGreaterThanOrEqual(2);
    expect(result.coverage?.unprocessed_chars).toBe(0);
  });

  it('一个小节超过 4 个知识点时不会按固定数量截断', async () => {
    const blocks = [block('dddddddd-dddd-4ddd-8ddd-dddddddddddd', '很长的小节原文', 1)];
    const fivePoints: AgentCall = async function* (_provider, input) {
      const ids = idsIn(input.userMessage);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: ['目标'],
        knowledge_points: Array.from({ length: 5 }, (_, index) => ({
          title: `知识点 ${index + 1}`, explanation: '解释', concepts: [], formulas: [], methods: [],
          prerequisites: [], block_ids: [blocks[0].id], origin: 'textbook', confidence: 0.9, needs_review: false,
        })),
      }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks, config: { ...config, maxAttempts: 1 }, callImpl: fivePoints,
    });
    expect(result.chapters[0].sections[0].knowledge_points).toHaveLength(5);
    expect(result.coverage?.points).toBe(5);
  });

  it('超时传给 provider 的 signal 会被 abort，并记录取消状态', async () => {
    const blocks = [block('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', '正文', 1)];
    const stats: import('../services/learning-agent').AgentCallStat[] = [];
    const hanging: AgentCall = async function* (_provider, input) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      expect(input.signal?.aborted).toBe(true);
      yield { type: 'text-delta', text: '{}' };
    };
    await expect(parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks,
      config: { ...config, timeoutMs: 50, maxAttempts: 1, onCall: (stat) => stats.push(stat) },
      callImpl: hanging,
    })).rejects.toMatchObject({ code: 'TIMEOUT' });
    expect(stats.some((stat) => stat.aborted)).toBe(true);
    expect(stats.every((stat) => stat.task.length > 0)).toBe(true);
  });

  it('学案某一个部分失败时保留其他成功部分', async () => {
    const blocks = [block('ffffffff-ffff-4fff-8fff-ffffffffffff', '相反数正文', 1)];
    const designCall: AgentCall = async function* (_provider, input) {
      const ids = idsIn(input.userMessage);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      if (input.userMessage.includes('"knowledge_points"')) {
        yield { type: 'text-delta', text: JSON.stringify({
          objectives: ['理解相反数'],
          knowledge_points: [{ title: '相反数', explanation: '相反数相加为零', concepts: [], formulas: [], methods: [], prerequisites: [], block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false }],
        }) };
        return;
      }
      if (input.userMessage.includes('"teaching_flow"')) {
        throw new Error('人为让核心学习部分失败');
      }
      if (input.userMessage.includes('"examples"')) {
        yield { type: 'text-delta', text: JSON.stringify({ examples: [{ title: '教材例题', problem: '求 5 的相反数', steps: ['-5'], answer: '-5', source: 'textbook', block_ids: [ids[0]] }], misconceptions: [] }) };
        return;
      }
      if (input.userMessage.includes('"activities"')) {
        yield { type: 'text-delta', text: JSON.stringify({ activities: [{ prompt: '解释相反数并举一个数轴例子', purpose: '训练概念解释', source: 'derived', block_ids: [ids[0]] }], summary: { text: '相反数关于原点对称', source: 'textbook', block_ids: [ids[0]] }, self_check: [] }) };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({ objectives: [{ text: '理解相反数', kind: 'knowledge' }], preparation: [], introduction: { text: '从数轴观察', source: 'textbook', block_ids: [ids[0]] } }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1, sectionConcurrency: 1 }, callImpl: designCall, generateLessonDesigns: true,
    });
    const section = result.chapters[0].sections[0];
    expect(section.lesson_design?.objectives.length).toBeGreaterThan(0);
    expect(section.lesson_design?.examples.length).toBeGreaterThan(0);
    expect(section.lesson_design?.teaching_flow.length).toBe(0);
    expect(section.design_status).toBe('needs_review');
    expect((result.lesson_designs?.part_failures || 0)).toBeGreaterThan(0);
  });
});


describe('整本教材目录结构校验', () => {
  it('程序识别到多章多节但 Agent 只返回一节时触发结构兜底', async () => {
    const blocks = [
      block('11111111-1111-4111-8111-111111111111', '第一章 有理数', 1, 'heading', 'chapter'),
      block('22222222-2222-4222-8222-222222222222', '1.1 正数和负数', 2, 'heading', 'section'),
      block('33333333-3333-4333-8333-333333333333', '正数和负数表示相反意义的量。', 3),
      block('44444444-4444-4444-8444-444444444444', '第二章 整式', 4, 'heading', 'chapter'),
      block('55555555-5555-4555-8555-555555555555', '2.1 整式', 5, 'heading', 'section'),
      block('66666666-6666-4666-8666-666666666666', '整式由数字和字母组成。', 6),
    ];
    const collapsed: AgentCall = async function* (_provider, input) {
      const ids = idsIn(input.userMessage);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '整本教材', sections: [{ title: '整本教材', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: ['目标'], knowledge_points: [{
          title: '知识点', explanation: '解释', concepts: [], formulas: [], methods: [], prerequisites: [],
          block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false,
        }],
      }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '数学', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1, sectionConcurrency: 1 }, callImpl: collapsed,
    });
    expect(result.chapters.length).toBeGreaterThanOrEqual(2);
    expect(result.chapters.flatMap((chapter) => chapter.sections).length).toBeGreaterThanOrEqual(2);
    expect(result.validation_errors.some((error) => error.includes('目录结构异常'))).toBe(true);
  });
});


describe('原文块引用不截断', () => {
  it('一个目录小节引用超过 30 个原文块时全部保留', async () => {
    const blocks = Array.from({ length: 35 }, (_, index) => block(
      `99999999-9999-4999-8999-${String(index).padStart(12, '0')}`,
      `正文 ${index}`, index + 1,
    ));
    const call: AgentCall = async function* (_provider, input) {
      const ids = idsIn(input.userMessage);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: ids }] }] }) };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: [],
        knowledge_points: [{ title: '知识点', explanation: '解释', concepts: [], formulas: [], methods: [], prerequisites: [], block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false }],
      }) };
    };
    const result = await parseTextbookWithAgent({
      courseTitle: '测试', subject: '数学', grade: '七年级', blocks,
      config: { ...config, maxAttempts: 1, sectionConcurrency: 1 }, callImpl: call,
    });
    expect(result.chapters[0].sections[0].block_ids).toHaveLength(35);
  });
});


describe('低质量页面视觉修复', () => {
  it('页面图像转录成功后替换 PDF 图片标记并保留页面状态', async () => {
    const audit = { page_records: [{ page: 1, status: 'garbled' as string }], fallback_pages: [1] };
    const call: AgentCall = async function* () {
      yield { type: 'text-delta', text: JSON.stringify({ text: '修复后的公式：$x^2 + 1$', notes: '保留公式' }) };
    };
    const result = await repairFallbackPages({
      text: '页码：1\n![PDF扫描页](pdf://x.pdf?page=1&id=abc)',
      images: [{ kind: 'image', data_url: 'data:image/png;base64,abc', page: 1, is_fallback: true }],
      audit,
    }, { ...config, timeoutMs: 1000 }, call);
    expect(result.repairedPages).toEqual([1]);
    expect(result.text).toContain('修复后的公式');
    expect(result.text).not.toContain('PDF扫描页');
    expect(audit.page_records[0].status).toBe('vision_repaired');
  });

  it('视觉转录失败时明确保留失败页面，不伪造原文', async () => {
    const audit = { page_records: [{ page: 2, status: 'garbled' as string }], fallback_pages: [2] };
    const call: AgentCall = async function* () {
      throw new Error('vision unavailable');
    };
    const result = await repairFallbackPages({
      text: '页码：2\n![PDF扫描页](pdf://x.pdf?page=2&id=def)',
      images: [{ kind: 'image', data_url: 'data:image/png;base64,abc', page: 2, is_fallback: true }],
      audit,
    }, { ...config, timeoutMs: 1000, maxAttempts: 1 }, call);
    expect(result.failedPages).toEqual([2]);
    expect(result.text).toContain('PDF扫描页');
    expect(audit.page_records[0].status).toBe('vision_repair_failed');
  });
});
