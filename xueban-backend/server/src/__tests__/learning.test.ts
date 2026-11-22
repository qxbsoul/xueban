/**
 * 学习中心框架测试（2026-11-15）。
 * 只验证课程/目录/教案/练习/进度的结构与账号隔离，不验证真实解析与判题。
 */
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let store: typeof import('../storage/learning');
let service: typeof import('../services/learning');
let agent: typeof import('../services/learning-agent');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-learning-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  store = await import('../storage/learning');
  service = await import('../services/learning');
  agent = await import('../services/learning-agent');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

const fakeAgentCall: import('../services/learning-agent').AgentCall = async function* (_provider, input) {
  const ids = [...input.userMessage.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]);
  if (input.userMessage.includes('"chapters"')) {
    yield {
      type: 'text-delta',
      text: JSON.stringify({
        course: { title: '智能解析课程', subject: '数学', grade: '七年级', book_info: '' },
        chapters: [{
          title: '智能章节', summary: '依据原文归纳',
          sections: [{ title: '智能小节', summary: '依据原文归纳', block_ids: [...new Set(ids)] }],
        }],
      }),
    };
    return;
  }
  yield {
    type: 'text-delta',
    text: JSON.stringify({
      objectives: ['理解原文中的核心概念'],
      knowledge_points: [{
        title: '智能知识点', explanation: '依据教材原文整理。',
        concepts: [], formulas: [], methods: [], prerequisites: [],
        block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false,
      }],
    }),
  };
};

const agentOptions = {
  config: { provider: 'deepseek' as const, apiKey: 'test-key', model: 'test-model' },
  callImpl: fakeAgentCall,
};

