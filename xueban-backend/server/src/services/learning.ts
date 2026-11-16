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
  clearCourseStructure,
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
import { LearningAgentError, parseTextbookWithAgent, type AgentCall, type AgentKnowledgePoint, type AgentModelConfig } from './learning-agent';

export interface CourseSourceInput {
  source_type: CourseSourceType;
  source_name?: string;
  material_id?: string;
  text: string;
  images?: Array<{ image_id?: string; mime: string; data_url: string; page?: number | null; context?: string }>;
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
export interface CourseGenerationOptions {
  config: AgentModelConfig;
  callImpl?: AgentCall;
}

function pointSourceLabel(sourceName: string, block?: LearningSourceBlock): string {
  if (!block) return sourceName || '课程材料';
  return `${sourceName || '课程材料'} · 第 ${block.line_start}-${block.line_end} 行`;
}

function buildAgentLessonMarkdown(input: {
  title: string;
  objectives: string[];
  knowledgePoints: AgentKnowledgePoint[];
  sourceLabel: string;
  extraBlocks?: LearningSourceBlock[];
}): string {
  const lines = ['> 试学版：本节依据教材原文整理，教案仍在完善。', '', '## 学习目标', ''];
  if (input.objectives.length) lines.push(...input.objectives.map((item) => `- ${item}`));
  else lines.push('本节学习目标尚未从教材中明确识别，待确认后再补充。');
  lines.push('', '## 知识点', '');
  if (!input.knowledgePoints.length) lines.push('本节暂未识别出可靠的知识点，请以教材原文为准。');
  input.knowledgePoints.forEach((point) => {
    lines.push(`### ${point.title}`, '');
    if (point.origin === 'agent_inferred') lines.push('> Agent 归纳：以下解释由原文归纳，需结合教材原文核对。', '');
    lines.push(point.explanation || '原文内容待确认。', '');
    if (point.concepts.length) lines.push('**核心概念**', '', ...point.concepts.map((item) => `- ${item}`), '');
    if (point.formulas.length) lines.push('**公式 / 表达**', '', ...point.formulas.map((item) => `- ${item}`), '');
    if (point.methods.length) lines.push('**方法 / 步骤**', '', ...point.methods.map((item) => `- ${item}`), '');
    if (point.prerequisites.length) lines.push('**前置知识**', '', ...point.prerequisites.map((item) => `- ${item}`), '');
  });
  if (input.extraBlocks?.length) {
    lines.push('## 补充原文', '');
    const seen = new Set<string>();
    input.extraBlocks.forEach((block) => {
      const value = block.block_type === 'table' || block.block_type === 'image' ? block.markdown : block.text;
      if (!value || seen.has(value)) return;
      seen.add(value);
      lines.push(value, '');
    });
  }
  lines.push('## 教材出处', '', `- ${input.sourceLabel}`, '');
  return lines.join('\n');
}

/**
 * 执行一次课程生成。
 * 程序负责原文块与持久化；Agent 负责语义结构；程序再校验并落库。
 */
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
  const insertedBlockIds: string[] = [];
  try {
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'extracting', progress: 8, error: '' });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'extracting' });

    // 1. 程序提取原文块并落库，Agent 只能引用这些块。
    const parsed = parseStructuredDocument(source.text);
    const blockIds = new Map<number, string>();
    const storedBlocks: LearningSourceBlock[] = [];
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

    // 2. Agent 分批解析；程序记录每次尝试和批次。
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'outline', progress: 15, error: '' });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'outline' });
    const result = await parseTextbookWithAgent({
      courseTitle: course.title,
      subject: course.subject,
      grade: course.grade,
      blocks: storedBlocks,
      config: options.config,
      callImpl: options.callImpl,
      imagesByBlock: imageMap,
      onProgress: (stage, current, total) => {
        const progress = Math.min(85, 15 + Math.round((current / Math.max(total, 1)) * 60));
        updateCourseStatus(ownerId, courseId, { status: 'generating', stage, progress });
        if (job) updateJob(ownerId, job.id, { status: 'running', stage: `${stage} ${current}/${total}` });
      },
      onAttempt: (attempt) => {
        if (job) updateJob(ownerId, job.id, { attempts: Math.max(job.attempts, attempt) });
      },
    });

    // 3. 程序校验通过后才替换旧结构；Agent 失败时旧内容保持可用。
    clearCourseStructure(ownerId, courseId);
    deleteSourceBlocksExcept(ownerId, courseId, insertedBlockIds);
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'persisting', progress: 88 });
    if (job) updateJob(ownerId, job.id, { status: 'running', stage: 'persisting', batch_count: result.batch_count });
    if (result.course.title || result.course.subject || result.course.grade) {
      updateCourseMeta(ownerId, courseId, {
        title: result.course.title || course.title,
        subject: result.course.subject || course.subject,
        grade: result.course.grade || course.grade,
      });
    }

    const blockMap = new Map(storedBlocks.map((block) => [block.id, block]));
    const sourceName = source.source_name || course.source_name;
    const createdPoints: Array<{ id: string; point: AgentKnowledgePoint }> = [];
    let sectionOrdinal = 0;
    let totalPoints = 0;

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
        const sectionRef = sectionBlocks.length ? [{
          material_id: materialId,
          source_name: sourceName,
          line_start: Math.min(...sectionBlocks.map((block) => block.line_start)),
          line_end: Math.max(...sectionBlocks.map((block) => block.line_end)),
          page: sectionBlocks[0]?.page ?? null,
          excerpt: sectionBlocks.find((block) => block.block_type === 'paragraph')?.text.slice(0, 300) || '',
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
              line_start: firstBlock?.line_start ?? sectionBlocks[0]?.line_start ?? 0,
              line_end: firstBlock?.line_end ?? sectionBlocks[0]?.line_end ?? 0,
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
      item.includes('没有提取出') || item.includes('未被 Agent 分组') || item.includes('循环') || item.includes('重复知识点') || item.includes('没有有效原文块引用')
    )).length;
    const parseQuality: 'complete' | 'partial' | 'needs_review' = (
      sectionsWithoutPoints > 0 || structuralErrors > 0 || needsReviewPoints > 0
        ? (needsReviewPoints / Math.max(totalPoints, 1) > 0.5 || structuralErrors >= 3 ? 'needs_review' : 'partial')
        : 'complete'
    );
    const quality = {
      level: parseQuality,
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
    };
    if (job) updateJob(ownerId, job.id, {
      status: 'done', stage: 'done', batch_count: result.batch_count, details, error: '',
    });
    updateCourseStatus(ownerId, courseId, {
      status: 'ready', stage: 'done', progress: 100, error: '', parse_quality: parseQuality, quality,
    });
    return getCourse(ownerId, courseId);
  } catch (error) {
    const agentError = error instanceof LearningAgentError ? error : null;
    const message = error instanceof Error ? error.message.slice(0, 500) : '课程生成失败';
    deleteSourceBlocksByIds(ownerId, insertedBlockIds);
    if (job) updateJob(ownerId, job.id, {
      status: 'failed',
      stage: 'failed',
      error: message,
      attempts: agentError?.attempts || job.attempts,
      details: { code: agentError?.code || 'GENERATION_FAILED' },
    });
    updateCourseStatus(ownerId, courseId, hadContent
      ? { status: 'ready', stage: 'previous', progress: 100, error: message }
      : { status: 'failed', stage: 'failed', progress: 0, error: message });
    return getCourse(ownerId, courseId);
  }
}
