/**
 * 教材解析与学案生成提示词（2026-11-22 分段版）。
 *
 * 不在这里做原文截断；原文覆盖由 learning-agent 的 segment 分块负责。
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

function formatBlock(block: {
  id: string;
  type: string;
  text: string;
  line: number;
  page: number | null;
  offset_start?: number;
  offset_end?: number;
  status?: string;
  table_status?: string;
}): string {
  const location = block.page ? `第${block.page}页/第${block.line}行` : `第${block.line}行`;
  const range = typeof block.offset_start === 'number' && typeof block.offset_end === 'number'
    ? `字符${block.offset_start}-${block.offset_end}`
    : '';
  const uncertainTable = block.type === 'table' && block.table_status !== 'content_verified';
  const warning = uncertainTable ? ' 【表格未完成内容核验；其中数字和结论不得当作已确认事实，只能作为待核对线索】' : '';
  return `[${block.id}] (${block.type}, ${location}${range ? `, ${range}` : ''})${warning} ${block.text}`;
}

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
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null; offset_start?: number; offset_end?: number; status?: string; table_status?: string }>;
  hasImages?: boolean;
}): string {
  const catalog = input.blocks.map(formatBlock).join('\n');
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
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null; offset_start?: number; offset_end?: number; status?: string; table_status?: string }>;
  previousTitles?: string[];
  hasImages?: boolean;
}): string {
  const catalog = input.blocks.map(formatBlock).join('\n');
  return `课程：${input.courseTitle}
章节：${input.chapterTitle}
小节：${input.sectionTitle}
${input.previousTitles?.length ? `已出现的知识点：${input.previousTitles.join('、')}\n` : ''}
请只依据以下原文块，提取本节学习目标与知识点。不得补充教材没有的内容。
每个知识点必须引用真实块 ID；没有依据就标记 needs_review 或不要输出。
如果当前输入是同一小节的一个片段，只提取本片段能支持的知识点；片段范围会在原文块标记中给出，不要假设片段之外的内容。
公式统一使用 LaTeX：行内公式用 $...$，独立公式用 $$...$$；不要用 Unicode 拼公式。
如果教材用例子、填空或探究问题引入概念但没有写出结论，必须根据这些原文证据归纳结论，origin 标记为 agent_inferred，并在 explanation 中说明“由哪些例子/问题归纳”；不要只写“本片段未给出结论”。
${input.hasImages ? '本次还附带了教材中的原始图片/图表，图片与原文中的 image 块对应。请结合图片中的文字、公式、表格或图示理解知识点，并引用对应 image 块 ID。' : ''}

原文块：
${catalog}

${SECTION_SCHEMA}`;
}

export type LessonDesignPart = 'A' | 'B' | 'C1' | 'C2' | 'D1' | 'D2';

export const LESSON_PART_SCHEMAS: Record<LessonDesignPart, string> = {
  A: `输出结构：
{
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
  }
}`,
  B: `输出结构：
{
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
  ]
}`,
  C1: `输出结构：
{
  "examples": [
    {
      "title": "例题标题",
      "problem": "题目",
      "steps": ["解题步骤"],
      "answer": "答案",
      "source": "textbook" | "derived" | "supplemental",
      "block_ids": ["真实块ID"]
    }
  ]
}`,
  C2: `输出结构：
{
  "misconceptions": [
    { "text": "易错点或易混淆处", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ]
}`,
  D1: `输出结构：
{
  "activities": [
    { "prompt": "具体的学习活动任务", "purpose": "该活动训练的理解过程", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ]
}`,
  D2: `输出结构：
{
  "summary": { "text": "本节核心知识与逻辑关系", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] },
  "self_check": [
    { "question": "检查是否真正理解的问题", "answer_hint": "简短提示，不直接代替思考", "source": "textbook" | "derived" | "supplemental" | "needs_review", "block_ids": ["真实块ID"] }
  ]
}`,
};

const PART_INSTRUCTIONS: Record<LessonDesignPart, string> = {
  A: `本任务只生成教学目标、学习准备和情境导入。
- 学习目标区分知识理解、方法掌握、应用能力；
- 学习准备只写教材明确要求或直接隐含的前置知识；
- 情境导入优先从教材已有问题、例子、现象或应用切入；没有就说明教材未提供，不要编造生活情境。`,
  B: `本任务只生成核心知识学习环节。
- 按学科和本节内容组织认知顺序，不要机械套统一模板；
- 数学说明概念、条件、公式和联系；物理说明情境、物理量、规律和适用条件；语文/英语/历史按各自学科方式组织；
- 一个环节可以覆盖多个知识点，但每个重要知识点都要能找到对应讲解；
- 讲解要完整，不能只写要点；不要因为知识点多就压缩教学内容；
- 教材没有的内容不要编造，确实为教学补充时必须标 supplemental。`,
  C1: `本任务只生成典型例题。
- 优先使用教材原例，source="textbook"；
- 教材没有例题时，可以补充能由本节已学内容推出的简单示例，source="supplemental"；
- 例题必须给出必要步骤，不能只给答案；
- 如果本学科不适合数学式例题（如语文阅读、历史材料），返回空 examples 即可，不要硬造。`,
  C2: `本任务只生成易错提醒。
- 必须有教材依据或明确标记 derived/supplemental；
- 不得编造“学生普遍存在”的问题。`,
  D1: `本任务只生成学习活动。
- 活动要具体可执行：解释、比较、推导、归纳、应用或材料分析；
- 禁止空泛的“认真阅读”“积极思考”“小组讨论”。`,
  D2: `本任务只生成本节小结和自我检查。
- 小结总结核心知识及其逻辑关系；
- 自我检查给出少量判断是否真正理解的问题，不接入正式练习系统。`,
};

export function buildLessonDesignPartPrompt(input: {
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
  blocks: Array<{ id: string; type: string; text: string; line: number; page: number | null; offset_start?: number; offset_end?: number; status?: string; table_status?: string }>;
  hasImages?: boolean;
}, part: LessonDesignPart): string {
  const catalog = input.blocks.map(formatBlock).join('\n');
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

本任务只负责学案的一个部分。${PART_INSTRUCTIONS[part]}
每个关键内容都用 block_ids 引用真实原文块；教材直接提供 source="textbook"；由教材直接推导 source="derived"；教学补充 source="supplemental"；依据不足 source="needs_review"。
不得编造教材没有的定义、公式、定理、例题或结论。公式统一 LaTeX：行内 $...$，独立 $$...$$。
只输出合法 JSON，不要 Markdown 代码围栏，不要解释。

原文块：
${catalog}

${LESSON_PART_SCHEMAS[part]}`;
}
