/** 一元一次表达式、不等式与区间的符号解析。 */
export interface Fraction { n: number; d: number }
export interface Linear { a: Fraction; b: Fraction }
export interface Bound { op: '<' | '<=' | '>' | '>='; value: Fraction }

function gcd(a: number, b: number): number { a = Math.abs(a); b = Math.abs(b); while (b) { [a, b] = [b, a % b]; } return a || 1; }
function frac(n: number, d = 1): Fraction { if (d < 0) { n = -n; d = -d; } const g = gcd(n, d); return { n: Math.round(n / g), d: Math.round(d / g) }; }
function add(a: Fraction, b: Fraction) { return frac(a.n * b.d + b.n * a.d, a.d * b.d); }
function sub(a: Fraction, b: Fraction) { return frac(a.n * b.d - b.n * a.d, a.d * b.d); }
function mul(a: Fraction, b: Fraction) { return frac(a.n * b.n, a.d * b.d); }
function div(a: Fraction, b: Fraction) { if (!b.n) throw new Error('division by zero'); return frac(a.n * b.d, a.d * b.n); }
function zero(a: Fraction) { return a.n === 0; }
function fmt(a: Fraction) { return a.d === 1 ? String(a.n) : '\\frac{' + a.n + '}{' + a.d + '}'; }
function value(f: Fraction): number { return f.n / f.d; }

export function normalize(value: string): string {
  return String(value || '')
    .replace(/\\dfrac/g, '\\frac')
    .replace(/\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}/g, '(($1)/($2))')
    .replace(/\\left|\\right/g, '')
    .replace(/\\[()]/g, '')
    .replace(/\\cdot|\\times/g, '*')
    .replace(/\\ge/g, '>=').replace(/\\le/g, '<=')
    .replace(/≤/g, '<=').replace(/≥/g, '>=')
    .replace(/＜/g, '<').replace(/＞/g, '>')
    .replace(/\s+/g, '');
}

/** 直接返回 a*x+b；任何非线性结构都抛错，不做取样猜测。 */
export function parseLinear(expression: string): Linear {
  const source = normalize(expression); let index = 0;
  const fail = (): never => { throw new Error('unsupported expression'); };
  const parseExpression = (): Linear => {
    let term = parseTerm();
    while (index < source.length && (source[index] === '+' || source[index] === '-')) {
      const op = source[index++]; const right = parseTerm();
      term = op === '+' ? { a: add(term.a, right.a), b: add(term.b, right.b) } : { a: sub(term.a, right.a), b: sub(term.b, right.b) };
    }
    return term;
  };
  const multiply = (left: Linear, right: Linear): Linear => {
    if (!zero(left.a) && !zero(right.a)) fail();
    if (zero(left.a)) return { a: mul(left.b, right.a), b: mul(left.b, right.b) };
    return { a: mul(left.a, right.b), b: mul(left.b, right.b) };
  };
  const divide = (left: Linear, right: Linear): Linear => {
    if (!zero(right.a) || zero(right.b)) fail();
    return { a: div(left.a, right.b), b: div(left.b, right.b) };
  };
  const parseTerm = (): Linear => {
    let term = parseFactor();
    while (index < source.length) {
      if (source[index] === '*' || source[index] === '/') {
        const op = source[index++]; const right = parseFactor();
        term = op === '*' ? multiply(term, right) : divide(term, right);
        continue;
      }
      if (source[index] === 'x' || source[index] === '(') {
        term = multiply(term, parseFactor());
        continue;
      }
      break;
    }
    return term;
  };
  const parseFactor = (): Linear => {
    if (source[index] === '+') { index++; return parseFactor(); }
    if (source[index] === '-') { index++; const item = parseFactor(); return { a: mul(frac(-1), item.a), b: mul(frac(-1), item.b) }; }
    if (source[index] === '(') { index++; const item = parseExpression(); if (source[index] !== ')') fail(); index++; return item; }
    if (source[index] === 'x') { index++; return { a: frac(1), b: frac(0) }; }
    if (source[index] === '^') fail();
    const start = index; while (index < source.length && /[0-9.]/.test(source[index])) index++;
    if (start === index) fail();
    const raw = source.slice(start, index);
    if (!/^[0-9]+(?:\.[0-9]+)?$/.test(raw)) fail();
    const [whole, decimal = ''] = raw.split('.');
    return { a: frac(0), b: frac(Number(whole + decimal), decimal ? 10 ** decimal.length : 1) };
  };
  const result = parseExpression();
  if (index !== source.length) fail();
  return result;
}