describe('学习中心课程框架', () => {
  it('按章节标题拆分目录', () => {
    const sections = service.parseSections([
      '第一章 有理数',
      '正数和负数，数轴与相反数。',
      '第二章 整式',
      '单项式与多项式。',
    ].join('\n'), '数学教材');
    expect(sections.map((item) => item.title)).toEqual(['有理数', '整式']);
    expect(sections[0].body).toContain('正数和负数');
  });

  it('没有标题时按段落分组，至少生成一节', () => {
    const sections = service.parseSections('第一段内容。\n\n第二段内容。\n\n第三段内容。', '自定义课程');
    expect(sections.length).toBeGreaterThanOrEqual(1);
    expect(sections.every((item) => item.title.length > 0)).toBe(true);
  });

  it('生成课程后包含章节/小节/教案/课上与课后练习，状态 ready', async () => {
    const course = store.createCourse({
      owner_id: 'learner-a', title: '七年级数学', subject: '数学', grade: '七年级',
      source_type: 'textbook', source_name: '数学教材.txt',
    });
    store.createGenerationJob('learner-a', course.id);
    const result = await service.runCourseGeneration('learner-a', course.id, {
      source_type: 'textbook',
      source_name: '数学教材.txt',
      text: '第一章 有理数\n1.1 正数和负数\n正数和负数表示相反意义的量。\n1.2 有理数的加减法\n同号相加，异号相减。\n第二章 整式\n2.1 单项式\n单项式由数字和字母的积组成。',
    }, agentOptions);
    expect(result?.status).toBe('ready');

    const detail = store.getCourseDetail('learner-a', course.id)!;
    expect(detail.chapters).toHaveLength(2);
    expect(detail.chapters[0].sections.length).toBeGreaterThanOrEqual(2);
    expect(service.parseOutline('第一章 有理数\n1.1 正数和负数\n内容\n1.2 加减法\n内容', '数学')[0].sections).toHaveLength(2);
    const sectionOnly = service.parseOutline('1.1 正数\n内容\n1.2 负数\n内容', '数学');
    expect(sectionOnly).toHaveLength(1);
    expect(sectionOnly[0].title).toBe('课程目录');
    expect(sectionOnly[0].sections).toHaveLength(2);
    expect(detail.chapters[0].sections[0].lesson?.status).toBe('draft');
    expect(detail.chapters[0].sections[0].exercise_counts).toEqual({ in_class: 1, post_class: 1 });
    expect(detail.chapters[0].sections[0].knowledge_points.length).toBeGreaterThanOrEqual(1);
    expect(['parsed', 'needs_review']).toContain(detail.chapters[0].sections[0].knowledge_points[0].status);
    expect(detail.source_blocks.length).toBeGreaterThan(0);
    const firstSection = detail.chapters[0].sections[0];
    expect(firstSection.knowledge_points[0].lesson_id).toBe(firstSection.lesson?.id);
    expect(firstSection.knowledge_points[0].source_block_ids.length).toBeGreaterThan(0);
    expect(detail.source_blocks.some((block) => block.line_start > 0 && block.line_end >= block.line_start)).toBe(true);
    const lessonRefs = JSON.parse(firstSection.lesson?.source_refs_json || '[]');
    expect(lessonRefs[0].line_start).toBeGreaterThan(0);

    const exercises = store.listExercises('learner-a', { course_id: course.id });
    expect(exercises.every((item) => item.validation_status === 'placeholder')).toBe(true);
    expect(exercises.every((item) => item.answer === '')).toBe(true);
  });

  it('账号隔离：其他账号读不到课程与练习', async () => {
    const course = store.createCourse({
      owner_id: 'learner-b', title: '英语课程', source_type: 'description',
    });
    store.createGenerationJob('learner-b', course.id);
    await service.runCourseGeneration('learner-b', course.id, {
      source_type: 'description', text: '我想学习英语时态，从一般现在时开始，逐步学习过去时和将来时。',
    }, agentOptions);
    expect(store.getCourse('learner-b', course.id)).toBeTruthy();
    expect(store.getCourse('learner-a', course.id)).toBeUndefined();
    expect(store.getCourseDetail('learner-a', course.id)).toBeUndefined();
    expect(store.listExercises('learner-a', { course_id: course.id })).toHaveLength(0);
    expect(store.listExercises('learner-b', { course_id: course.id }).length).toBeGreaterThan(0);
  });

  it('完成本节后课后练习解锁；未完成前不能判为已解锁', async () => {
    const course = store.createCourse({ owner_id: 'learner-c', title: '物理', source_type: 'curriculum' });
    store.createGenerationJob('learner-c', course.id);
    await service.runCourseGeneration('learner-c', course.id, {
      source_type: 'curriculum', text: '第一章 运动\n速度与加速度。',
    }, agentOptions);
    const detail = store.getCourseDetail('learner-c', course.id)!;
    const lesson = detail.chapters[0].sections[0].lesson!;
    expect(store.isLessonCompleted('learner-c', lesson.id)).toBe(false);
    store.setLessonProgress('learner-c', course.id, lesson.id, 'completed');
    expect(store.isLessonCompleted('learner-c', lesson.id)).toBe(true);
  });
});


