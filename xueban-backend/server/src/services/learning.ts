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
  createExercise,
  createLesson,
  createUnit,
  getCourse,
  getLatestJob,
  updateCourseStatus,
  updateJob,
  type CourseSourceType,
  type LearningCourse,
} from '../storage/learning';
import { buildLessonMarkdown, parseStructuredDocument, type ParsedBlock } from './learning-parser';

export interface CourseSourceInput {
  source_type: CourseSourceType;
  source_name?: string;
  material_id?: string;
  text: string;
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

/**
 * 执行一次课程生成。真实 agent 接入后，只需要替换本函数内部实现，
 * 保持课程/目录/教案/练习的落库结构不变。
 */
export function runCourseGeneration(ownerId: string, courseId: string, source: CourseSourceInput): LearningCourse | undefined {
  const course = getCourse(ownerId, courseId);
  if (!course) return undefined;
  const job = getLatestJob(ownerId, courseId);
  const materialId = source.material_id || '';
  try {
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'outline', progress: 10 });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'outline' });
    clearCourseContent(ownerId, courseId);

    const parsed = parseStructuredDocument(source.text);
    const blockIds = new Map<number, string>();
    const blockById = new Map<number, ParsedBlock>();
    parsed.blocks.forEach((block) => {
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
      });
      blockIds.set(block.key, stored.id);
      blockById.set(block.key, block);
    });

    const totalSections = parsed.chapters.reduce((sum, chapter) => sum + chapter.sections.length, 0);
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'lessons' });
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'lessons', progress: 35, error: '' });

    const sourceName = source.source_name || course.source_name;
    let sectionOrdinal = 0;
    parsed.chapters.forEach((chapter, chapterIndex) => {
      const chapterRefs = [{
        material_id: materialId,
        source_name: sourceName,
        line_start: chapter.line_start,
        line_end: chapter.line_end,
      }];
      const chapterUnit = createUnit({
        owner_id: ownerId,
        course_id: courseId,
        level: 'chapter',
        ordinal: chapterIndex + 1,
        title: chapter.title,
        summary: `${chapter.sections.length} 个学习小节`,
        source_ref: chapterRefs,
        status: chapter.status,
      });

      chapter.sections.forEach((section) => {
        sectionOrdinal += 1;
        const sectionRefs = [{
          material_id: materialId,
          source_name: sourceName,
          page: section.blocks[0]?.page ?? null,
          line_start: section.line_start,
          line_end: section.line_end,
        }];
        const unit = createUnit({
          owner_id: ownerId,
          course_id: courseId,
          parent_id: chapterUnit.id,
          level: 'section',
          ordinal: sectionOrdinal,
          title: section.title,
          summary: section.blocks.find((block) => block.block_type === 'paragraph')?.text.slice(0, 120) || '',
          source_ref: sectionRefs,
          status: section.status,
        });

        const sourceLabel = `${sourceName || '课程材料'} · 第 ${section.line_start}-${section.line_end} 行`;
        const content = buildLessonMarkdown(section, sourceLabel);
        const lesson = createLesson({
          owner_id: ownerId,
          course_id: courseId,
          unit_id: unit.id,
          ordinal: sectionOrdinal,
          title: section.title,
          objective: section.objectives.join('；'),
          content,
          key_points: section.knowledge_points.map((point) => point.title),
          source_refs: [{
            material_id: materialId,
            source_name: sourceName,
            page: section.blocks[0]?.page ?? null,
            line_start: section.line_start,
            line_end: section.line_end,
            excerpt: section.blocks.find((block) => block.block_type === 'paragraph')?.text.slice(0, 300) || '',
          }],
          concepts: section.concepts,
          formulas: section.formulas,
          methods: section.methods,
          status: 'draft',
        });

        section.knowledge_points.forEach((point, pointIndex) => {
          const pointBlocks = point.block_keys
            .map((key) => blockById.get(key))
            .filter((block): block is ParsedBlock => Boolean(block));
          const pointSourceBlockIds = point.block_keys
            .map((key) => blockIds.get(key))
            .filter((id): id is string => Boolean(id));
          createKnowledgePoint({
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
            source_block_ids: pointSourceBlockIds,
            status: point.status,
            confidence: point.confidence,
          });

          const firstBlock = pointBlocks[0];
          const sourceType = source.source_type === 'textbook' ? 'textbook' : 'generated';
          const base = {
            owner_id: ownerId,
            course_id: courseId,
            unit_id: unit.id,
            lesson_id: lesson.id,
            source_type: sourceType,
            source_ref: {
              material_id: materialId,
              source_name: sourceName,
              page: firstBlock?.page ?? null,
              line_start: firstBlock?.line_start ?? section.line_start,
              line_end: firstBlock?.line_end ?? section.line_end,
              excerpt: point.explanation.slice(0, 200),
            },
          } as const;
          createExercise({
            ...base,
            phase: 'in_class',
            ordinal: pointIndex + 1,
            stem: `请用自己的话解释「${point.title}」，并举一个例子。`,
            explanation: '完成后对照本节教材原文，检查核心概念是否解释清楚。',
            validation_status: 'placeholder',
          });
          createExercise({
            ...base,
            phase: 'post_class',
            ordinal: pointIndex + 1,
            stem: `结合「${point.title}」，写出解决一类问题的关键步骤，并说明每一步的理由。`,
            explanation: '完成本节学习后作答，重点检查步骤是否完整、理由是否清楚。',
            validation_status: 'placeholder',
          });
        });
      });
    });
    void totalSections;

    if (job) updateJob(ownerId, job.id, { status: 'done', stage: 'done' });
    updateCourseStatus(ownerId, courseId, { status: 'ready', stage: 'done', progress: 100, error: '' });
    return getCourse(ownerId, courseId);
  } catch (error) {
    const message = error instanceof Error ? error.message.slice(0, 300) : '课程生成失败';
    if (job) updateJob(ownerId, job.id, { status: 'failed', stage: 'failed', error: message });
    updateCourseStatus(ownerId, courseId, { status: 'failed', stage: 'failed', progress: 0, error: message });
    return getCourse(ownerId, courseId);
  }
}
