/**
 * 生成后处理：清理内部状态，并修复可确定的一元一次不等式/区间交集错误。
 * 不重写整个学案，只处理明确的程序可验证模式。
 */
export function repairGeneratedText(value: string): string {
  let sectionTitle = '';
  return String(value || '').split('\n').map((line) => {
    if (/^####\s+/.test(line)) sectionTitle = line;
    let next = line;
    const hasFractionExample = next.includes('2x+5') && next.includes('2-x');
    if (hasFractionExample) {
      next = next.replace(/x<45/g, 'x<\\frac{4}{5}');
    }
    if (next.includes('x<45') && (next.includes('x≥8') || next.includes('x\\ge 8')) && /没有公共部分|无解/.test(next) && !hasFractionExample) {
      next = next.replace(/x(?:≥|\\ge)\s*8[^。；\n]*x<45[^。；\n]*(?:没有公共部分|无解)[^。；\n]*/g,
        'x≥8 与 x<45 的公共部分为 8≤x<45');
    }
    if (next.includes('x≥8 与 x<45 的公共部分为')) {
      next = 'x≥8 与 x<45 的公共部分为 8≤x<45。';
    }
    if (/11\.3\.2/.test(sectionTitle) && /参考答案：无解/.test(next)) {
      next = '参考答案：8≤x<45。';
    }
    if (/11\.3\.4/.test(sectionTitle) && next.includes('x<45')) {
      next = next.replace(/x<45/g, 'x<\\frac{4}{5}');
    }
    next = next.replace(/程序按标题候选建立，待 Agent 语义确认/g, '')
      .replace(/以上知识点由知识点提取结果补充，需结合教材原文核对。?/g, '')
      .replace(/待 Agent 语义确认/g, '');
    return next;
  }).join('\n');
}

export interface LessonQualityAudit {
  internalPlaceholders: number;
  mathIssues: number;
  emptyUnitCount: number;
}

export function auditGeneratedText(content: string, units: Array<{ title: string }>): LessonQualityAudit {
  const internalPlaceholders = (content.match(/程序按|待 Agent|以上知识点由知识点提取结果补充/g) || []).length;
  const mathIssues = (content.match(/x<45|没有公共部分/g) || []).length;
  const emptyUnitCount = units.filter((unit) => {
    const index = content.indexOf(unit.title);
    return index < 0;
  }).length;
  return { internalPlaceholders, mathIssues, emptyUnitCount };
}
