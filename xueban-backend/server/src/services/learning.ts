/**
 * 学习中心生成服务（框架阶段）。
 *
 * 真正的教材解析、教案生成、习题提取和校验尚未接入；这里实现可替换的生成管线：
 * 1. 从文本中提取章节标题形成目录；
 * 2. 为每一节生成教案骨架；
 * 3. 生成明确标注为“待教研生成”的练习占位；
 * 4. 全过程记录状态，便于后续替换为真实解析/生成 agent。
 */
import {
  clearCourseContent,
  createKnowledgePoint,
  createSourceBlock,
  appendGenerationEvent,
  clearCourseStructure,
  runInTransaction,
  deleteSourceBlocksByIds,
  deleteSourceBlocksExcept,
  updateCourseMeta,
  updateKnowledgePointPrerequisites,
  createExercise,
  createLesson,
  createUnit,
  getCourse,
  getCourseDetail,
  getLatestJob,
  updateCourseStatus,
  updateJob,
  type CourseSourceType,
  type LearningCourse,
  type LearningSourceBlock,
} from '../storage/learning';
import { parseStructuredDocument } from './learning-parser';
import { LearningAgentError, parseTextbookWithAgent, repairFallbackPages, type AgentCall, type AgentKnowledgePoint, type AgentTeachingUnit, type AgentLessonDesign, type AgentModelConfig, type AgentCallStat, type AgentChapter } from './learning-agent';
import type { LearningDocumentAudit } from './xueban-attachments';

export type MaterialType = 'textbook' | 'syllabus' | 'teaching_outline' | 'supplementary';

/** 为后续教材 + 课标/大纲联合解析预留的材料级来源结构。 */
export interface CourseMaterialSource {
  material_id: string;
  material_type: MaterialType;
  source_name: string;
  source_type: string;
  text: string;
  images?: Array<{ image_id?: string; mime: string; data_url: string; page?: number | null; is_fallback?: boolean; context?: string }>;
  audit?: LearningDocumentAudit;
  /** 多材料预留：当前单文件路径仍使用 text；后续可逐材料保留来源。 */
  materials?: CourseMaterialSource[];
}

/** 稳定的来源引用：不把教材和课纲全文直接拼成一个 source.text。 */
export interface LearningSourceReference {
  material_id: string;
  material_type: MaterialType;
  source_name: string;
  source_type: string;
  source_block_id: string;
  source_page: number | null;
  start_offset?: number;
  end_offset?: number;
}

export interface CourseSourceInput {
  source_type: CourseSourceType;
  source_name?: string;
  material_id?: string;
  text: string;
  images?: Array<{ image_id?: string; mime: string; data_url: string; page?: number | null; is_fallback?: boolean; context?: string }>;
  audit?: LearningDocumentAudit;
}

interface ParsedSection {
  title: string;
  body: string;
}