describe('Agent 解析失败与重试', () => {
  it('Agent 失败时课程标记 failed，重试成功后恢复 ready', async () => {
    const course = store.createCourse({ owner_id: 'learner-retry', title: '重试课程', source_type: 'textbook' });
    store.createGenerationJob('learner-retry', course.id);
    const failing = async function* () {
      throw new Error('模型暂时不可用');
    };
    const failed = await service.runCourseGeneration('learner-retry', course.id, {
      source_type: 'textbook', text: '第一章 测试\n正文内容。',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm', maxAttempts: 1 }, callImpl: failing as never });
    expect(failed?.status).toBe('failed');
    expect(store.getCourseDetail('learner-retry', course.id)?.chapters).toHaveLength(0);

    store.createGenerationJob('learner-retry', course.id);
    const ready = await service.runCourseGeneration('learner-retry', course.id, {
      source_type: 'textbook', text: '第一章 测试\n正文内容。',
    }, agentOptions);
    expect(ready?.status).toBe('ready');
    expect(store.getCourseDetail('learner-retry', course.id)?.chapters.length).toBeGreaterThan(0);
  });

  it('旧课程数据（只有课程/小节/教案）仍能读取，知识结构为空', () => {
    const course = store.createCourse({ owner_id: 'legacy', title: '旧课程', source_type: 'textbook' });
    const chapter = store.createUnit({ owner_id: 'legacy', course_id: course.id, level: 'chapter', ordinal: 1, title: '旧章' });
    const section = store.createUnit({ owner_id: 'legacy', course_id: course.id, parent_id: chapter.id, level: 'section', ordinal: 1, title: '旧节' });
    store.createLesson({
      owner_id: 'legacy', course_id: course.id, unit_id: section.id, ordinal: 1, title: '旧教案',
      objective: '', content: '旧内容', key_points: [], source_refs: [], status: 'draft',
    });
    const detail = store.getCourseDetail('legacy', course.id)!;
    expect(detail.chapters[0].sections[0].lesson?.title).toBe('旧教案');
    expect(detail.chapters[0].sections[0].knowledge_points).toEqual([]);
    expect(detail.source_blocks).toEqual([]);
  });
});


describe('解析质量与旧内容保护', () => {
  it('重新解析失败时保留上一次成功课程', async () => {
    const course = store.createCourse({ owner_id: 'learner-preserve', title: '保留旧版本', source_type: 'textbook' });
    store.createGenerationJob('learner-preserve', course.id);
    const ok = await service.runCourseGeneration('learner-preserve', course.id, {
      source_type: 'textbook', text: '第一章 旧章\n旧知识点内容在这里。',
    }, agentOptions);
    expect(ok?.status).toBe('ready');
    const before = store.getCourseDetail('learner-preserve', course.id)!;
    expect(before.chapters.length).toBeGreaterThan(0);
    const blocksBefore = before.source_blocks.length;

    store.createGenerationJob('learner-preserve', course.id);
    const failing = async function* () {
      throw new Error('Agent 中途失败');
    };
    const failed = await service.runCourseGeneration('learner-preserve', course.id, {
      source_type: 'textbook', text: '第一章 新章\n新的内容。',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm', maxAttempts: 1 }, callImpl: failing as never });
    expect(failed?.status).toBe('ready');
    const after = store.getCourseDetail('learner-preserve', course.id)!;
    expect(after.chapters[0].title).toBe(before.chapters[0].title);
    expect(after.chapters[0].sections[0].lesson?.title).toBe(before.chapters[0].sections[0].lesson?.title);
    expect(after.source_blocks.length).toBe(blocksBefore);
  });

  it('待确认知识点不会把课程标成完整解析', async () => {
    const reviewCall: import('../services/learning-agent').AgentCall = async function* (_provider, input) {
      const ids = [...input.userMessage.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          objectives: [],
          knowledge_points: [{
            title: '待确认知识点', explanation: '原文不完整', block_ids: [ids[0]],
            origin: 'agent_inferred', confidence: 0.4, needs_review: true,
          }],
        }),
      };
    };
    const course = store.createCourse({ owner_id: 'learner-quality', title: '质量测试', source_type: 'textbook' });
    store.createGenerationJob('learner-quality', course.id);
    const result = await service.runCourseGeneration('learner-quality', course.id, {
      source_type: 'textbook', text: '第一章 内容\n只有一段不完整的话。',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm' }, callImpl: reviewCall });
    expect(result?.status).toBe('ready');
    expect(result?.parse_quality).not.toBe('complete');
  });
});


describe('旧结构替换是原子的', () => {
  it('事务中途异常会回滚旧结构清理', () => {
    const course = store.createCourse({ owner_id: 'atomic', title: '原子测试', source_type: 'textbook' });
    const chapter = store.createUnit({ owner_id: 'atomic', course_id: course.id, level: 'chapter', ordinal: 1, title: '旧章' });
    const section = store.createUnit({ owner_id: 'atomic', course_id: course.id, parent_id: chapter.id, level: 'section', ordinal: 1, title: '旧节' });
    store.createLesson({ owner_id: 'atomic', course_id: course.id, unit_id: section.id, ordinal: 1, title: '旧教案', objective: '', content: '旧内容', key_points: [], source_refs: [] });
    expect(() => store.runInTransaction(() => {
      store.clearCourseStructure('atomic', course.id);
      store.createUnit({ owner_id: 'atomic', course_id: course.id, level: 'chapter', ordinal: 1, title: '新章' });
      throw new Error('写入失败');
    })).toThrow('写入失败');
    const detail = store.getCourseDetail('atomic', course.id)!;
    expect(detail.chapters).toHaveLength(1);
    expect(detail.chapters[0].title).toBe('旧章');
    expect(detail.chapters[0].sections[0].lesson?.title).toBe('旧教案');
  });
});


describe('真实持久化中断保护', () => {
  const targets = [
    'createUnit',
    'createLesson',
    'createKnowledgePoint',
    'createExercise',
    'updateKnowledgePointPrerequisites',
  ] as const;

  targets.forEach((target) => {
    it(`${target} 失败时完整保留旧课程`, async () => {
      const owner = `atomic-${target}`;
      const course = store.createCourse({ owner_id: owner, title: '真实原子测试', source_type: 'textbook' });
      store.createGenerationJob(owner, course.id);
      const first = await service.runCourseGeneration(owner, course.id, {
        source_type: 'textbook', text: '第一章 旧章\n旧内容。',
      }, agentOptions);
      expect(first?.status).toBe('ready');
      store.updateCourseMeta(owner, course.id, { title: '旧标题', subject: '旧学科', grade: '旧年级' });
      const lesson = store.getCourseDetail(owner, course.id)!.chapters[0].sections[0].lesson!;
      store.setLessonProgress(owner, course.id, lesson.id, 'completed');

      const beforeDetail = store.getCourseDetail(owner, course.id)!;
      const beforeCourse = store.getCourse(owner, course.id)!;
      const beforeExercises = store.listExercises(owner, { course_id: course.id });
      const beforeProgress = store.getProgress(owner, lesson.id);
      const beforeBlocks = beforeDetail.source_blocks.map((block) => block.id).sort();

      store.createGenerationJob(owner, course.id);
      const spy = vi.spyOn(store, target as 'createUnit').mockImplementationOnce((() => {
        throw new Error('模拟持久化失败');
      }) as never);
      try {
        const result = await service.runCourseGeneration(owner, course.id, {
          source_type: 'textbook', text: '第一章 新章\n新内容。',
        }, agentOptions);
        expect(result?.status).toBe('ready');
        expect(result?.error).toContain('模拟持久化失败');
      } finally {
        spy.mockRestore();
      }

      const afterDetail = store.getCourseDetail(owner, course.id)!;
      const afterCourse = store.getCourse(owner, course.id)!;
      expect(afterDetail.chapters[0].title).toBe(beforeDetail.chapters[0].title);
      expect(afterDetail.chapters[0].sections[0].lesson?.title).toBe(beforeDetail.chapters[0].sections[0].lesson?.title);
      expect(afterDetail.chapters[0].sections[0].knowledge_points[0].title)
        .toBe(beforeDetail.chapters[0].sections[0].knowledge_points[0].title);
      expect(store.listExercises(owner, { course_id: course.id }).map((item) => item.id).sort())
        .toEqual(beforeExercises.map((item) => item.id).sort());
      expect(store.getProgress(owner, lesson.id)?.status).toBe(beforeProgress?.status);
      expect(afterDetail.source_blocks.map((block) => block.id).sort()).toEqual(beforeBlocks);
      expect(afterCourse.title).toBe(beforeCourse.title);
      expect(afterCourse.subject).toBe(beforeCourse.subject);
      expect(afterCourse.grade).toBe(beforeCourse.grade);
    });
  });
});


describe('未分组内容块计入结构质量', () => {
  it('表格块未被 Agent 分组时不会误报 complete', async () => {
    const unassignedTable: import('../services/learning-agent').AgentCall = async function* (_provider, input) {
      const ids = [...input.userMessage.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]);
      if (input.userMessage.includes('"chapters"')) {
        // 故意漏掉最后一个 table 块
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids.slice(0, -1))] }] }] }) };
        return;
      }
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          objectives: [],
          knowledge_points: [{
            title: '正文知识点', explanation: '正文', block_ids: [ids[0]],
            origin: 'textbook', confidence: 0.9, needs_review: false,
          }],
        }),
      };
    };
    const course = store.createCourse({ owner_id: 'table-quality', title: '表格质量', source_type: 'textbook' });
    store.createGenerationJob('table-quality', course.id);
    const result = await service.runCourseGeneration('table-quality', course.id, {
      source_type: 'textbook',
      text: '第一章 章\n正文内容。\n| 概念 | 例子 |\n| --- | --- |\n| 正数 | +3 |',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm' }, callImpl: unassignedTable });
    expect(result?.status).toBe('ready');
    expect(result?.parse_quality).not.toBe('complete');
    const detail = store.getCourseDetail('table-quality', course.id)!;
    expect(detail.source_blocks.some((block) => block.block_type === 'table')).toBe(true);
  });
});


