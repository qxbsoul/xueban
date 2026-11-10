/** 学生学段档案：年级随升学年（9 月 1 日）自动+1，学期按当前日期判断。 */

export type SchoolStage = 'primary' | 'junior' | 'senior' | 'college';

export interface StageDef {
  key: SchoolStage;
  label: string;
  grades: number;
  gradeName: (grade: number) => string;
}

const CN = ['一', '二', '三', '四', '五', '六'];
const JUNIOR = ['七', '八', '九'];

export const STAGES: StageDef[] = [
  { key: 'primary', label: '小学', grades: 6, gradeName: grade => `${CN[grade - 1] ?? grade}年级` },
  { key: 'junior', label: '初中', grades: 3, gradeName: grade => `${JUNIOR[grade - 1] ?? grade}年级` },
  { key: 'senior', label: '高中', grades: 3, gradeName: grade => `高${CN[grade - 1] ?? grade}` },
  { key: 'college', label: '大学', grades: 4, gradeName: grade => `大${CN[grade - 1] ?? grade}` },
];

export interface StudentProfile {
  stage: SchoolStage;
  grade: number;
  /** 设定时的 ISO 时间，用来计算之后自动升了几级。 */
  setAt: string;
}

export interface EffectiveProfile {
  stage: SchoolStage;
  stageLabel: string;
  grade: number;
  gradeName: string;
  semester: '上学期' | '下学期';
  advanced: number;
}

const KEY = 'sf_student_profile';
const PROMPT_KEY = 'sf_student_profile_prompted';

export function loadProfile(): StudentProfile | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StudentProfile>;
    const valid = STAGES.some(stage => stage.key === parsed.stage)
      && typeof parsed.grade === 'number' && parsed.grade >= 1;
    if (!valid) return null;
    return { stage: parsed.stage as SchoolStage, grade: parsed.grade as number, setAt: typeof parsed.setAt === 'string' ? parsed.setAt : new Date().toISOString() };
  } catch {
    return null;
  }
}

export function saveProfile(stage: SchoolStage, grade: number): StudentProfile {
  const profile: StudentProfile = { stage, grade, setAt: new Date().toISOString() };
  try { localStorage.setItem(KEY, JSON.stringify(profile)); } catch { /* 忽略存储异常 */ }
  return profile;
}

export function currentSemester(now = new Date()): '上学期' | '下学期' {
  const month = now.getMonth() + 1;
  // 9 月开学到次年 1 月是上学期，2 月到 8 月是下学期。
  return month >= 9 || month === 1 ? '上学期' : '下学期';
}

/** 每过一个 9 月 1 日升一级；跨学段自动进入下一学段的一年级。 */
export function effectiveProfile(profile: StudentProfile, now = new Date()): EffectiveProfile {
  let stageIndex = Math.max(0, STAGES.findIndex(stage => stage.key === profile.stage));
  let grade = Math.max(1, Math.min(profile.grade, STAGES[stageIndex].grades));
  const start = new Date(profile.setAt);
  let advanced = 0;
  for (let year = start.getFullYear(); year <= now.getFullYear(); year += 1) {
    const september = new Date(year, 8, 1, 0, 0, 0);
    if (september > start && september <= now) {
      advanced += 1;
      grade += 1;
      if (grade > STAGES[stageIndex].grades) {
        if (stageIndex < STAGES.length - 1) {
          stageIndex += 1;
          grade = 1;
        } else {
          grade = STAGES[stageIndex].grades;
        }
      }
    }
  }
  const stage = STAGES[stageIndex];
  return {
    stage: stage.key,
    stageLabel: stage.label,
    grade,
    gradeName: stage.gradeName(grade),
    semester: currentSemester(now),
    advanced,
  };
}

/** 给模型看的学情背景：学段、年级、学期，以及难度约束。 */
export function profileContext(now = new Date()): string {
  const profile = loadProfile();
  if (!profile) return '';
  const effective = effectiveProfile(profile, now);
  return [
    `【学生学段】${effective.stageLabel}${effective.gradeName} · ${effective.semester}（${now.getFullYear()} 年 ${now.getMonth() + 1} 月）`,
    '讲解深度、例题、练习题和用词都必须匹配这个学段；不要使用明显超出该学段的知识或术语，也不要出跨学段的难题。',
  ].join('\n');
}

export function needsDailyPrompt(): boolean {
  if (loadProfile()) return false;
  try {
    return localStorage.getItem(PROMPT_KEY) !== new Date().toISOString().slice(0, 10);
  } catch {
    return false;
  }
}

export function markDailyPrompt(): void {
  try { localStorage.setItem(PROMPT_KEY, new Date().toISOString().slice(0, 10)); } catch { /* 忽略存储异常 */ }
}
