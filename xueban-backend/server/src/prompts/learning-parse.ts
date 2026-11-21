/**
 * 教材解析 Agent 提示词（2026-11-16）。
 *
 * 提示词与业务逻辑分离：调整解析字段或规则时只改这里。
 * 模型名、provider、API Key 不在此文件出现。
 */

export const AGENT_SYSTEM_PROMPT = `你是一名严谨的教材结构化分析员。你只能依据用户提供的教材原文块工作。
你必须遵守：
1. 不得使用你自己的学科知识补充教材没有写出的内容。
2. 不得编造学习目标、公式、定义、例题或教材页码。
3. 每个知识点必须引用至少一个真实存在的原文块 ID。
4. 区分教材原文明确表达的内容（origin="textbook"）与你根据原文归纳的内容（origin="agent_inferred"）。
5. 无法确认的内容标记 needs_review=true，不要猜测。
6. 章节、小节、知识点是不同层级，不能把知识点当作章节标题。
7. 只输出合法 JSON，不要 Markdown 代码围栏，不要解释。`;

export const OUTLINE_SCHEMA = `输出结构：
{
  "course": { "title": "课程名", "subject": "学科", "grade": "年级", "book_info": "教材版本/册次等原文信息" },
  "chapters": [
    {
      "title": "章名",
      "summary": "本章概述，只能概括原文",
      "sections": [
        { "title": "小节名", "summary": "本节概述", "block_ids": ["块ID"] }
      ]
    }
  ]
}`;

export const SECTION_SCHEMA = `输出结构：
{
  "objectives": ["学习目标，只能来自原文或由原文直接归纳"],
  "knowledge_points": [
    {
      "title": "知识点名",
      "explanation": "知识点解释，尽量使用原文表述",
      "concepts": ["核心概念"],
      "formulas": ["公式或表达，使用 LaTeX：行内 $...$，独立公式 $$...$$"],
      "methods": ["解题方法或操作步骤"],
      "prerequisites": ["前置知识点名称，仅限本教材内已有或明确隐含的知识点"],
      "block_ids": ["至少一个真实原文块ID"],
      "origin": "textbook" 或 "agent_inferred",
      "confidence": 0.0,
      "needs_review": false
    }
  ]
}`;

export function buildOutlinePrompt(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null }>;
  hasImages?: boolean;
}): string {
  const catalog = input.blocks.map((block) => {
    const location = block.page ? `第${block.page}页/第${block.line}行` : `第${block.line}行`;
    return `[${block.id}] (${block.type}, ${location}) ${block.text.slice(0, 260)}`;
  }).join('\n');
  return `课程名称：${input.courseTitle}
学科：${input.subject || '未提供'}
年级：${input.grade || '未提供'}

请根据下面的教材原文块，识别课程、章节、小节层级。每个小节必须给出它包含的原文块 ID。
只能使用下面真实存在的块 ID。

${input.hasImages ? '本次还附带了教材中的原始图片/图表。请结合图片理解章节内容，但图片不能脱离原文块单独编造。\n\n' : ''}原文块：
${catalog}

${OUTLINE_SCHEMA}`;
}

export function buildSectionPrompt(input: {
  courseTitle: string;
  chapterTitle: string;
  sectionTitle: string;
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null }>;
  previousTitles?: string[];
  hasImages?: boolean;
}): string {
  const catalog = input.blocks.map((block) => {
    const location = block.page ? `第${block.page}页/第${block.line}行` : `第${block.line}行`;
    return `[${block.id}] (${block.type}, ${location}) ${block.text}`;
  }).join('\n');
  return `课程：${input.courseTitle}
章节：${input.chapterTitle}
小节：${input.sectionTitle}
${input.previousTitles?.length ? `已出现的知识点：${input.previousTitles.join('、')}\n` : ''}
请只依据以下原文块，提取本节学习目标与知识点。不得补充教材没有的内容。
每个知识点必须引用真实块 ID；没有依据就标记 needs_review 或不要输出。
公式统一使用 LaTeX：行内公式用 $...$，独立公式用 $$...$$；不要用 Unicode 拼公式。
${input.hasImages ? '本次还附带了教材中的原始图片/图表，图片与原文中的 image 块对应。请结合图片中的文字、公式、表格或图示理解知识点，并引用对应 image 块 ID。' : ''}

原文块：
${catalog}

${SECTION_SCHEMA}`;
}