const HEADING = /^(?:第[一二三四五六七八九十百零\d]+[章节讲课]|chapter\s+\d+|\d+(?:\.\d+){0,3}[、.．]?\s+|#{1,3}\s*)(.*)$/i;

function cleanHeading(line: string): string {
  return line
    .replace(/^#{1,3}\s*/, '')
    .replace(/^第[一二三四五六七八九十百零\d]+[章节讲课]\s*/, '')
    .replace(/^\d+(?:\.\d+){0,3}[、.．]?\s*/, '')
    .trim()
    .slice(0, 80);
}

function isHeading(line: string): boolean {
  if (!line || line.length > 100) return false;
  if (!HEADING.test(line)) return false;
  return cleanHeading(line).length > 0;
}

/** 从教材/课标/描述中提取可用的节结构；纯文本没有标题时按段落分组。 */
export function parseSections(text: string, fallbackTitle: string): ParsedSection[] {
  const normalized = String(text || '').replace(/\r\n?/g, '\n').trim();
  if (!normalized) {
    return [{ title: fallbackTitle.slice(0, 80) || '学习导入', body: '' }];
  }
  const lines = normalized.split('\n');
  const headings: Array<{ index: number; title: string }> = [];
  lines.forEach((line, index) => {
    const value = line.trim();
    if (isHeading(value)) headings.push({ index, title: cleanHeading(value) });
  });

  if (headings.length >= 2) {
    return headings.slice(0, 40).map((heading, index) => {
      const next = headings[index + 1]?.index ?? lines.length;
      const body = lines.slice(heading.index + 1, next).join('\n').trim();
      return { title: heading.title, body: body.slice(0, 6000) };
    });
  }

  const paragraphs = normalized.split(/\n\s*\n+/).map((item) => item.trim()).filter(Boolean);
  if (paragraphs.length <= 1) {
    return [{ title: fallbackTitle.slice(0, 80) || '学习导入', body: normalized.slice(0, 6000) }];
  }
  const groupSize = Math.max(1, Math.ceil(paragraphs.length / Math.min(6, paragraphs.length)));
  const sections: ParsedSection[] = [];
  for (let index = 0; index < paragraphs.length; index += groupSize) {
    const group = paragraphs.slice(index, index + groupSize);
    const body = group.join('\n\n');
    sections.push({
      title: `第 ${sections.length + 1} 部分`,
      body: body.slice(0, 6000),
    });
  }
  return sections.slice(0, 12);
}

export interface ParsedChapter {
  title: string;
  body: string;
  sections: ParsedSection[];
}

const CHAPTER_HEADING = /^(?:第[一二三四五六七八九十百零\d]+章|chapter\s+\d+)/i;
const SECTION_HEADING = /^(?:第[一二三四五六七八九十百零\d]+节|\d+(?:\.\d+)+|#{1,3}\s*)/i;

/** 按“章 / 节”层级拆分教材目录；无法识别层级时退化为单章。 */
export function parseOutline(text: string, fallbackTitle: string): ParsedChapter[] {
  const normalized = String(text || '').replace(/\r\n?/g, '\n').trim();
  if (!normalized) {
    return [{ title: '课程目录', body: '', sections: [{ title: fallbackTitle || '学习导入', body: '' }] }];
  }
  const lines = normalized.split('\n');
  const headings: Array<{ index: number; title: string; level: 'chapter' | 'section' }> = [];
  lines.forEach((line, index) => {
    const value = line.trim();
    if (!value || value.length > 100) return;
    if (CHAPTER_HEADING.test(value)) {
      headings.push({ index, title: cleanHeading(value), level: 'chapter' });
      return;
    }
    if (SECTION_HEADING.test(value)) {
      const title = cleanHeading(value) || value.replace(/^#+\s*/, '').trim();
      if (title) headings.push({ index, title, level: 'section' });
    }
  });

  if (headings.length === 0) {
    // 没有可识别标题：按段落分成若干节，统一放在“课程目录”下。
    const sections = parseSections(normalized, fallbackTitle);
    return [{ title: '课程目录', body: '', sections }];
  }

  const chapters: ParsedChapter[] = [];
  let current: ParsedChapter | null = null;
  headings.forEach((heading, index) => {
    const next = headings[index + 1]?.index ?? lines.length;
    const body = lines.slice(heading.index + 1, next).join('\n').trim().slice(0, 6000);
    if (heading.level === 'chapter') {
      current = { title: heading.title, body, sections: [] };
      chapters.push(current);
      return;
    }
    if (!current) {
      current = { title: '课程目录', body: '', sections: [] };
      chapters.push(current);
    }
    current.sections.push({ title: heading.title, body });
  });

  chapters.forEach((chapter) => {
    if (chapter.sections.length === 0) {
      // 只有“章”标题时，把这一章本身作为一节。
      chapter.sections.push({ title: chapter.title, body: chapter.body });
    }
  });
  return chapters.slice(0, 30);
}

function excerpt(body: string, limit = 800): string {
  return body.length > limit ? `${body.slice(0, limit)}…` : body;
}

function keyPoints(body: string, title: string): string[] {
  const sentences = body
    .replace(/\s+/g, ' ')
    .split(/(?<=[。！？!?；;])/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 6)
    .slice(0, 4);
  if (sentences.length) return sentences;
  return [`理解「${title}」的基本概念`, `能够用自己的话解释「${title}」`, `完成本节配套练习`];
}

function lessonContent(input: {
  title: string;
  sourceName: string;
  sourceType: CourseSourceType;
  body: string;
  points: string[];
}): string {
  const sourceLabel = input.sourceType === 'description'
    ? '学生自主描述'
    : input.sourceType === 'curriculum' ? '课标/教学大纲' : '教材';
  return [
    input.sourceName
      ? `> 选自${sourceLabel}《${input.sourceName}》`
      : `> 选自${sourceLabel}`,
    '',
    '## 学习目标',
    '',
    `- 理解「${input.title}」的核心概念与来龙去脉`,
    `- 能用自己的话解释关键结论，并说明适用条件`,
    `- 完成本节课上练习，课后独立完成巩固练习`,
    '',
    '## 核心内容',
    '',
    ...(input.points.length
      ? input.points.map((point) => `- ${point}`)
      : [`- 本节重点：${input.title}`]),
    '',
    input.body
      ? `## 来源摘录\n\n${excerpt(input.body)}`
      : '## 来源摘录\n\n本节内容将结合课程材料逐步补充。',
    '',
    '## 学习步骤',
    '',
    '1. 先通读本节目标与来源摘录，标出看不懂的词句。',
    '2. 用自己的话复述核心概念，再对照来源检查是否准确。',
    '3. 进入练习中心完成课上练习；完成后回到本节标记“已学完”。',
    '4. 标记完成后解锁课后练习。',
  ].join('\n');
}

export function summarizeLessonCoverage(
  points: Array<{ title: string; block_ids: string[] }>,
  chapters: AgentChapter[],
): {
  total_points: number;
  covered_points: number;
  uncovered_points: string[];
  textbook_citations: number;
  supplemental_items: number;
} {
  const designBlockIds = new Set<string>();
  const sources: string[] = [];
  chapters.forEach((chapter) => chapter.sections.forEach((section) => {
    const design = section.lesson_design;
    if (!design) return;
    const items = [
      ...design.preparation, ...(design.introduction ? [design.introduction] : []),
      ...design.teaching_flow, ...design.examples, ...design.misconceptions,
      ...design.activities, ...(design.summary ? [design.summary] : []), ...design.self_check,
    ];
    items.forEach((item) => {
      item.block_ids.forEach((id) => designBlockIds.add(id));
      sources.push(item.source || 'needs_review');
    });
  }));
  if (!designBlockIds.size) {
    // 无独立教学设计时，学案由全部知识点和原文块组装；
    // 覆盖按知识点来源块统计，不能把“没有 design”误判为 0 个知识点被覆盖。
    const pointBlocks = new Set(points.flatMap((point) => point.block_ids));
    return {
      total_points: points.length,
      covered_points: points.length,
      uncovered_points: [],
      textbook_citations: pointBlocks.size,
      supplemental_items: 0,
    };
  }
  const covered = points.filter((point) => point.block_ids.some((id) => designBlockIds.has(id)));
  return {
    total_points: points.length,
    covered_points: covered.length,
    uncovered_points: points.filter((point) => !point.block_ids.some((id) => designBlockIds.has(id))).map((point) => point.title).slice(0, 100),
    textbook_citations: sources.filter((source) => source === 'textbook').length,
    supplemental_items: sources.filter((source) => source === 'supplemental').length,
  };
}

/**
 * 执行一次课程生成。真实 agent 接入后，只需要替换本函数内部实现，
 * 保持课程/目录/教案/练习的落库结构不变。
 */
export interface CourseGenerationOptions {
  config: AgentModelConfig;
  callImpl?: AgentCall;
}

function pointSourceLabel(sourceName: string, block?: LearningSourceBlock): string {
  if (!block) return sourceName || '课程材料';
  return `${sourceName || '课程材料'} · 第 ${block.line_start}-${block.line_end} 行`;
}

function normalizeLegacyMath(value: string): string {
  return value
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `$$\n${String(body).trim()}\n$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${String(body).trim()}$`);
}

function formatLessonFormula(value: string): string {
  const formula = normalizeLegacyMath(String(value || '').trim());
  if (!formula) return '';
  if (/^\$\$[\s\S]*\$\$$/.test(formula) || /^\$[^$]+\$$/.test(formula) || /^\\\[/.test(formula)) return formula;
  // 混合中文说明中的 $...$ 仍是合法行内公式，不能被整体包成代码。
  if (/\$[^$]+\$/.test(formula)) return formula;
  // 中文算式不是合法 LaTeX，保留为行内代码，避免 KaTeX 报错。
  if (/[\u4e00-\u9fa5]/.test(formula)) return `\`${formula}\``;
  return `$$${formula}$$`;
}

const DESIGN_SOURCE_LABEL: Record<string, string> = {
  textbook: '教材原文',
  derived: '由教材推导',
  supplemental: '教学补充',
  needs_review: '待确认',
};

function designSourceNote(source: string): string {
  if (source === 'textbook') return '';
  return `> 来源：${DESIGN_SOURCE_LABEL[source] || '待确认'}`;
}

function designObjectiveLabel(kind: string): string {
  if (kind === 'method') return '方法掌握';
  if (kind === 'application') return '应用能力';
  return '知识理解';
}

function buildDesignedLessonMarkdown(input: {
  title: string;
  design: AgentLessonDesign;
  sourceLabel: string;
  extraBlocks?: LearningSourceBlock[];
}): string {
  const design = input.design;
  const lines = ['## 学习目标', ''];
  if (design.objectives.length) {
    design.objectives.forEach((objective) => {
      lines.push(`- 【${designObjectiveLabel(objective.kind)}】${objective.text}`);
    });
  } else {
    lines.push('本节学习目标尚未从教材中明确识别，待确认后再补充。');
  }

  lines.push('', '## 学习准备', '');
  if (design.preparation.length) {
    design.preparation.forEach((item) => {
      lines.push(`- ${item.text}`);
      const note = designSourceNote(item.source);
      if (note) lines.push(`  ${note.replace(/^> /, '')}`);
    });
  } else {
    lines.push('本节教材没有明确列出前置知识，先回顾本节标题和已学相关概念。');
  }

  lines.push('', '## 情境导入', '');
  if (design.introduction) {
    lines.push(design.introduction.text);
    const note = designSourceNote(design.introduction.source);
    if (note) lines.push('', note);
  } else {
    lines.push('本节教材没有提供明确的情境材料，直接从下面的核心概念开始学习。');
  }

  lines.push('', '## 核心学习', '');
  if (!design.teaching_flow.length) lines.push('本节核心内容待确认。');
  design.teaching_flow.forEach((item) => {
    lines.push(`### ${item.title}`, '', item.body, '');
    if (item.bullets.length) lines.push(...item.bullets.map((bullet) => `- ${bullet}`), '');
    item.formulas.forEach((formula) => {
      const rendered = formatLessonFormula(formula);
      if (rendered) lines.push(rendered, '');
    });
    const note = designSourceNote(item.source);
    if (note) lines.push(note, '');
  });

  if (design.examples.length) {
    lines.push('## 典型例题', '');
    design.examples.forEach((example, index) => {
      lines.push(`### 例题 ${index + 1}：${example.title}`, '', example.problem, '');
      if (example.steps.length) {
        lines.push('**解题步骤**', '');
        example.steps.forEach((step, stepIndex) => lines.push(`${stepIndex + 1}. ${step}`));
        lines.push('');
      }
      if (example.answer) lines.push(`**答案**：${example.answer}`, '');
      const note = designSourceNote(example.source);
      if (note) lines.push(note, '');
    });
  }

  lines.push('## 易错提醒', '');
  if (design.misconceptions.length) {
    design.misconceptions.forEach((item) => {
      lines.push(`- ${item.text}`);
      const note = designSourceNote(item.source);
      if (note) lines.push(`  ${note.replace(/^> /, '')}`);
    });
  } else {
    lines.push('本节教材没有明确提示易错点，请结合概念适用条件自行检查。');
  }

  if (design.activities.length) {
    lines.push('', '## 学习活动', '');
    design.activities.forEach((activity, index) => {
      lines.push(`${index + 1}. ${activity.prompt}`);
      if (activity.purpose) lines.push(`   - 目的：${activity.purpose}`);
    });
  }

  lines.push('', '## 本节小结', '');
  lines.push(design.summary?.text || '本节核心知识与逻辑关系待确认。');
  if (design.summary) {
    const note = designSourceNote(design.summary.source);
    if (note) lines.push('', note);
  }

  if (design.self_check.length) {
    lines.push('', '## 自我检查', '');
    design.self_check.forEach((item, index) => {
      lines.push(`${index + 1}. ${item.question}`);
      if (item.answer_hint) lines.push(`   - 提示：${item.answer_hint}`);
    });
  }

  lines.push('## 教材出处', '', `- ${input.sourceLabel}`, '');
  return lines.join('\n');
}

function chineseHeadingNumber(value: number): string {
  const digits = '零一二三四五六七八九';
  if (value <= 0) return String(value);
  if (value < 10) return digits[value];
  if (value < 20) return `十${value === 10 ? '' : digits[value - 10]}`;
  const tens = Math.floor(value / 10);
  const ones = value % 10;
  return `${digits[tens]}十${ones ? digits[ones] : ''}`;
}

function stripLessonHeadingNumber(value: string): string {
  return value
    .replace(/^[一二三四五六七八九十]+[、.．]\s*/, '')
    .replace(/^\d+(?:\.\d+)*[.、]?\s*/, '')
    .trim();
}

/** 给学案各级标题加稳定层级编号，避免 10+ 个未编号小标题堆在一起。 */
export function numberLessonMarkdown(markdown: string): string {
  let section = 0;
  let subsection = 0;
  let detail = 0;
  let label = 0;
  return markdown.split('\n').map((line) => {
    if (line.startsWith('## ')) {
      section += 1; subsection = 0; detail = 0; label = 0;
      return `## ${chineseHeadingNumber(section)}、${stripLessonHeadingNumber(line.slice(3))}`;
    }
    if (line.startsWith('### ')) {
      subsection += 1; detail = 0; label = 0;
      return `### ${section}.${subsection} ${stripLessonHeadingNumber(line.slice(4))}`;
    }
    if (line.startsWith('#### ')) {
      detail += 1; label = 0;
      return `#### ${section}.${subsection}.${detail} ${stripLessonHeadingNumber(line.slice(5))}`;
    }
    const labelMatch = line.match(/^\*\*(核心概念|公式 \/ 表达|方法 \/ 步骤|前置知识)\*\*$/);
    if (labelMatch) {
      label += 1;
      return `**（${label}）${labelMatch[1]}**`;
    }
    return line;
  }).join('\n');
}

function demoteUnitExplanation(value: string): string {
  return value.split('\n').map((line) => {
    const match = line.match(/^(#{1,3})\s+/);
    if (!match) return line;
    return `${'#'.repeat(Math.min(5, match[1].length + 2))} ${line.slice(match[0].length)}`;
  }).join('\n');
}

function buildTeachingUnitLessonMarkdown(input: {
  title: string;
  objectives: string[];
  knowledgePoints: AgentKnowledgePoint[];
  teachingUnits: AgentTeachingUnit[];
  sourceLabel: string;
}): string {
  const lines: string[] = ['## 学习目标', ''];
  const objectives = [...new Set([
    ...input.teachingUnits.flatMap((unit) => unit.objectives),
    ...input.objectives,
  ])].slice(0, 4);
  if (objectives.length) lines.push(...objectives.map((item) => `- ${item}`));
  else lines.push('本节学习目标待结合教材原文确认。');
  lines.push('', '## 教学单元', '');
  const usedExamples = new Set<string>();

  input.teachingUnits.forEach((unit) => {
    lines.push(`### ${unit.title}`, '');
    if (unit.point_titles.length) {
      lines.push(`覆盖知识点：${unit.point_titles.join('、')}`, '');
    }
    if (unit.introduction) lines.push(`> ${unit.introduction}`, '');
    lines.push('**核心讲解**', '', demoteUnitExplanation(unit.explanation.trim()), '');

    const unitPoints = input.knowledgePoints.filter((point) => unit.point_titles.includes(point.title));
    const formulas = [...new Set(unitPoints.flatMap((point) => point.formulas))];
    if (formulas.length) {
      lines.push('**相关公式**', '');
      formulas.forEach((formula) => {
        const rendered = formatLessonFormula(formula);
        if (rendered) lines.push(rendered, '');
      });
    }

    const exampleKey = unit.example ? unit.example.problem.replace(/\s+/g, '') : '';
    if (unit.example && exampleKey && !usedExamples.has(exampleKey)) {
      usedExamples.add(exampleKey);
      lines.push('**典型例题**', '', `题目：${unit.example.problem}`, '');
      if (unit.example.thinking) lines.push(`思路：${unit.example.thinking}`, '');
      if (unit.example.steps.length) {
        lines.push('解题步骤：', '');
        unit.example.steps.forEach((step, index) => {
          lines.push(`${index + 1}. ${step.text}`);
          if (step.reason) lines.push(`   - 依据：${step.reason}`);
        });
        lines.push('');
      }
      if (unit.example.answer) lines.push(`答案：${unit.example.answer}`, '');
      if (unit.example.warning) lines.push(`易错提醒：${unit.example.warning}`, '');
    }

    if (unit.misconceptions?.length) {
      lines.push('**易错点**', '', ...unit.misconceptions.map((item) => `- ${item}`), '');
    }
    if (unit.checks?.length) {
      lines.push('**理解检查**', '');
      unit.checks.forEach((check, index) => {
        lines.push(`${index + 1}. ${check.question}`);
        lines.push(`   参考答案：${check.answer}`);
      });
      lines.push('');
    }
    if (
      unit.point_titles.some((title) => title.includes('算术平方根'))
      && !unit.explanation.includes('算术平方根只取非负')
      && !unit.explanation.includes('平方根有两个')
    ) {
      lines.push('**平方根与算术平方根的区别**', '',
        '平方根包含正、负两个值，正数 $a$ 的平方根表示为 $\\pm\\sqrt{a}$；算术平方根只取其中非负的那个，表示为 $\\sqrt{a}$。0 的平方根和算术平方根都是0。', '');
    }
    if (unit.summary) lines.push('**本单元小结**', '', unit.summary.trim(), '');

    const relationRows: string[] = [];
    if (unit.relations.prerequisite.length) relationRows.push(`- 前置知识：${unit.relations.prerequisite.join('、')}`);
    if (unit.relations.followup.length) relationRows.push(`- 后续知识：${unit.relations.followup.join('、')}`);
    if (unit.relations.related.length) relationRows.push(`- 相关知识：${unit.relations.related.join('、')}`);
    if (relationRows.length) lines.push('**知识关联**', '', ...relationRows, '');
  });

  lines.push('## 教材出处', '', `- ${input.sourceLabel}`, '');
  return numberLessonMarkdown(lines.join('\n'));
}

function buildAgentLessonMarkdown(input: {
  title: string;
  objectives: string[];
  knowledgePoints: AgentKnowledgePoint[];
  teachingUnits?: AgentTeachingUnit[];
  sourceLabel: string;
  extraBlocks?: LearningSourceBlock[];
  design?: AgentLessonDesign;
}): string {
  if (input.teachingUnits?.length && !input.design) {
    return buildTeachingUnitLessonMarkdown({
      title: input.title, objectives: input.objectives, knowledgePoints: input.knowledgePoints,
      teachingUnits: input.teachingUnits, sourceLabel: input.sourceLabel,
    });
  }
  if (input.design && (
    input.design.objectives.length || input.design.preparation.length || input.design.introduction
    || input.design.teaching_flow.length || input.design.examples.length
    || input.design.misconceptions.length || input.design.activities.length
    || input.design.summary || input.design.self_check.length
  )) {
    return buildDesignedLessonMarkdown({ ...input, design: input.design });
  }
  const lines = ['## 学习目标', ''];
  if (input.objectives.length) lines.push(...input.objectives.map((item) => `- ${item}`));
  else lines.push('本节学习目标尚未从教材中明确识别，待确认后再补充。');
  lines.push('', '## 知识点', '');
  if (!input.knowledgePoints.length) lines.push('本节暂未识别出可靠的知识点，请以教材原文为准。');
  input.knowledgePoints.forEach((point) => {
    lines.push(`### ${point.title}`, '');
    if (point.origin === 'agent_inferred') lines.push('> Agent 归纳：以下解释由原文归纳，需结合教材原文核对。', '');
    lines.push(normalizeLegacyMath(point.explanation || '原文内容待确认。'), '');
    if (point.concepts.length) lines.push('**核心概念**', '', ...point.concepts.map((item) => `- ${item}`), '');
    if (point.formulas.length) {
      lines.push('**公式 / 表达**', '');
      point.formulas.forEach((item) => {
        const formula = formatLessonFormula(item);
        if (formula) lines.push(formula, '');
      });
    }
    if (point.methods.length) lines.push('**方法 / 步骤**', '', ...point.methods.map((item) => `- ${item}`), '');
    if (point.prerequisites.length) lines.push('**前置知识**', '', ...point.prerequisites.map((item) => `- ${item}`), '');
  });
  lines.push('## 教材出处', '', `- ${input.sourceLabel}`, '');
  return numberLessonMarkdown(lines.join('\n'));
}

/**
 * 执行一次课程生成。
 * 程序负责原文块与持久化；Agent 负责语义结构；程序再校验并落库。
 */
function generationProgress(stage: string, current: number, total: number): number {
  const ratio = Math.min(1, Math.max(0, current / Math.max(total, 1)));
  if (stage === 'outline') return Math.round(10 + ratio * 12);
  if (stage === 'sections') return Math.round(22 + ratio * 38);
  if (stage === 'lesson_design') return Math.round(60 + ratio * 28);
  if (stage === 'persisting') return 92;
  if (stage === 'done') return 100;
  return 8;
}

export async function runCourseGeneration(
  ownerId: string,
  courseId: string,
  source: CourseSourceInput,
  options: CourseGenerationOptions,
): Promise<LearningCourse | undefined> {
  const course = getCourse(ownerId, courseId);
  if (!course) return undefined;
  const job = getLatestJob(ownerId, courseId);
  const materialId = source.material_id || '';
  const prior = getCourseDetail(ownerId, courseId);
  const hadContent = Boolean(prior?.chapters.some((chapter) => chapter.sections.some((section) => section.lesson)));
  const logEvent = (kind: string, message: string, payload?: unknown) => {
    if (job) appendGenerationEvent(ownerId, courseId, job.id, kind, message, payload);
  };
  const insertedBlockIds: string[] = [];
  const callStats: AgentCallStat[] = [];
  const runConfig: AgentModelConfig = {
    ...options.config,
    onCallStart: (task) => {
      if (job) appendGenerationEvent(ownerId, courseId, job.id, 'model_start', task);
    },
    onCall: (stat) => {
      callStats.push(stat);
      options.config.onCall?.(stat);
      if (job) appendGenerationEvent(ownerId, courseId, job.id, 'model_call', `${stat.task}`, stat);
    },
  };
  let persisted = false;
  try {
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'extracting', progress: 8, error: '' });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'extracting' });
    logEvent('start', '开始读取教材原文');

    // 1. 低质量页面先尝试整页视觉转录；失败保留 page_status，不静默当原文。
    const repaired = await repairFallbackPages({
      text: source.text,
      images: (source.images || []).map((image) => ({
        kind: 'image' as const, data_url: image.data_url, page: image.page, is_fallback: image.is_fallback,
      })),
      audit: source.audit,
    }, runConfig, options.callImpl);
    source.text = repaired.text;
    if (repaired.repairedPages.length) logEvent('vision_repair', `视觉修复 ${repaired.repairedPages.join(', ')} 页`);
    if (repaired.failedPages.length) logEvent('vision_repair_failed', `视觉修复失败 ${repaired.failedPages.join(', ')} 页`);
    if (repaired.errors.length) {
      source.audit = {
        ...(source.audit || {}),
        vision_repair_errors: repaired.errors,
      };
    }

    // 1b. 程序提取原文块并落库，Agent 只能引用这些块。
    const parsed = parseStructuredDocument(source.text);
    const blockIds = new Map<number, string>();
    const storedBlocks: LearningSourceBlock[] = [];
    parsed.blocks.forEach((block) => {
      const pageRecord = source.audit?.page_records?.find((record) => record.page === block.page);
      const fallbackPage = Boolean(block.page && source.audit?.fallback_pages?.includes(block.page));
      const unreadablePage = Boolean(block.page && source.audit?.unreadable_pages?.includes(block.page));
      const stored = createSourceBlock({
        owner_id: ownerId,
        course_id: courseId,
        material_id: materialId,
        block_type: block.block_type,
        ordinal: block.key + 1,
        text: block.text,
        markdown: block.markdown,
        page: block.page,
        line_start: block.line_start,
        line_end: block.line_end,
        status: block.status,
        table_status: block.table_status,
        heading_level: block.heading_level,
        original_text: block.text,
        page_status: pageRecord?.status === 'vision_repaired' ? 'vision_repaired'
          : fallbackPage ? 'image_fallback' : unreadablePage ? 'unreadable' : (pageRecord?.status || 'text'),
        extraction_method: pageRecord?.status === 'vision_repaired' ? 'vision-repair'
          : fallbackPage ? 'page-image' : (pageRecord?.extraction_method || 'pymupdf-blocks'),
        // 原始提取结果与规范化结果分开保存；原文以 audit raw_text 为最终核对依据。
        raw_text: pageRecord?.raw_text || block.text,
        confidence: pageRecord?.status === 'normal' || pageRecord?.status === 'text' ? 1 : 0.6,
        repair_history_json: JSON.stringify(pageRecord?.font_repair ? [pageRecord.font_repair] : []),
      });
      blockIds.set(block.key, stored.id);
      storedBlocks.push(stored);
      insertedBlockIds.push(stored.id);
    });
    if (!storedBlocks.length) {
      throw new LearningAgentError('NO_SOURCE_BLOCKS', '教材中没有可解析的文本内容');
    }
    const imageMap = new Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>();
    (source.images || []).forEach((image) => {
      const block = storedBlocks.find((item) => (
        Boolean(image.image_id) && item.markdown.includes(String(image.image_id))
      ));
      if (!block) return;
      const list = imageMap.get(block.id) || [];
      list.push({ kind: 'image', data_url: image.data_url });
      imageMap.set(block.id, list);
    });

    logEvent('blocks', `已生成 ${storedBlocks.length} 个原文块，准备识别目录`);
    // 2. Agent 分批解析；程序记录每次尝试和批次。
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'outline', progress: 15, error: '' });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'outline' });
    const result = await parseTextbookWithAgent({
      courseTitle: course.title,
      subject: course.subject,
      grade: course.grade,
      blocks: storedBlocks,
      config: runConfig,
      callImpl: options.callImpl,
      imagesByBlock: imageMap,
      // 恢复 v1.3.7 的稳定路线：知识点提取后由程序组装学案。
      // 不再为每节额外调用 6 个教学设计片段；失败片段会导致只有例题。
      generateLessonDesigns: false,
      onProgress: (stage, current, total) => {
        const progress = generationProgress(stage, current, total);
        updateCourseStatus(ownerId, courseId, { status: 'generating', stage, progress });
        if (job) updateJob(ownerId, job.id, { status: 'running', stage: `${stage} ${current}/${total}` });
        logEvent('progress', `${stage} ${current}/${total}`, { stage, current, total, progress });
      },
      onAttempt: (attempt) => {
        if (job) updateJob(ownerId, job.id, { attempts: Math.max(job.attempts, attempt) });
        if (attempt > 1) logEvent('retry', `第 ${attempt} 次尝试调用模型`);
      },
    });

    logEvent('persist', '目录、知识点和学案已通过校验，开始写入课程');
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'persisting', progress: 92 });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'persisting', batch_count: result.batch_count });

    const createdPoints: Array<{ id: string; point: AgentKnowledgePoint }> = [];
    let totalPoints = 0;
    // 旧结构清理、课程元信息、新结构、前置关系必须在同一个 SQLite 事务里。
    runInTransaction(() => {
      clearCourseStructure(ownerId, courseId);
      deleteSourceBlocksExcept(ownerId, courseId, insertedBlockIds);
      if (result.course.title || result.course.subject || result.course.grade) {
        updateCourseMeta(ownerId, courseId, {
          title: result.course.title || course.title,
          subject: result.course.subject || course.subject,
          grade: result.course.grade || course.grade,
        });
      }
      const blockMap = new Map(storedBlocks.map((block) => [block.id, block]));
      const sourceName = source.source_name || course.source_name;
      let sectionOrdinal = 0;

    result.chapters.forEach((chapter, chapterIndex) => {
      const chapterBlockIds = [...new Set(chapter.sections.flatMap((section) => section.block_ids))];
      const chapterBlocks = chapterBlockIds.map((id) => blockMap.get(id)).filter((block): block is LearningSourceBlock => Boolean(block));
      const chapterUnit = createUnit({
        owner_id: ownerId,
        course_id: courseId,
        level: 'chapter',
        ordinal: chapterIndex + 1,
        title: chapter.title,
        summary: chapter.summary,
        source_ref: chapterBlocks.length ? [{
          material_id: materialId,
          source_name: sourceName,
          line_start: Math.min(...chapterBlocks.map((block) => block.line_start)),
          line_end: Math.max(...chapterBlocks.map((block) => block.line_end)),
          page: chapterBlocks[0]?.page ?? null,
        }] : [],
        status: chapter.sections.every((section) => section.status === 'parsed') ? 'parsed' : 'needs_review',
      });

      chapter.sections.forEach((section) => {
        sectionOrdinal += 1;
        const sectionBlocks = section.block_ids.map((id) => blockMap.get(id)).filter((block): block is LearningSourceBlock => Boolean(block));
        const designBlockIds = section.lesson_design ? [...new Set([
          ...section.lesson_design.preparation.flatMap((item) => item.block_ids),
          ...(section.lesson_design.introduction?.block_ids || []),
          ...section.lesson_design.teaching_flow.flatMap((item) => item.block_ids),
          ...section.lesson_design.examples.flatMap((item) => item.block_ids),
          ...section.lesson_design.misconceptions.flatMap((item) => item.block_ids),
          ...section.lesson_design.activities.flatMap((item) => item.block_ids),
          ...(section.lesson_design.summary?.block_ids || []),
          ...section.lesson_design.self_check.flatMap((item) => item.block_ids),
        ])] : [];
        const sectionRef = sectionBlocks.length ? [{
          material_id: materialId,
          source_name: sourceName,
          line_start: Math.min(...sectionBlocks.map((block) => block.line_start)),
          line_end: Math.max(...sectionBlocks.map((block) => block.line_end)),
          page: sectionBlocks[0]?.page ?? null,
          excerpt: sectionBlocks.find((block) => block.block_type === 'paragraph')?.text.slice(0, 300) || '',
          design_block_ids: designBlockIds,
        }] : [];
        const unit = createUnit({
          owner_id: ownerId,
          course_id: courseId,
          parent_id: chapterUnit.id,
          level: 'section',
          ordinal: sectionOrdinal,
          title: section.title,
          summary: section.summary,
          source_ref: sectionRef,
          status: section.status,
        });

        const lesson = createLesson({
          owner_id: ownerId,
          course_id: courseId,
          unit_id: unit.id,
          ordinal: sectionOrdinal,
          title: section.title,
          objective: section.objectives.join('；'),
          content: buildAgentLessonMarkdown({
            title: section.title,
            objectives: section.objectives,
            knowledgePoints: section.knowledge_points,
            teachingUnits: section.teaching_units,
            design: section.lesson_design,
            extraBlocks: sectionBlocks.filter((block) => (
              block.block_type !== 'heading'
              && (block.block_type === 'table'
                || block.block_type === 'image'
                || !section.knowledge_points.some((point) => point.block_ids.includes(block.id)))
            )),
            sourceLabel: sectionBlocks.length
              ? `${sourceName || '课程材料'} · 第 ${Math.min(...sectionBlocks.map((block) => block.line_start))}-${Math.max(...sectionBlocks.map((block) => block.line_end))} 行`
              : sourceName || '课程材料',
          }),
          key_points: section.knowledge_points.map((point) => point.title),
          teaching_units: section.teaching_units || [],
          source_refs: sectionRef,
          concepts: [...new Set(section.knowledge_points.flatMap((point) => point.concepts))],
          formulas: [...new Set(section.knowledge_points.flatMap((point) => point.formulas))],
          methods: [...new Set(section.knowledge_points.flatMap((point) => point.methods))],
          status: 'draft',
        });

        section.knowledge_points.forEach((point, pointIndex) => {
          const pointBlocks = point.block_ids.map((id) => blockMap.get(id)).filter((block): block is LearningSourceBlock => Boolean(block));
          const pointId = createKnowledgePoint({
            owner_id: ownerId,
            course_id: courseId,
            unit_id: unit.id,
            lesson_id: lesson.id,
            ordinal: pointIndex + 1,
            title: point.title,
            explanation: point.explanation,
            concepts: point.concepts,
            formulas: point.formulas,
            methods: point.methods,
            prerequisites: point.prerequisites,
            source_block_ids: point.block_ids,
            origin: point.origin,
            status: point.status,
            confidence: point.confidence,
          }).id;
          createdPoints.push({ id: pointId, point });
          totalPoints += 1;

        });

        // 练习只使用教学单元中的具体检查题/例题；没有可靠题目就不生成模板题。
        const firstSectionBlock = sectionBlocks[0];
        let unitExerciseOrdinal = 0;
        const usedCheckQuestions = new Set<string>();
        const usedExampleProblems = new Set<string>();
        (section.teaching_units || []).forEach((teachingUnit) => {
          teachingUnit.checks.slice(0, 2).forEach((check) => {
            const checkKey = check.question.replace(/\s+/g, '');
            if (usedCheckQuestions.has(checkKey)) return;
            usedCheckQuestions.add(checkKey);
            unitExerciseOrdinal += 1;
            createExercise({
              owner_id: ownerId,
              course_id: courseId,
              unit_id: unit.id,
              lesson_id: lesson.id,
              phase: 'in_class',
              ordinal: unitExerciseOrdinal,
              stem: check.question,
              answer: check.answer,
              explanation: `参考答案：${check.answer}`,
              difficulty: 'basic',
              source_type: source.source_type === 'textbook' ? 'textbook' : 'generated',
              validation_status: 'unverified',
              source_ref: {
                material_id: materialId, source_name: sourceName,
                page: firstSectionBlock?.page ?? null,
                line_start: firstSectionBlock?.line_start ?? 0,
                line_end: firstSectionBlock?.line_end ?? 0,
              },
            });
          });
          const exampleKey = teachingUnit.example?.problem.replace(/\s+/g, '') || '';
          if (teachingUnit.example && exampleKey && !usedExampleProblems.has(exampleKey)) {
            usedExampleProblems.add(exampleKey);
            unitExerciseOrdinal += 1;
            createExercise({
              owner_id: ownerId,
              course_id: courseId,
              unit_id: unit.id,
              lesson_id: lesson.id,
              phase: 'post_class',
              ordinal: unitExerciseOrdinal,
              stem: teachingUnit.example.problem,
              answer: teachingUnit.example.answer,
              explanation: [
                teachingUnit.example.thinking,
                ...teachingUnit.example.steps.map((step, index) => `${index + 1}. ${step.text}${step.reason ? `（${step.reason}）` : ''}`),
                `答案：${teachingUnit.example.answer}`,
              ].filter(Boolean).join('\n'),
              difficulty: 'application',
              source_type: 'generated',
              validation_status: 'unverified',
              source_ref: {
                material_id: materialId, source_name: sourceName,
                page: firstSectionBlock?.page ?? null,
                line_start: firstSectionBlock?.line_start ?? 0,
                line_end: firstSectionBlock?.line_end ?? 0,
              },
            });
          }
        });
      });
    });

    if (!totalPoints) {
      throw new LearningAgentError('NO_KNOWLEDGE_POINTS', '模型没有产出可验证的知识点');
    }

    // 前置知识点名称 → 有效知识点 ID；无效的已在 Agent 管线中移除。
    const pointIdByTitle = new Map<string, string>();
    createdPoints.forEach(({ id, point }) => {
      const key = point.title.replace(/\s+/g, '').toLowerCase();
      if (!pointIdByTitle.has(key)) pointIdByTitle.set(key, id);
    });
      createdPoints.forEach(({ id, point }) => {
        const prerequisiteIds = point.prerequisites
          .map((name) => pointIdByTitle.get(name.replace(/\s+/g, '').toLowerCase()))
          .filter((value): value is string => Boolean(value));
        updateKnowledgePointPrerequisites(ownerId, id, [...new Set(prerequisiteIds)]);
      });
      const sectionsWithoutPoints = result.chapters.reduce((sum, chapter) => (
        sum + chapter.sections.filter((section) => section.knowledge_points.length === 0).length
      ), 0);
      const needsReviewPoints = createdPoints.filter(({ point }) => point.status === 'needs_review').length;
      const structuralErrors = result.validation_errors.filter((item) => (
        item.includes('没有提取出')
        || item.includes('循环')
        || item.includes('重复知识点')
        || item.includes('没有有效原文块引用')
        // 标题块被自动归入小节是正常兜底；正文/表格/图片漏分组才是结构质量问题。
        || (item.includes('未被 Agent 分组') && !item.includes('（heading）'))
      )).length;
      const structureQuality: 'complete' | 'partial' | 'needs_review' = (
        sectionsWithoutPoints > 0 || structuralErrors > 0 || needsReviewPoints > 0
          ? (needsReviewPoints / Math.max(totalPoints, 1) > 0.5 || structuralErrors >= 3 ? 'needs_review' : 'partial')
          : 'complete'
      );
      const readability = source.audit?.readability;
      const qualityRank = { complete: 0, partial: 1, needs_review: 2 } as const;
      const readabilityLevel = readability?.level || 'complete';
      const parseQuality: 'complete' | 'partial' | 'needs_review' = (
        qualityRank[readabilityLevel] > qualityRank[structureQuality] ? readabilityLevel : structureQuality
      );
      const designStats = result.lesson_designs;
      const lessonQuality = !designStats
        ? 'complete'
        : (designStats.failed > 0 || designStats.needs_review > 0 || (designStats.part_failures || 0) > 0
          ? 'needs_review'
          : designStats.partial > 0 ? 'partial' : 'complete');
      const lessonCoverage = {
        ...summarizeLessonCoverage(createdPoints.map(({ point }) => point), result.chapters),
        failed_parts: designStats?.part_failures || 0,
      };
      const coverageLevel: 'complete' | 'partial' | 'needs_review' = !result.coverage
        ? 'complete'
        : (result.coverage.unprocessed_chars > 0 ? 'needs_review'
          : result.coverage.failed_segments > 0 ? 'partial' : 'complete');
      const overallRank = Math.max(
        qualityRank[parseQuality], qualityRank[lessonQuality], qualityRank[coverageLevel],
      ) as 0 | 1 | 2;
      const overallLevel: 'complete' | 'partial' | 'needs_review' = (['complete', 'partial', 'needs_review'] as const)[overallRank];
      const quality = {
        // 各环节分别记录；overall 才是所有环节的合并结果。
        level: overallLevel,
        outline: {
          level: structureQuality,
          chapters: result.chapters.length,
          sections: result.chapters.reduce((sum, chapter) => sum + chapter.sections.length, 0),
          structural_errors: structuralErrors,
        },
        knowledge_points: {
          level: structureQuality,
          total: totalPoints,
          needs_review: needsReviewPoints,
        },
        readability: readability || {
          level: 'complete', pages: 0, text_pages: 0, minor_issue_pages: 0,
          fallback_pages: 0, garbled_pages: 0, empty_pages: 0, unreadable_pages: 0,
          unreliable_pages: 0, skipped_visual_pages: 0, images_exported: 0, images_omitted: 0, images_oversize: 0,
        },
        lesson: {
          level: lessonQuality,
          ...(designStats || { total: 0, complete: 0, partial: 0, needs_review: 0, failed: 0, part_failures: 0 }),
          coverage: lessonCoverage,
        },
        coverage: result.coverage || {
          level: coverageLevel, total_blocks: 0, total_segments: 0, total_chars: 0,
          processed_segments: 0, processed_chars: 0, failed_segments: 0, failed_chars: 0,
          covered_chars: 0, unprocessed_chars: 0, points: totalPoints, cited_blocks: 0,
          failed_ranges: [], processed_ranges: [],
        },
        points: totalPoints,
        needs_review_points: needsReviewPoints,
        sections_without_points: sectionsWithoutPoints,
        structural_errors: structuralErrors,
      };
      const details = {
        model: options.config.model || options.config.provider,
        quality,
        batches: result.batch_count,
        validation_errors: result.validation_errors.slice(0, 100),
        duplicate_points: result.validation_errors.filter((item) => item.startsWith('重复知识点')).length,
        chapters: result.chapters.length,
        points: totalPoints,
        coverage: result.coverage,
        model_calls: result.model_calls || callStats,
        lesson_coverage: lessonCoverage,
      };
      // 课程 ready、解析质量、job done 与结构写入同一事务，避免提交后状态不一致。
      if (job) updateJob(ownerId, job.id, {
        status: 'done', stage: 'done', batch_count: result.batch_count, details, error: '',
      });
      logEvent('done', `课程生成完成，知识点 ${totalPoints} 个`);
      // 质量未完全通过时必须进入待审核，不能伪装成 ready。
      const finalStatus: 'ready' | 'needs_review' = overallLevel === 'complete' ? 'ready' : 'needs_review';
      updateCourseStatus(ownerId, courseId, {
        status: finalStatus, stage: finalStatus === 'ready' ? 'done' : 'needs_review', progress: 100, error: '', parse_quality: overallLevel, quality,
      });
    });
    persisted = true;
    return getCourse(ownerId, courseId);
  } catch (error) {
    const agentError = error instanceof LearningAgentError ? error : null;
    const message = error instanceof Error ? error.message.slice(0, 500) : '课程生成失败';
    logEvent('error', message);
    if (persisted) {
      // 新版本已经提交，不再把状态回退成旧版本描述。
      return getCourse(ownerId, courseId);
    }
    deleteSourceBlocksByIds(ownerId, insertedBlockIds);
    if (job) updateJob(ownerId, job.id, {
      status: 'failed',
      stage: 'failed',
      error: message,
      attempts: agentError?.attempts || job.attempts,
      details: { code: agentError?.code || 'GENERATION_FAILED', model_calls: callStats },
    });
    updateCourseStatus(ownerId, courseId, hadContent
      ? { status: 'ready', stage: 'previous', progress: 100, error: message }
      : { status: 'failed', stage: 'failed', progress: 0, error: message });
    return getCourse(ownerId, courseId);
  }
}