describe('教案公式', () => {
  it('formulas 字段会写成 KaTeX 可渲染的 $$...$$', async () => {
    const formulaCall: import('../services/learning-agent').AgentCall = async function* (_provider, input) {
      const ids = [...input.userMessage.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '章', sections: [{ title: '节', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          objectives: [],
          knowledge_points: [{
            title: '相反数', explanation: '互为相反数的两个数相加得零。',
            formulas: ['a + (-a) = 0'], concepts: [], methods: [], prerequisites: [],
            block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false,
          }],
        }),
      };
    };
    const course = store.createCourse({ owner_id: 'formula', title: '公式测试', source_type: 'textbook' });
    store.createGenerationJob('formula', course.id);
    const result = await service.runCourseGeneration('formula', course.id, {
      source_type: 'textbook', text: '第一章 公式\n互为相反数的两个数相加得零。',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm' }, callImpl: formulaCall });
    expect(result?.status).toBe('ready');
    const detail = store.getCourseDetail('formula', course.id)!;
    const lesson = detail.chapters[0].sections[0].lesson!;
    expect(lesson.content).toContain('$$a + (-a) = 0$$');
  });
});


describe('学案教学结构', () => {
  it('第二阶段教学设计会写进教案，而不是只拼接知识点', async () => {
    const designedCall: import('../services/learning-agent').AgentCall = async function* (_provider, input) {
      const ids = [...input.userMessage.matchAll(/\[([0-9a-f-]{36})\]/g)].map((match) => match[1]);
      if (input.userMessage.includes('"chapters"')) {
        yield { type: 'text-delta', text: JSON.stringify({ chapters: [{ title: '第一章', sections: [{ title: '1.1 相反数', block_ids: [...new Set(ids)] }] }] }) };
        return;
      }
      if (input.userMessage.includes('本任务只负责')) {
        yield { type: 'text-delta', text: JSON.stringify({
          design_status: 'complete',
          objectives: [{ text: '理解相反数的意义', kind: 'knowledge' }],
          preparation: [{ text: '回忆数轴上的正负方向', source: 'derived', block_ids: [ids[0]] }],
          introduction: { text: '从教材的温度问题观察相反方向的变化。', source: 'textbook', block_ids: [ids[0]] },
          teaching_flow: [{ kind: 'concept', title: '相反数', body: '在数轴上，位于原点两侧且到原点距离相等的两个数互为相反数。', bullets: [], formulas: ['$a+(-a)=0$'], source: 'textbook', block_ids: [ids[0]] }],
          examples: [{ title: '教材例题', problem: '求 5 的相反数。', steps: ['5 的相反数是 -5。'], answer: '-5', source: 'textbook', block_ids: [ids[0]] }],
          misconceptions: [{ text: '不要漏写负号。', source: 'derived', block_ids: [ids[0]] }],
          activities: [{ prompt: '用自己的话解释相反数并举一个数轴例子。', purpose: '训练概念解释', source: 'derived', block_ids: [ids[0]] }],
          summary: { text: '相反数关于原点对称。', source: 'textbook', block_ids: [ids[0]] },
          self_check: [{ question: '7 的相反数是多少？', answer_hint: '看原点的另一侧。', source: 'derived', block_ids: [ids[0]] }],
        }) };
        return;
      }
      yield { type: 'text-delta', text: JSON.stringify({
        objectives: ['理解相反数'],
        knowledge_points: [{ title: '相反数', explanation: '互为相反数相加为零。', concepts: [], formulas: ['a+(-a)=0'], methods: [], prerequisites: [], block_ids: [ids[0]], origin: 'textbook', confidence: 0.9, needs_review: false }],
      }) };
    };
    const course = store.createCourse({ owner_id: 'designed', title: '教学设计测试', source_type: 'textbook' });
    store.createGenerationJob('designed', course.id);
    const result = await service.runCourseGeneration('designed', course.id, {
      source_type: 'textbook', text: '第一章 相反数\n在数轴上，位于原点两侧且到原点距离相等的两个数互为相反数。',
    }, { config: { provider: 'deepseek', apiKey: 'k', model: 'm' }, callImpl: designedCall });
    expect(result?.status).toBe('ready');
    const lesson = store.getCourseDetail('designed', course.id)!.chapters[0].sections[0].lesson!;
    expect(lesson.content).toContain('## 情境导入');
    expect(lesson.content).toContain('## 核心学习');
    expect(lesson.content).toContain('## 典型例题');
    expect(lesson.content).toContain('## 自我检查');
    expect(lesson.content).toContain('$a+(-a)=0$');
    const quality = JSON.parse(result?.quality_json || '{}');
    expect(quality.lesson.coverage.total_points).toBeGreaterThan(0);
    expect(quality.lesson.coverage.covered_points).toBeGreaterThan(0);
    expect(quality.lesson.coverage.uncovered_points).toEqual([]);
  });
});


describe('原文可读性质量', () => {
  it('页面图像回退会降低课程解析质量并保留可读性统计', async () => {
    const course = store.createCourse({ owner_id: 'readability', title: '可读性测试', source_type: 'textbook' });
    store.createGenerationJob('readability', course.id);
    const result = await service.runCourseGeneration('readability', course.id, {
      source_type: 'textbook',
      text: '第一章 有理数\n正文内容。',
      audit: {
        format: 'pdf',
        pages: 3,
        readability: {
          level: 'partial', pages: 3, text_pages: 2, minor_issue_pages: 0,
          fallback_pages: 1, garbled_pages: 1, empty_pages: 0, unreadable_pages: 0,
          unreliable_pages: 1, skipped_visual_pages: 0, images_exported: 1, images_omitted: 0,
        },
      },
    }, agentOptions);
    expect(result?.status).toBe('ready');
    expect(result?.parse_quality).not.toBe('complete');
    const quality = JSON.parse(result?.quality_json || '{}');
    expect(quality.readability.level).toBe('partial');
    expect(quality.readability.fallback_pages).toBe(1);
    expect(quality.readability.text_pages).toBe(2);
  });
});


describe('学案完整性统计', () => {
  it('能识别未被学案覆盖的知识点', () => {
    const coverage = service.summarizeLessonCoverage([
      { title: '已覆盖知识点', block_ids: ['b1'] },
      { title: '未覆盖知识点', block_ids: ['b2'] },
    ], [{
      title: '章', summary: '',
      sections: [{
        title: '节', summary: '', block_ids: ['b1', 'b2'], objectives: [], knowledge_points: [], status: 'parsed',
        lesson_design: {
          objectives: [], preparation: [], introduction: null,
          teaching_flow: [{ kind: 'concept', title: '核心', body: '讲解', bullets: [], formulas: [], source: 'textbook', block_ids: ['b1'] }],
          examples: [], misconceptions: [], activities: [], summary: null, self_check: [], status: 'complete',
        },
      }],
    }]);
    expect(coverage.total_points).toBe(2);
    expect(coverage.covered_points).toBe(1);
    expect(coverage.uncovered_points).toEqual(['未覆盖知识点']);
  });
});
