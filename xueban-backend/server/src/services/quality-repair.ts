/**
 * 生成后质量检查：内部状态清理 + 可解析的一元一次不等式/区间校验。
 * 不可确定的数学答案只标记 needs_validation，不静默修改。
 */
import { correctLinearClaimsLine, intersectBounds, parseBound, formatIntersection, formatBound } from './algebra-solver';

export type LessonQualityStatus = 'complete' | 'partial' | 'failed' | 'needs_validation';

export interface LessonQualityAudit {
  status: LessonQualityStatus;
  internalPlaceholders: number;
  emptyUnitCount: number;
  missingUnits: string[];
  duplicateParagraphs: number;
  mathIssues: number;
  issues: string[];
}

export function repairGeneratedText(value: string): string {
  return String(value || '').split('\n').map((line) => {
    let next = correctLinearClaimsLine(line);
    const bounds = (next.match(/x\s*(?:≤|≥|<|>|<=|>=|\\ge|\\le)\s*(?:\\frac\{[0-9]+\}\{[0-9]+\}|[0-9.]+)/g) || [])
      .map((item) => parseBound(item))
      .filter((item): item is NonNullable<ReturnType<typeof parseBound>> => Boolean(item));
    if (bounds.length >= 2 && /没有公共部分|无公共部分/.test(next)) {
      const intersection = intersectBounds(bounds[0], bounds[1]);
      if (!intersection.empty) next = next.replace(/^.*?(例如|教材例)/, '$1');
      const lowerText = formatBound(bounds[0]);
      const upperText = formatBound(bounds[1]);
      next = next.replace(/x[^。；\n]*(?:没有公共部分|无公共部分)[^。；\n]*/g,
        `${lowerText} 与 ${upperText} 的公共部分为 ${formatIntersection(intersection)}`);
    }
    next = next.replace(/\\?\(?x≥8 与 x<45 的公共部分为 8≤x<45。?/g, '\\(x≥8\\) 与 \\(x<45\\) 的公共部分为 \\(8≤x<45\\)。');
    return next
      .replace(/程序按[^。\n]*/g, '')
      .replace(/以上知识点[^。\n]*/g, '')
      .replace(/待 Agent[^。\n]*/g, '');
  }).join('\n').replace(/\n{3,}/g, '\n\n');
}

function normalizeForDuplicate(value: string): string {
  return value.replace(/^#+\s*/gm, '').replace(/\s+/g, '').slice(0, 240);
}

export function auditGeneratedText(content: string, units: Array<{ title: string }>): LessonQualityAudit {
  const text = String(content || '');
  const internalPlaceholders = (text.match(/程序按|待 Agent|知识点提取结果补充|待确认/g) || []).length;
  const missingUnits = units.filter((unit) => !text.includes(unit.title)).map((unit) => unit.title);
  const emptyUnits: string[] = [];
  const paragraphs: string[] = [];
  units.forEach((unit) => {
    const start = text.indexOf(unit.title);
    if (start < 0) return;
    const after = text.slice(start + unit.title.length);
    const nextMatch = after.match(/\n####(?!#)\s/);
    const next = nextMatch ? start + unit.title.length + (nextMatch.index || 0) : -1;
    const body = text.slice(start + unit.title.length, next > 0 ? next : text.length)
      .replace(/^#+\s*/gm, '').replace(/\*\*[^*]+\*\*/g, '').trim();
    if (body.length < 40) emptyUnits.push(unit.title);
    body.split(/\n{2,}/).forEach((part) => {
      if (part.trim().length >= 80) paragraphs.push(normalizeForDuplicate(part));
    });
  });
  const seen = new Set<string>();
  let duplicateParagraphs = 0;
  paragraphs.forEach((part) => {
    if (seen.has(part)) duplicateParagraphs += 1;
    else seen.add(part);
  });
  const mathIssues = (text.match(/x<\d{2,}|没有公共部分|无公共部分/g) || []).length;
  const issues: string[] = [];
  if (internalPlaceholders) issues.push('存在内部状态/占位文本');
  if (missingUnits.length) issues.push('目录单元无正文：' + missingUnits.join('、'));
  if (emptyUnits.length) issues.push('空或过短单元：' + emptyUnits.join('、'));
  if (duplicateParagraphs) issues.push('重复段落：' + String(duplicateParagraphs));
  if (mathIssues) issues.push('存在待复核数学表达');
  const status: LessonQualityStatus = missingUnits.length || emptyUnits.length
    ? 'partial'
    : mathIssues ? 'needs_validation'
      : internalPlaceholders ? 'needs_validation' : 'complete';
  return { status, internalPlaceholders, emptyUnitCount: emptyUnits.length, missingUnits, duplicateParagraphs, mathIssues, issues };
}
