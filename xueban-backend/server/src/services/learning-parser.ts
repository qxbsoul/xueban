/**
 * 结构化教材解析（2026-11-15，基础版）。
 *
 * 只做可追溯的结构提取，不做语义补全：
 * - 章 / 节 / 知识点分层；
 * - 每个知识点记录来源行号、原文摘录和块 ID；
 * - 公式、表格、图片保留 Markdown / LaTeX 原文；
 * - 无法可靠识别学习目标或知识点时标记 needs_review，而不是编造。
 */

export type BlockType = 'heading' | 'paragraph' | 'formula' | 'table' | 'image' | 'list';

export interface ParsedBlock {
  key: number;
  block_type: BlockType;
  text: string;
  markdown: string;
  page: number | null;
  line_start: number;
  line_end: number;
  heading_level: 'chapter' | 'section' | 'knowledge' | null;
  status: 'parsed' | 'needs_review';
}

export interface ParsedKnowledgePoint {
  title: string;
  explanation: string;
  concepts: string[];
  formulas: string[];
  methods: string[];
  block_keys: number[];
  status: 'parsed' | 'needs_review';
  confidence: number;
}

export interface ParsedSection {
  title: string;
  line_start: number;
  line_end: number;
  blocks: ParsedBlock[];
  knowledge_points: ParsedKnowledgePoint[];
  objectives: string[];
  concepts: string[];
  formulas: string[];
  methods: string[];
  status: 'parsed' | 'needs_review';
}

export interface ParsedChapter {
  title: string;
  line_start: number;
  line_end: number;
  sections: ParsedSection[];
  status: 'parsed' | 'needs_review';
}

export interface ParsedDocument {
  blocks: ParsedBlock[];
  chapters: ParsedChapter[];
}

