import { describe, expect, it } from 'vitest';
import { buildProgramOutline } from '../services/learning-agent';
import type { LearningSourceBlock } from '../storage/learning';

function block(id: string, heading: 'chapter' | 'section' | null, text: string, page = 1): LearningSourceBlock {
  return {
    id, owner_id: 'o', course_id: 'c', material_id: 'm', block_type: heading ? 'heading' : 'paragraph',
    ordinal: 1, text, markdown: text, page, line_start: 1, line_end: 1, bbox_json: '{}',
    status: 'parsed', heading_level: heading, created_at: 1,
  } as LearningSourceBlock;
}

describe('通用教材章节候选校验', () => {
  it('过滤统计数字/连续数值伪小节，合并重复章标题，保留 12.1/12.2', () => {
    const blocks = [
      block('ch1', 'chapter', '第十二章 数据的收集、整理与描述', 1),
      block('s1', 'section', '12.1 统计调查', 2),
      block('p1', null, '统计调查需要收集数据。', 2),
      block('bad1', 'section', '8.3', 3),
      block('bad2', 'section', '16.7', 3),
      block('bad3', 'section', '4.0 4.3 4.6 4.9 5.2', 4),
      block('p2', null, '这是统计图表中的连续数值，不是教材小节标题。', 4),
      block('ch2', 'chapter', '第十二章 数据的收集、整理与描述 155', 5),
      block('s2', 'section', '12.2 用统计图描述数据', 5),
      block('p3', null, '条形图、扇形图和折线图用于描述数据。', 5),
    ];
    const outline = buildProgramOutline(blocks);
    expect(outline).toHaveLength(1);
    expect(outline[0].title).toContain('数据的收集');
    const titles = outline[0].sections.map((section) => section.title);
    expect(titles).toContain('12.1 统计调查');
    expect(titles).toContain('12.2 用统计图描述数据');
    expect(titles.some((title) => /^8\.3|^16\.7|^4\.0 4\.3/.test(title))).toBe(false);
  });
});