function compare(a: Fraction, b: Fraction): number { return value(a) - value(b); }

export function solveLinearInequality(expression: string): Bound | null {
  const normalized = normalize(expression);
  const match = normalized.match(/^(.*?)(<=|>=|<|>)(.*)$/);
  if (!match) return null;
  const left = parseLinear(match[1]); const right = parseLinear(match[3]);
  const a = sub(left.a, right.a); const b = sub(left.b, right.b);
  if (zero(a)) return null;
  const op = match[2] as Bound['op'];
  const result = div(mul(frac(-1), b), a);
  const flipped: Bound['op'] = a.n > 0 ? op : ({ '<': '>', '<=': '>=', '>': '<', '>=': '<=' } as const)[op];
  return { op: flipped, value: result };
}

export function parseBound(text: string): Bound | null {
  const cleaned = String(text || '').replace(/\\[()]/g, '').replace(/\\ge/g, '>=').replace(/\\le/g, '<=').replace(/\s+/g, '');
  const match = cleaned.match(/x(<=|>=|<|>)(\\frac\{([0-9]+)\}\{([0-9]+)\}|[0-9]+(?:\.[0-9]+)?)/);
  if (!match) return null;
  if (match[3] && match[4]) return { op: match[1] as Bound['op'], value: frac(Number(match[3]), Number(match[4])) };
  const raw = match[2]; const [whole, decimal = ''] = raw.split('.');
  return { op: match[1] as Bound['op'], value: frac(Number(whole + decimal), decimal ? 10 ** decimal.length : 1) };
}

function isLower(bound: Bound): boolean { return bound.op === '>' || bound.op === '>='; }
export function formatBound(bound: Bound): string { return 'x' + bound.op.replace('<=', '≤').replace('>=', '≥') + fmt(bound.value); }

export function intersectBounds(a: Bound, b: Bound): { empty: boolean; lower?: Bound; upper?: Bound } {
  const lowerBounds = [a, b].filter(isLower).sort((left, right) => compare(left.value, right.value));
  const upperBounds = [a, b].filter((item) => !isLower(item)).sort((left, right) => compare(left.value, right.value));
  const lower = lowerBounds[lowerBounds.length - 1];
  const upper = upperBounds[0];
  if (lower && upper) {
    const diff = compare(lower.value, upper.value);
    const empty = diff > 0 || (diff === 0 && (lower.op === '>' || upper.op === '<'));
    return { empty, lower, upper };
  }
  return { empty: false, lower, upper };
}

export function formatIntersection(result: { empty: boolean; lower?: Bound; upper?: Bound }): string {
  if (result.empty) return '无解';
  if (result.lower && result.upper) {
    return fmt(result.lower.value) + (result.lower.op === '>=' ? '≤' : '<') + 'x' + (result.upper.op === '<=' ? '≤' : '<') + fmt(result.upper.value);
  }
  return result.lower ? formatBound(result.lower) : result.upper ? formatBound(result.upper) : '全体实数';
}

export function correctLinearClaimsLine(line: string): string {
  const expressionMatch = line.match(/解不等式[^：:]*[：:]([^，。]+)[，,]/);
  if (!expressionMatch) return line;
  try {
    const bound = solveLinearInequality(expressionMatch[1]);
    if (!bound) return line;
    return line.replace(/得\s*\\?\(?x(?:<|>|≤|≥)[^）)。\n]*\)?/g, '得 \\(' + formatBound(bound) + '\\)');
  } catch {
    return line;
  }
}
