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
7. JSON 输出必须紧凑，不要重复原文，不要输出无关解释；只输出合法 JSON，不要 Markdown 代码围栏。`;

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
  compact?: boolean;
}): string {
  const catalog = input.blocks.map((block) => {
    const location = block.page ? `第${block.page}页/第${block.line}行` : `第${block.line}行`;
    return `[${block.id}] (${block.type}, ${location}) ${block.text}`;
  }).join('\n').slice(0, input.compact ? 6_000 : 20_000);
  return `课程：${input.courseTitle}
章节：${input.chapterTitle}
小节：${input.sectionTitle}
${input.previousTitles?.length ? `已出现的知识点：${input.previousTitles.join('、')}\n` : ''}
请只依据以下原文块，提取本节学习目标与知识点。不得补充教材没有的内容。
每个知识点必须引用真实块 ID；没有依据就标记 needs_review 或不要输出。
公式统一使用 LaTeX：行内公式用 $...$，独立公式用 $$...$$；不要用 Unicode 拼公式。
${input.compact ? '本次必须极简输出：最多 4 个知识点，每个 explanation 不超过 150 字，objectives 最多 3 条；不要重复原文，不要展开长篇解释。\n' : ''}
${input.hasImages ? '本次还附带了教材中的原始图片/图表，图片与原文中的 image 块对应。请结合图片中的文字、公式、表格或图示理解知识点，并引用对应 image 块 ID。' : ''}

原文块：
${catalog}

${SECTION_SCHEMA}`;
}

export const LESSON_DESIGN_SCHEMA = `输出结构：
{
  "design_status": "complete" | "partial" | "needs_review",
  "objectives": [
    { "text": "学生学完本节后能完成的具体目标", "kind": "knowledge" | "method" | "application" }
  ],
  "preparation": [
    { "text": "需要回忆或准备的前置知识", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ],
  "introduction": {
    "text": "从教材问题、例子、现象或应用切入的导入",
    "source": "textbook" | "derived" | "supplemental" | "needs_review",
    "block_ids": ["真实块ID"]
  },
  "teaching_flow": [
    {
      "kind": "concept" | "formula" | "reading" | "experiment" | "timeline" | "comparison" | "application" | "activity",
      "title": "教学环节标题",
      "body": "面向学生的完整讲解，不是知识点罗列",
      "bullets": ["必要的要点"],
      "formulas": ["使用 LaTeX 的公式"],
      "source": "textbook" | "derived" | "supplemental" | "needs_review",
      "block_ids": ["真实块ID"]
    }
  ],
  "examples": [
    {
      "title": "例题标题",
      "problem": "题目",
      "steps": ["解题步骤"],
      "answer": "答案",
      "source": "textbook" | "derived" | "supplemental",
      "block_ids": ["真实块ID"]
    }
  ],
  "misconceptions": [
    { "text": "易错点或易混淆处", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ],
  "activities": [
    { "prompt": "具体的学习活动任务", "purpose": "该活动训练的理解过程", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ],
  "summary": { "text": "本节核心知识与逻辑关系", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] },
  "self_check": [
    { "question": "检查是否真正理解的问题", "answer_hint": "简短提示，不直接代替思考", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ]
}`;

export function buildLessonDesignPrompt(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  chapterTitle: string;
  sectionTitle: string;
  objectives: string[];
  knowledgePoints: Array<{
    title: string;
    explanation: string;
    concepts: string[];
    formulas: string[];
    methods: string[];
    origin: string;
    status: string;
  }>;
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null }>;
  hasImages?: boolean;
  compact?: boolean;
}): string {
  const catalog = input.blocks.map((block) => {
    const location = block.page ? `第${block.page}页/第${block.line}行` : `第${block.line}行`;
    return `[${block.id}] (${block.type}, ${location}) ${block.text}`;
  }).join('\n').slice(0, input.compact ? 6_000 : 20_000);
  const points = input.knowledgePoints.map((point) => (
    `- ${point.title}（${point.origin === 'textbook' ? '教材原文' : '由原文归纳'}，${point.status === 'parsed' ? '已确认' : '待确认'}）\n  ${point.explanation}\n  概念：${point.concepts.join('、') || '无'}\n  公式：${point.formulas.join('；') || '无'}\n  方法：${point.methods.join('、') || '无'}`
  )).join('\n');
  return `你正在为${input.grade || '该年级'}${input.subject || ''}学生设计一节可以独立阅读和思考的学案。
课程：${input.courseTitle}
章节：${input.chapterTitle}
小节：${input.sectionTitle}
${input.hasImages ? '本次附带教材原始图片，请结合图片中的文字、图形、公式或表格理解内容。\n' : ''}
现有学习目标：${input.objectives.join('；') || '未明确'}
已确认知识点：
${points || '暂无'}

请设计一份“教学学案”，不是知识点提取报告。要求：
1. 按学科和本节内容选择合适的教学组织顺序；数学可围绕概念、条件、公式、推导、例题展开，物理可围绕情境、物理量、规律、适用条件、实验展开，语文/英语/历史等也按各自学科特点组织。不要机械套用固定模板。
2. 至少包含：学习目标、学习准备、情境导入、核心知识学习、本节小结；尽量包含典型例题、易错提醒、学习活动和自我检查。每个部分要写给学生看，语言完整、具体。
3. 学习目标要区分知识理解、方法掌握、应用能力；学习准备只写教材明确要求或直接隐含的前置知识。
4. 核心讲解要按认知顺序展开，说明概念含义、适用条件和知识联系，不要把知识点重新排列一遍。
5. 例题优先使用教材原例；教材没有例题时，可以基于本节已明确知识补充简单示例，但必须标记 source="supplemental"，并给出步骤。教材原例标 source="textbook"。
6. 易错点必须有教材依据，或明确标记为 derived/supplemental；不得声称“学生普遍存在”某个没有依据的问题。
7. 学习活动要具体可执行，例如解释、比较、推导、归纳、应用；不要写“认真阅读”“积极思考”“小组讨论”这类空话。
8. 每一个关键内容都用 block_ids 引用真实原文块；教材直接提供 source="textbook"；由教材直接推导 source="derived"；教学补充 source="supplemental"；依据不足 source="needs_review"。
9. 不得编造教材没有的定义、公式、定理、例题或结论。公式统一 LaTeX：行内 $...$，独立 $$...$$。
10. 只输出合法 JSON，不要 Markdown 代码围栏，不要解释。
${input.compact ? '输出必须极简：teaching_flow 最多 4 项， examples 最多 1 个，activities 最多 2 个，self_check 最多 2 个；每段 body 不超过 180 字。\n' : ''}

原文块：
${catalog}

${LESSON_DESIGN_SCHEMA}`;
}
