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
  type AgentCall,
  type AgentModelConfig,
} from '../services/learning-agent';
import type { LearningSourceBlock } from '../storage/learning';

function block(id: string, text: string, line: number, type: LearningSourceBlock['block_type'] = 'paragraph'): LearningSourceBlock {
  return {
    id, owner_id: 'u', course_id: 'c', material_id: 'm', block_type: type, ordinal: line,
    text, markdown: text, page: null, line_start: line, line_end: line, bbox_json: '{}',
    status: 'parsed', created_at: 1,
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
