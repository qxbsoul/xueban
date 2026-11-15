/**
 * 学习中心框架测试（2026-11-15）。
 * 只验证课程/目录/教案/练习/进度的结构与账号隔离，不验证真实解析与判题。
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp = '';
let origCwd = '';
let sqlite: typeof import('../storage/sqlite');
let store: typeof import('../storage/learning');
let service: typeof import('../services/learning');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-learning-'));
  process.chdir(tmp);
  sqlite = await import('../storage/sqlite');
  sqlite._resetForTests();
  store = await import('../storage/learning');
  service = await import('../services/learning');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

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

  it('生成课程后包含章节/小节/教案/课上与课后练习，状态 ready', () => {
    const course = store.createCourse({
      owner_id: 'learner-a', title: '七年级数学', subject: '数学', grade: '七年级',
      source_type: 'textbook', source_name: '数学教材.txt',
    });
    store.createGenerationJob('learner-a', course.id);
    const result = service.runCourseGeneration('learner-a', course.id, {
      source_type: 'textbook',
      source_name: '数学教材.txt',
      text: '第一章 有理数\n1.1 正数和负数\n正数和负数表示相反意义的量。\n1.2 有理数的加减法\n同号相加，异号相减。\n第二章 整式\n2.1 单项式\n单项式由数字和字母的积组成。',
    });
    expect(result?.status).toBe('ready');

    const detail = store.getCourseDetail('learner-a', course.id)!;
    expect(detail.chapters).toHaveLength(2);
    expect(detail.chapters[0].sections).toHaveLength(2);
    expect(service.parseOutline('第一章 有理数\n1.1 正数和负数\n内容\n1.2 加减法\n内容', '数学')[0].sections).toHaveLength(2);
    const sectionOnly = service.parseOutline('1.1 正数\n内容\n1.2 负数\n内容', '数学');
    expect(sectionOnly).toHaveLength(1);
    expect(sectionOnly[0].title).toBe('课程目录');
    expect(sectionOnly[0].sections).toHaveLength(2);
    expect(detail.chapters[0].sections[0].lesson?.status).toBe('draft');
    expect(detail.chapters[0].sections[0].exercise_counts).toEqual({ in_class: 1, post_class: 1 });
    expect(detail.chapters[0].sections[0].knowledge_points.length).toBeGreaterThanOrEqual(1);
    expect(detail.chapters[0].sections[0].knowledge_points[0].status).toBe('needs_review');
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

  it('账号隔离：其他账号读不到课程与练习', () => {
    const course = store.createCourse({
      owner_id: 'learner-b', title: '英语课程', source_type: 'description',
    });
    store.createGenerationJob('learner-b', course.id);
    service.runCourseGeneration('learner-b', course.id, {
      source_type: 'description', text: '我想学习英语时态，从一般现在时开始，逐步学习过去时和将来时。',
    });
    expect(store.getCourse('learner-b', course.id)).toBeTruthy();
    expect(store.getCourse('learner-a', course.id)).toBeUndefined();
    expect(store.getCourseDetail('learner-a', course.id)).toBeUndefined();
    expect(store.listExercises('learner-a', { course_id: course.id })).toHaveLength(0);
    expect(store.listExercises('learner-b', { course_id: course.id }).length).toBeGreaterThan(0);
  });

  it('完成本节后课后练习解锁；未完成前不能判为已解锁', () => {
    const course = store.createCourse({ owner_id: 'learner-c', title: '物理', source_type: 'curriculum' });
    store.createGenerationJob('learner-c', course.id);
    service.runCourseGeneration('learner-c', course.id, {
      source_type: 'curriculum', text: '第一章 运动\n速度与加速度。',
    });
    const detail = store.getCourseDetail('learner-c', course.id)!;
    const lesson = detail.chapters[0].sections[0].lesson!;
    expect(store.isLessonCompleted('learner-c', lesson.id)).toBe(false);
    store.setLessonProgress('learner-c', course.id, lesson.id, 'completed');
    expect(store.isLessonCompleted('learner-c', lesson.id)).toBe(true);
  });
});