const CHAPTER_RE = /^(?:#{1,3}\s*)?(?:第[一二三四五六七八九十百零\d]+(?:章|单元)|chapter\s+\d+)\s*[:：.、]?\s*(.*)$/i;
const SECTION_RE = /^(?:#{1,3}\s*)?(?:第[一二三四五六七八九十百零\d]+(?:节|课|讲)|\d+(?:\.\d+)+)[\s:：.、]*(.*)$/i;
const CHINESE_SECTION_RE = /^(?:#{1,3}\s*)?([一二三四五六七八九十]+)[、.．]\s*(\S.{0,60})$/;
const NUMBERED_HEADING_RE = /^(?:#{1,3}\s*)?(\d{1,2})[.、]\s*(\S.{0,50})$/;
const KNOWLEDGE_RE = /^(?:#{2,4}\s*|知识点\s*[:：]\s*|\*\*)(.+?)(?:\*\*)?$/;

function cleanTitle(value: string): string {
  return value
    .replace(/^#+\s*/, '')
    .replace(/^[一二三四五六七八九十]+[、.．]\s*/, '')
    .replace(/^\d{1,2}[.、]\s*/, '')
    .replace(/^[*_]+|[*_]+$/g, '')
    .trim()
    .slice(0, 120);
}

function isTable(lines: string[], index: number): boolean {
  const line = lines[index].trim();
  if (!line.startsWith('|')) return false;
  const next = (lines[index + 1] || '').trim();
  return /\|/.test(line) && /^\|?[\s:|-]+\|?$/.test(next);
}

function isFormula(line: string): boolean {
  return /(\$[^$]+\$|\\\(|\\\[|\\frac|\\begin\{)/.test(line)
    || (/=/.test(line) && /[a-zA-Z0-9)]/.test(line) && line.length < 300);
}

function isList(line: string): boolean {
  return /^([-*+]\s+|\d+[.、)]\s+)/.test(line.trim());
}

function isImage(line: string): boolean {
  return /!\[[^\]]*\]\([^)]+\)/.test(line);
}

function headingLevel(line: string, usedKnowledge: boolean): ParsedBlock['heading_level'] {
  const value = line.trim();
  if (!value) return null;
  if (CHAPTER_RE.test(value)) return 'chapter';
  if (SECTION_RE.test(value) || CHINESE_SECTION_RE.test(value)) return 'section';
  // 1. / 2. 只有在短标题、无句末标点时才作为层级候选；
  // 真正的编号列表仍由 isList + 上下文/Agent 判断。
  const numbered = value.match(NUMBERED_HEADING_RE);
  if (numbered && !/[。！？!?；;]$/.test(value) && value.length <= 60) return 'section';
  if (KNOWLEDGE_RE.test(value) && !value.startsWith('# ')) {
    // 「# 标题」通常是大节标题；二级以下或无 # 的“知识点：”才作为知识点。
    if (!usedKnowledge || /^#{2,4}\s+/.test(value) || /^知识点/.test(value) || /^\*\*/.test(value)) return 'knowledge';
  }
  return null;
}

function splitInlineList(value: string): string[] {
  return value
    .replace(/^[：:]\s*/, '')
    .split(/[；;]|(?<=\S)、/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 4)
    .slice(0, 8);
}

function syntheticTitle(text: string): string {
  const first = text.replace(/^[-*+]\s+/, '').split(/[。！？!?；;]/)[0].trim();
  return (first || text).slice(0, 40);
}

/**
 * 解析纯文本 / Markdown。页码仅在未来 PDF 提取提供 page 信息时使用；
 * 文本文件用 line_start/line_end 精确定位。
 */
export function parseStructuredDocument(text: string, options: { pages?: Array<number | null> } = {}): ParsedDocument {
  const normalized = String(text || '').replace(/\r\n?/g, '\n');
  const lines = normalized.split('\n');
  const blocks: ParsedBlock[] = [];
  let currentPage: number | null = null;
  let ordinal = 0;
  let skipUntil = -1;

  lines.forEach((raw, index) => {
    if (index <= skipUntil) return;
    if (raw.includes('\f')) currentPage = (currentPage || 0) + 1;
    const line = raw.replace(/\f/g, '').trim();
    if (!line) return;
    const pageMarker = line.match(/^页码[：:]\s*(\d+)/);
    if (pageMarker) {
      currentPage = Number(pageMarker[1]);
      return;
    }
    ordinal += 1;

    let blockType: BlockType = 'paragraph';
    let heading: ParsedBlock['heading_level'] = null;
    if (isTable(lines, index)) blockType = 'table';
    else if (isImage(line)) blockType = 'image';
    else if (isFormula(line)) blockType = 'formula';
    else {
      heading = headingLevel(line, blocks.some((block) => block.heading_level === 'knowledge'));
      if (heading) blockType = 'heading';
      else if (isList(line)) blockType = 'list';
    }

    blocks.push({
      key: blocks.length,
      block_type: blockType,
      text: line,
      markdown: line,
      page: options.pages?.[index] ?? currentPage ?? null,
      line_start: index + 1,
      line_end: index + 1,
      heading_level: heading,
      status: heading ? 'parsed' : 'parsed',
    });

    // 表格后续行并入当前表格块，保留 Markdown。
    if (blockType === 'table') {
      let cursor = index + 1;
      while (cursor < lines.length && lines[cursor].trim().startsWith('|')) {
        blocks[blocks.length - 1].markdown += `\n${lines[cursor].trim()}`;
        blocks[blocks.length - 1].line_end = cursor + 1;
        cursor += 1;
      }
      skipUntil = cursor - 1;
    }
  });

  const chapters: ParsedChapter[] = [];
  let currentChapter: ParsedChapter | null = null;
  let currentSection: ParsedSection | null = null;
  let currentPoint: { title: string; blocks: ParsedBlock[]; explicit: boolean } | null = null;
  let pendingBlocks: ParsedBlock[] = [];

  const ensureChapter = (title: string, line: number) => {
    currentChapter = { title, line_start: line, line_end: line, sections: [], status: 'needs_review' };
    chapters.push(currentChapter);
    return currentChapter;
  };
  const ensureSection = (title: string, line: number) => {
    if (!currentChapter) ensureChapter('课程内容', line);
    currentSection = {
      title,
      line_start: line,
      line_end: line,
      blocks: pendingBlocks,
      knowledge_points: [],
      objectives: [],
      concepts: [],
      formulas: [],
      methods: [],
      status: 'needs_review',
    };
    pendingBlocks = [];
    currentChapter!.sections.push(currentSection);
    return currentSection;
  };
  const pushPoint = () => {
    if (!currentSection || !currentPoint) return;
    const body = currentPoint.blocks.map((block) => block.text).join('\n').trim();
    currentSection.knowledge_points.push({
      title: cleanTitle(currentPoint.title),
      explanation: body,
      concepts: [],
      formulas: [],
      methods: [],
      block_keys: currentPoint.blocks.map((block) => block.key),
      status: currentPoint.explicit ? 'parsed' : 'needs_review',
      confidence: currentPoint.explicit ? 0.9 : 0.45,
    });
    currentPoint = null;
  };

  blocks.forEach((block) => {
    if (block.heading_level === 'chapter') {
      pushPoint();
      const match = block.text.match(CHAPTER_RE);
      if (pendingBlocks.length && currentChapter) {
        const fallback = ensureSection('本章导学', block.line_start);
        fallback.blocks.push(...pendingBlocks);
        pendingBlocks = [];
      }
      ensureChapter(cleanTitle(match?.[1] || block.text), block.line_start);
      currentSection = null;
      pendingBlocks = [];
      return;
    }
    if (block.heading_level === 'section') {
      pushPoint();
      const match = block.text.match(SECTION_RE);
      ensureSection(cleanTitle(match?.[1] || block.text), block.line_start);
      return;
    }
    if (block.heading_level === 'knowledge') {
      pushPoint();
      if (!currentSection) ensureSection('本节内容', block.line_start);
      const match = block.text.match(KNOWLEDGE_RE);
      currentPoint = {
        title: (match?.[1] || block.text).replace(/^知识点\s*[:：]\s*/, ''),
        blocks: [],
        explicit: true,
      };
      currentSection!.blocks.push(block);
      return;
    }

    if (!currentSection) {
      pendingBlocks.push(block);
      if (currentChapter) currentChapter.line_end = Math.max(currentChapter.line_end, block.line_end);
      return;
    }
    currentSection!.blocks.push(block);
    if (currentPoint) {
      currentPoint.blocks.push(block);
    }
    currentSection!.line_end = Math.max(currentSection!.line_end, block.line_end);
    if (currentChapter) currentChapter.line_end = Math.max(currentChapter.line_end, block.line_end);
  });
  pushPoint();
  if (pendingBlocks.length) {
    if (!currentChapter) ensureChapter('课程内容', pendingBlocks[0].line_start);
    const fallback = ensureSection('本节内容', pendingBlocks[0].line_start);
    fallback.blocks.push(...pendingBlocks);
    pendingBlocks = [];
  }

  // 没有显式“知识点”时，按段落建立待确认知识点，不把章/节标题当作知识点。
  chapters.forEach((chapter) => {
    chapter.sections.forEach((section) => {
      if (section.knowledge_points.length === 0) {
        section.blocks
          .filter((block) => block.block_type === 'paragraph' && block.text.length >= 6)
          .forEach((block) => {
            section.knowledge_points.push({
              title: syntheticTitle(block.text),
              explanation: block.text,
              concepts: [],
              formulas: [],
              methods: [],
              block_keys: [block.key],
              status: 'needs_review',
              confidence: 0.35,
            });
          });
      }

      section.knowledge_points.forEach((point) => {
        const texts = point.block_keys.map((key) => blocks.find((block) => block.key === key)?.text || '');
        const formulas = texts.filter((line) => isFormula(line) || /\\\(|\\\[|\$[^$]+\$/.test(line));
        const concepts = texts.filter((line) => /(概念|定义|叫做|称为|含义)/.test(line));
        const methods = texts.filter((line) => /(方法|步骤|解法|技巧|归纳|算法)/.test(line));
        point.concepts = [...new Set(concepts)].slice(0, 8);
        point.formulas = [...new Set([...formulas, ...texts.filter((line) => isFormula(line))])].slice(0, 8);
        point.methods = [...new Set(methods)].slice(0, 8);
        section.concepts.push(...point.concepts);
        section.formulas.push(...point.formulas);
        section.methods.push(...point.methods);
      });
      section.concepts = [...new Set(section.concepts)].slice(0, 20);
      section.formulas = [...new Set(section.formulas)].slice(0, 20);
      section.methods = [...new Set(section.methods)].slice(0, 20);

      // 学习目标只从原文识别；识别不到就留空并标记待确认。
      let collecting = false;
      section.blocks.forEach((block) => {
        const text = block.text.trim();
        if (/^(学习目标|教学目标|学习要求|课标要求)/.test(text)) {
          collecting = true;
          const inline = text.replace(/^(学习目标|教学目标|学习要求|课标要求)\s*[:：]?/, '').trim();
          if (inline) section.objectives.push(...splitInlineList(inline));
          return;
        }
        if (collecting && block.block_type === 'list') {
          section.objectives.push(text.replace(/^([-*+]\s+|\d+[.、)]\s+)/, '').trim());
          return;
        }
        if (collecting && block.block_type !== 'list') collecting = false;
      });
      section.objectives = [...new Set(section.objectives.filter(Boolean))].slice(0, 10);
      section.status = section.objectives.length > 0
        && section.knowledge_points.some((point) => point.status === 'parsed')
        ? 'parsed'
        : 'needs_review';
      chapter.status = chapter.sections.every((item) => item.status === 'parsed') ? 'parsed' : 'needs_review';
    });
  });

  return { blocks, chapters };
}

/** 由结构化解析结果生成 Markdown 教案；不补写教材事实。 */
export function buildLessonMarkdown(section: ParsedSection, sourceLabel: string): string {
  const lines: string[] = [
    '> 试学版：本节依据教材原文整理，教案仍在完善。',
    '',
    '## 学习目标',
    '',
  ];
  if (section.objectives.length) lines.push(...section.objectives.map((item) => `- ${item}`));
  else lines.push('本节学习目标尚未从教材中明确识别，待确认后再补充。');

  lines.push('', '## 知识点', '');
  if (section.knowledge_points.length === 0) {
    lines.push('本节暂未识别出可靠的知识点，请以教材原文为准。');
  } else {
    section.knowledge_points.forEach((point) => {
      lines.push(`### ${point.title}`, '', point.explanation || '原文内容待确认。', '');
      if (point.concepts.length) {
        lines.push('**核心概念**', '', ...point.concepts.map((item) => `- ${item}`), '');
      }
      if (point.formulas.length) {
        lines.push('**公式 / 表达**', '', ...point.formulas.map((item) => `- ${item}`), '');
      }
      if (point.methods.length) {
        lines.push('**方法 / 步骤**', '', ...point.methods.map((item) => `- ${item}`), '');
      }
    });
  }

  const referenced = new Set(section.knowledge_points.flatMap((point) => point.block_keys));
  const objectiveSet = new Set(section.objectives);
  const extras = section.blocks.filter((block) => {
    if (referenced.has(block.key)) return false;
    if (block.block_type === 'heading' || block.block_type === 'table' || block.block_type === 'image') return false;
    if (block.block_type === 'list') {
      const text = block.text.replace(/^([-*+]\s+|\d+[.、)]\s+)/, '').trim();
      if (objectiveSet.has(text)) return false;
    }
    if (/^(学习目标|教学目标|学习要求|课标要求)/.test(block.text.trim())) return false;
    return true;
  });
  if (extras.length) {
    lines.push('## 补充原文', '', ...extras.map((block) => block.markdown), '');
  }

  const tables = section.blocks.filter((block) => block.block_type === 'table');
  if (tables.length) lines.push('## 表格', '', ...tables.map((block) => block.markdown), '');
  const images = section.blocks.filter((block) => block.block_type === 'image');
  if (images.length) lines.push('## 图片引用', '', ...images.map((block) => block.markdown), '');

  lines.push('## 教材出处', '', `- ${sourceLabel}`, '');
  return lines.join('\n');
}
