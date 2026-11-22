import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { parseStructuredDocument } from '../services/learning-parser';

const GOLDEN = fs.readFileSync(path.resolve(process.cwd(), 'src/__tests__/fixtures/b01-golden.txt'), 'utf8');

describe('B01 真实教材回归基准', () => {
  it('识别第八章实数及 8.1/8.2/8.3，不把页眉第七章当成目录', () => {
    const doc = parseStructuredDocument(GOLDEN);
    expect(doc.chapters).toHaveLength(1);
    expect(doc.chapters[0].title).toContain('实数');
    expect(doc.chapters[0].sections.map((section) => section.title)).toEqual(['本章导学', '平方根', '立方根', '实数及其简单运算']);
    expect(doc.chapters[0].sections.some((section) => section.title.includes('第七章'))).toBe(false);
    expect(doc.blocks.some((block) => block.heading_level === 'section' && block.text.includes('第七章'))).toBe(false);
  });

  it('8.1 原文覆盖 PDF 第 2-9 页，8.2 覆盖第 10-13 页，8.3 覆盖第 14 页以后', () => {
    const doc = parseStructuredDocument(GOLDEN);
    const byTitle = new Map(doc.chapters[0].sections.map((section) => [section.title, section]));
    const pages = (title: string) => {
      const section = byTitle.get(title)!;
      return section.blocks.map((block) => block.page).filter((page): page is number => page !== null);
    };
    expect(Math.min(...pages('平方根'))).toBe(2);
    expect(Math.max(...pages('平方根'))).toBeGreaterThanOrEqual(9);
    expect(Math.min(...pages('立方根'))).toBe(10);
    expect(Math.min(...pages('实数及其简单运算'))).toBeGreaterThanOrEqual(14);
  });
});