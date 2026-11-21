/**
 * 教材解析 Agent 管线（2026-11-16）。
 *
 * 分工：
 * - blocks 由程序从原文提取并分配 ID；
 * - Agent 只负责语义组织（章节/小节/知识点/目标）；
 * - 程序负责 JSON 校验、块引用校验、重复与前置关系校验、循环检测、持久化。
 */
import { callProvider, PROVIDERS_NO_KEY, type ProviderId, type ProviderChunk } from '../transport/api-clients';
import type { LearningSourceBlock } from '../storage/learning';
import { buildOutlinePrompt, buildSectionPrompt, AGENT_SYSTEM_PROMPT } from '../prompts/learning-parse';

export interface AgentModelConfig {
  provider: ProviderId;
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  timeoutMs?: number;
  maxAttempts?: number;
}

export type AgentCall = (
  provider: ProviderId,
  input: {
    systemPrompt: string;
    userMessage: string;
    attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
    model?: string;
    api_key?: string;
    base_url?: string;
    max_tokens?: number;
    temperature?: number;
    thinking?: 'enabled' | 'disabled';
  },
) => AsyncIterable<ProviderChunk>;

export interface AgentKnowledgePoint {
  title: string;
  explanation: string;
  concepts: string[];
  formulas: string[];
  methods: string[];
  prerequisites: string[];
  block_ids: string[];
  origin: 'textbook' | 'agent_inferred';
  status: 'parsed' | 'needs_review';
  confidence: number;
}

export interface AgentSection {
  title: string;
  summary: string;
  block_ids: string[];
  objectives: string[];
  knowledge_points: AgentKnowledgePoint[];
  status: 'parsed' | 'needs_review';
}

export interface AgentChapter {
  title: string;
  summary: string;
  sections: AgentSection[];
}

export interface AgentCourseInfo {
  title: string;
  subject: string;
  grade: string;
  book_info: string;
}

export interface AgentParseResult {
  course: AgentCourseInfo;
  chapters: AgentChapter[];
  validation_errors: string[];
  batch_count: number;
}

export class LearningAgentError extends Error {
  code: string;
  attempts: number;
  constructor(code: string, message: string, attempts = 0) {
    super(message);
    this.name = 'LearningAgentError';
    this.code = code;
    this.attempts = attempts;
  }
}

const MAX_PROMPT_CHARS = 14_000;
const MAX_SECTION_CHARS = 10_000;

function chunkBlocks(blocks: LearningSourceBlock[], maxChars: number): LearningSourceBlock[][] {
  const chunks: LearningSourceBlock[][] = [];
  let current: LearningSourceBlock[] = [];
  let size = 0;
  blocks.forEach((block) => {
    const next = block.text.length + 100;
    if (current.length && size + next > maxChars) {
      chunks.push(current);
      current = [];
      size = 0;
    }
    current.push(block);
    size += next;
  });
  if (current.length) chunks.push(current);
  return chunks;
}

function compactBlocks(blocks: LearningSourceBlock[]) {
  return blocks.map((block) => ({
    id: block.id,
    type: block.block_type,
    text: block.text,
    line: block.line_start,
    page: block.page,
  }));
}

function parseJsonObject(text: string): Record<string, unknown> {
  const cleaned = String(text || '')
    .replace(/^\uFEFF/, '')
    // 部分模型把思考内容包在 <sf:thinking> 中；不能把思考里的示例 JSON 当成结果。
    .replace(/<sf:thinking>[\s\S]*?<\/sf:thinking>/gi, '')
    .replace(/<sf:analysis>[\s\S]*?<\/sf:analysis>/gi, '')
    .replace(/^```(?:json)?/i, '')
    .replace(/```$/i, '')
    .trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) {
    const preview = cleaned.slice(0, 160).replace(/\s+/g, ' ');
    throw new LearningAgentError('INVALID_JSON', preview ? `模型没有返回 JSON 对象：${preview}` : '模型没有返回 JSON 对象');
  }
  try {
    const parsed = JSON.parse(cleaned.slice(start, end + 1));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('not object');
    }
    return parsed as Record<string, unknown>;
  } catch {
    throw new LearningAgentError('INVALID_JSON', '模型返回的 JSON 无法解析');
  }
}

