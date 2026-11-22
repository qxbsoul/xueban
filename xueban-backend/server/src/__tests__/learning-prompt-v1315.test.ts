import { describe, expect, it } from 'vitest';
import { buildOutlinePrompt, buildSectionPrompt } from '../prompts/learning-parse';

const tableBlock = {
  id: 'table-1', type: 'table', text: '<table><tr><td>1</td></tr></table>',
  line: 275, page: 6, status: 'needs_review', table_status: 'structure_validated',
};

describe('待校验表格进入教学数据入口', () => {
  it('目录提示词明确标注表格未核验', () => {
    const prompt = buildOutlinePrompt({ courseTitle: '测试', subject: '数学', grade: '七年级', blocks: [tableBlock] });
    expect(prompt).toContain('表格未完成内容核验');
    expect(prompt).toContain('不得当作已确认事实');
  });
  it('小节提示词保留表格原文但明确不确定', () => {
    const prompt = buildSectionPrompt({
      courseTitle: '测试', chapterTitle: '第八章', sectionTitle: '8.1', blocks: [tableBlock],
    });
    expect(prompt).toContain('待核对线索');
    expect(prompt).not.toContain('√0.0625');
  });
});