async function callAgentJson(input: {
  systemPrompt: string;
  userMessage: string;
  config: AgentModelConfig;
  callImpl?: AgentCall;
  attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<Record<string, unknown>> {
  const config = input.config;
  if (!config.apiKey && !PROVIDERS_NO_KEY.has(config.provider)) {
    throw new LearningAgentError('NO_API_KEY', '请先在设置中配置当前模型的 API Key');
  }
  const call = input.callImpl ?? callProvider;
  const maxAttempts = Math.max(1, Math.min(config.maxAttempts ?? 2, 3));
  let lastError: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    input.onAttempt?.(attempt);
    try {
      const chunks: string[] = [];
      const run = (async () => {
        for await (const chunk of call(config.provider, {
          systemPrompt: input.systemPrompt,
          userMessage: input.userMessage,
          attachments: input.attachments,
          model: config.model,
          api_key: config.apiKey,
          base_url: config.baseUrl,
          max_tokens: 8192,
          temperature: 0.1,
          thinking: 'disabled',
        })) {
          if (chunk.type === 'text-delta') chunks.push(chunk.text);
        }
        return chunks.join('');
      })();
      const timeoutMs = config.timeoutMs ?? 90_000;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const timeoutPromise = new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new LearningAgentError('TIMEOUT', '模型调用超时')), timeoutMs);
      });
      try {
        const text = await Promise.race([run, timeoutPromise]);
        return parseJsonObject(text);
      } finally {
        if (timer) clearTimeout(timer);
      }
    } catch (error) {
      lastError = error;
      input.onAttempt?.(attempt, error instanceof Error ? error.message : 'unknown');
      if (error instanceof LearningAgentError && error.code === 'NO_API_KEY') throw error;
      if (error instanceof LearningAgentError && error.code === 'INVALID_JSON' && attempt < maxAttempts) continue;
      if (attempt < maxAttempts) continue;
    }
  }
  if (lastError instanceof LearningAgentError) {
    lastError.attempts = maxAttempts;
    throw lastError;
  }
  throw new LearningAgentError('AGENT_FAILED', lastError instanceof Error ? lastError.message : '模型调用失败', maxAttempts);
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean)
    .slice(0, 30);
}

function normalizeTitle(value: string): string {
  return value.replace(/\s+/g, '').replace(/[：:；;，,。.、]/g, '').toLowerCase();
}

function validateOutline(raw: Record<string, unknown>, knownBlocks: Set<string>): {
  chapters: AgentChapter[];
  errors: string[];
} {
  const errors: string[] = [];
  const chaptersRaw = Array.isArray(raw.chapters) ? raw.chapters : [];
  const chapters: AgentChapter[] = [];
  chaptersRaw.slice(0, 60).forEach((chapterValue) => {
    if (!chapterValue || typeof chapterValue !== 'object') return;
    const chapter = chapterValue as Record<string, unknown>;
    const title = typeof chapter.title === 'string' ? chapter.title.trim().slice(0, 120) : '';
    if (!title) return;
    const sectionsRaw = Array.isArray(chapter.sections) ? chapter.sections : [];
    const sections: AgentSection[] = [];
    sectionsRaw.slice(0, 80).forEach((sectionValue) => {
      if (!sectionValue || typeof sectionValue !== 'object') return;
      const section = sectionValue as Record<string, unknown>;
      const sectionTitle = typeof section.title === 'string' ? section.title.trim().slice(0, 120) : '';
      if (!sectionTitle) return;
      const blockIds = asStringArray(section.block_ids).filter((id) => knownBlocks.has(id));
      if (!blockIds.length) {
        errors.push(`小节「${sectionTitle}」没有有效原文块引用`);
        return;
      }
      sections.push({
        title: sectionTitle,
        summary: typeof section.summary === 'string' ? section.summary.slice(0, 500) : '',
        block_ids: [...new Set(blockIds)],
        objectives: [],
        knowledge_points: [],
        status: 'needs_review',
      });
    });
    if (sections.length) {
      chapters.push({
        title,
        summary: typeof chapter.summary === 'string' ? chapter.summary.slice(0, 500) : '',
        sections,
      });
    }
  });
  return { chapters, errors };
}

function mergeOutlineChapters(target: AgentChapter[], incoming: AgentChapter[]): void {
  incoming.forEach((chapter) => {
    let existingChapter = target.find((item) => normalizeTitle(item.title) === normalizeTitle(chapter.title));
    if (!existingChapter) {
      existingChapter = { ...chapter, sections: [] };
      target.push(existingChapter);
    }
    if (!existingChapter.summary && chapter.summary) existingChapter.summary = chapter.summary;
    chapter.sections.forEach((section) => {
      const existing = existingChapter!.sections.find((item) => normalizeTitle(item.title) === normalizeTitle(section.title));
      if (existing) {
        existing.block_ids = [...new Set([...existing.block_ids, ...section.block_ids])];
        if (!existing.summary && section.summary) existing.summary = section.summary;
      } else {
        existingChapter!.sections.push({ ...section });
      }
    });
  });
}

export async function runAgentOutline(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  blocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
  onProgress?: (stage: string, batch: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<{ chapters: AgentChapter[]; course: AgentCourseInfo; errors: string[]; batchCount: number }> {
  const known = new Set(input.blocks.map((block) => block.id));
  const chunks = chunkBlocks(input.blocks, MAX_PROMPT_CHARS);
  const chapters: AgentChapter[] = [];
  const errors: string[] = [];
  let courseInfo: AgentCourseInfo = {
    title: input.courseTitle,
    subject: input.subject,
    grade: input.grade,
    book_info: '',
  };
  for (let index = 0; index < chunks.length; index += 1) {
    input.onProgress?.('outline', index + 1, chunks.length);
    const raw = await callAgentJson({
      systemPrompt: AGENT_SYSTEM_PROMPT,
      userMessage: buildOutlinePrompt({
        courseTitle: input.courseTitle,
        subject: input.subject,
        grade: input.grade,
        blocks: compactBlocks(chunks[index]),
        hasImages: Boolean(input.attachments?.length),
      }),
      config: input.config,
      callImpl: input.callImpl,
      attachments: input.attachments,
      onAttempt: input.onAttempt,
    });
    const rawCourse = raw.course && typeof raw.course === 'object' ? raw.course as Record<string, unknown> : {};
    if (index === 0) {
      courseInfo = {
        title: typeof rawCourse.title === 'string' && rawCourse.title.trim() ? rawCourse.title.trim().slice(0, 120) : input.courseTitle,
        subject: typeof rawCourse.subject === 'string' ? rawCourse.subject.trim().slice(0, 20) : input.subject,
        grade: typeof rawCourse.grade === 'string' ? rawCourse.grade.trim().slice(0, 20) : input.grade,
        book_info: typeof rawCourse.book_info === 'string' ? rawCourse.book_info.trim().slice(0, 300) : '',
      };
    }
    const validated = validateOutline(raw, known);
    errors.push(...validated.errors);
    mergeOutlineChapters(chapters, validated.chapters);
  }
  if (!chapters.length || !chapters.some((chapter) => chapter.sections.length)) {
    // 图片型/极短材料可能没有足够文字让模型判断目录；程序保留原文并标记待确认，
    // 仍让 Agent 在下一阶段尝试理解图片和文字。
    const heading = input.blocks.find((block) => block.block_type === 'heading');
    const chapterTitle = heading?.text.replace(/^#{1,3}\s*/, '').replace(/^第[一二三四五六七八九十百零\d]+[章节]\s*/, '').trim() || '课程内容';
    chapters.push({
      title: chapterTitle,
      summary: '模型未返回明确目录，程序按原文建立待确认结构',
      sections: [{
        title: heading?.text.replace(/^#{1,3}\s*/, '').trim() || '本节内容',
        summary: '',
        block_ids: input.blocks.map((block) => block.id),
        objectives: [],
        knowledge_points: [],
        status: 'needs_review',
      }],
    });
    errors.push('模型未返回章节结构，已保留原文并标记待确认');
  }
  // 程序兜底：Agent 漏掉的表格/图片/段落不能被静默丢弃，归入最近的原文小节。
  const allSections = chapters.flatMap((chapter) => chapter.sections);
  const used = new Set(allSections.flatMap((section) => section.block_ids));
  const byId = new Map(input.blocks.map((block) => [block.id, block]));
  input.blocks.forEach((block) => {
    if (used.has(block.id)) return;
    let nearest = allSections[0];
    let nearestLine = -1;
    allSections.forEach((section) => {
      const lines = section.block_ids
        .map((id) => byId.get(id)?.line_start ?? -1)
        .filter((line) => line >= 0);
      const maxLine = lines.length ? Math.max(...lines) : -1;
      if (maxLine <= block.line_start && maxLine >= nearestLine) {
        nearest = section;
        nearestLine = maxLine;
      }
    });
    nearest.block_ids.push(block.id);
    errors.push(`原文块 ${block.id}（${block.block_type}）未被 Agent 分组，已归入小节「${nearest.title}」并保留原文`);
  });
  return { chapters, course: courseInfo, errors, batchCount: chunks.length };
}

function validateSectionPoints(raw: Record<string, unknown>, sectionBlockIds: Set<string>): {
  objectives: string[];
  points: AgentKnowledgePoint[];
  errors: string[];
} {
  const errors: string[] = [];
  const objectives = asStringArray(raw.objectives);
  const pointsRaw = Array.isArray(raw.knowledge_points) ? raw.knowledge_points : [];
  const points: AgentKnowledgePoint[] = [];
  pointsRaw.slice(0, 40).forEach((pointValue) => {
    if (!pointValue || typeof pointValue !== 'object') return;
    const point = pointValue as Record<string, unknown>;
    const title = typeof point.title === 'string' ? point.title.trim().slice(0, 120) : '';
    const explanation = typeof point.explanation === 'string' ? point.explanation.trim().slice(0, 5000) : '';
    if (!title) return;
    const blockIds = asStringArray(point.block_ids).filter((id) => sectionBlockIds.has(id));
    if (!blockIds.length) {
      errors.push(`知识点「${title}」没有有效原文块引用，已丢弃`);
      return;
    }
    const origin = point.origin === 'agent_inferred' ? 'agent_inferred' : 'textbook';
    const confidenceRaw = typeof point.confidence === 'number' ? point.confidence : 0.5;
    const confidence = Math.max(0, Math.min(1, confidenceRaw));
    const needsReview = point.needs_review === true || confidence < 0.6;
    points.push({
      title,
      explanation,
      concepts: asStringArray(point.concepts),
      formulas: asStringArray(point.formulas),
      methods: asStringArray(point.methods),
      prerequisites: asStringArray(point.prerequisites),
      block_ids: [...new Set(blockIds)],
      origin,
      status: needsReview ? 'needs_review' : 'parsed',
      confidence,
    });
  });
  return { objectives, points, errors };
}

export async function runAgentSections(input: {
  courseTitle: string;
  chapters: AgentChapter[];
  allBlocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  imagesByBlock?: Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>;
  onProgress?: (stage: string, section: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<{ chapters: AgentChapter[]; errors: string[]; batchCount: number }> {
  const blockMap = new Map(input.allBlocks.map((block) => [block.id, block]));
  const errors: string[] = [];
  let batchCount = 0;
  const totalSections = input.chapters.reduce((sum, chapter) => sum + chapter.sections.length, 0);
  const tasks: Array<{ chapter: AgentChapter; section: AgentSection }> = [];
  input.chapters.forEach((chapter) => chapter.sections.forEach((section) => tasks.push({ chapter, section })));
  let nextTask = 0;
  let done = 0;

  const runOne = async (chapter: AgentChapter, section: AgentSection): Promise<void> => {
    const sectionBlocks = section.block_ids
      .map((id) => blockMap.get(id))
      .filter((block): block is LearningSourceBlock => Boolean(block));
    if (!sectionBlocks.length) {
      errors.push(`小节「${section.title}」没有可读取原文，已标记待确认`);
      section.status = 'needs_review';
      return;
    }
    const sectionIds = new Set(sectionBlocks.map((block) => block.id));
    const sectionImages = [...new Set(section.block_ids.flatMap((id) => input.imagesByBlock?.get(id) || []))];
    const chunks = chunkBlocks(sectionBlocks, MAX_SECTION_CHARS);
    const allPoints: AgentKnowledgePoint[] = [];
    const allObjectives: string[] = [];
    for (let index = 0; index < chunks.length; index += 1) {
      batchCount += 1;
      input.onProgress?.('sections', done, totalSections);
      const raw = await callAgentJson({
        systemPrompt: AGENT_SYSTEM_PROMPT,
        userMessage: buildSectionPrompt({
          courseTitle: input.courseTitle,
          chapterTitle: chapter.title,
          sectionTitle: section.title,
          blocks: compactBlocks(chunks[index]),
          previousTitles: allPoints.map((point) => point.title),
          hasImages: sectionImages.length > 0,
        }),
        config: input.config,
        callImpl: input.callImpl,
        attachments: sectionImages,
        onAttempt: input.onAttempt,
      });
      const validated = validateSectionPoints(raw, sectionIds);
      allObjectives.push(...validated.objectives);
      allPoints.push(...validated.points);
      errors.push(...validated.errors);
    }

    // 同一小节内去重：同名知识点合并来源块。
    const merged = new Map<string, AgentKnowledgePoint>();
    allPoints.forEach((point) => {
      const key = normalizeTitle(point.title);
      const existing = merged.get(key);
      if (!existing) {
        merged.set(key, point);
        return;
      }
      existing.block_ids = [...new Set([...existing.block_ids, ...point.block_ids])];
      existing.concepts = [...new Set([...existing.concepts, ...point.concepts])];
      existing.formulas = [...new Set([...existing.formulas, ...point.formulas])];
      existing.methods = [...new Set([...existing.methods, ...point.methods])];
      existing.prerequisites = [...new Set([...existing.prerequisites, ...point.prerequisites])];
      existing.confidence = Math.max(existing.confidence, point.confidence);
      if (point.origin === 'textbook') existing.origin = 'textbook';
      if (point.status === 'parsed') existing.status = 'parsed';
    });
    section.objectives = [...new Set(allObjectives)].slice(0, 20);
    section.knowledge_points = [...merged.values()].slice(0, 40);
    section.status = section.knowledge_points.some((point) => point.status === 'parsed')
      ? 'parsed'
      : 'needs_review';
    if (!section.knowledge_points.length) {
      errors.push(`小节「${section.title}」没有提取出可验证的知识点，已标记待确认`);
    }
  };

  const worker = async (): Promise<void> => {
    while (true) {
      const index = nextTask;
      nextTask += 1;
      if (index >= tasks.length) return;
      const task = tasks[index];
      await runOne(task.chapter, task.section);
      done += 1;
      input.onProgress?.('sections', done, totalSections);
    }
  };

  const concurrency = Math.min(3, Math.max(1, tasks.length));
  await Promise.all(Array.from({ length: concurrency }, () => worker()));
  return { chapters: input.chapters, errors, batchCount };
}

/** 全局校验：重复知识点、未知前置、循环依赖。 */
export function validateAgentGraph(chapters: AgentChapter[]): {
  errors: string[];
  duplicates: string[];
  cycles: string[];
} {
  const errors: string[] = [];
  const duplicates: string[] = [];
  const points = chapters.flatMap((chapter) => chapter.sections.flatMap((section) => section.knowledge_points));
  const titleMap = new Map<string, AgentKnowledgePoint[]>();
  points.forEach((point) => {
    const key = normalizeTitle(point.title);
    titleMap.set(key, [...(titleMap.get(key) || []), point]);
  });
  titleMap.forEach((items, key) => {
    if (items.length > 1) {
      duplicates.push(items[0].title);
      items.forEach((point) => { point.status = 'needs_review'; });
      void key;
    }
  });

  const byTitle = new Map<string, AgentKnowledgePoint>();
  points.forEach((point) => {
    const key = normalizeTitle(point.title);
    if (!byTitle.has(key)) byTitle.set(key, point);
  });
  points.forEach((point) => {
    point.prerequisites = point.prerequisites.filter((name) => {
      const key = normalizeTitle(name);
      if (!byTitle.has(key) || key === normalizeTitle(point.title)) {
        errors.push(`知识点「${point.title}」的前置「${name}」不存在，已移除`);
        return false;
      }
      return true;
    });
  });

  // 循环检测：DFS；检测到环时清除相关前置并标记待确认。
  const cycles: string[] = [];
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (key: string, path: string[]): void => {
    if (visiting.has(key)) {
      const cycle = [...path.slice(path.indexOf(key)), key];
      cycles.push(cycle.map((item) => byTitle.get(item)?.title || item).join(' → '));
      byTitle.get(key)!.prerequisites = [];
      byTitle.get(key)!.status = 'needs_review';
      return;
    }
    if (visited.has(key)) return;
    visiting.add(key);
    const point = byTitle.get(key);
    point?.prerequisites.forEach((name) => visit(normalizeTitle(name), [...path, key]));
    visiting.delete(key);
    visited.add(key);
  };
  [...byTitle.keys()].forEach((key) => visit(key, []));

  const needsReview = points.filter((point) => point.status === 'needs_review').length;
  if (points.length > 0 && needsReview / points.length > 0.5) {
    errors.push(`待确认知识点占比过高：${needsReview}/${points.length}`);
  }
  return { errors, duplicates, cycles };
}

export async function parseTextbookWithAgent(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  blocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  imagesByBlock?: Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>;
  onProgress?: (stage: string, current: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<AgentParseResult> {
  const outline = await runAgentOutline({
    courseTitle: input.courseTitle,
    subject: input.subject,
    grade: input.grade,
    blocks: input.blocks,
    config: input.config,
    callImpl: input.callImpl,
    onProgress: input.onProgress,
    onAttempt: input.onAttempt,
  });
  const sections = await runAgentSections({
    courseTitle: input.courseTitle,
    chapters: outline.chapters,
    allBlocks: input.blocks,
    config: input.config,
    callImpl: input.callImpl,
    imagesByBlock: input.imagesByBlock,
    onProgress: input.onProgress,
    onAttempt: input.onAttempt,
  });
  const graph = validateAgentGraph(sections.chapters);
  const validationErrors = [...outline.errors, ...sections.errors, ...graph.errors];
  graph.duplicates.forEach((title) => validationErrors.push(`重复知识点：${title}`));
  graph.cycles.forEach((cycle) => validationErrors.push(`前置知识点存在循环：${cycle}`));
  return {
    course: outline.course,
    chapters: sections.chapters,
    validation_errors: validationErrors,
    batch_count: outline.batchCount + sections.batchCount,
  };
}
