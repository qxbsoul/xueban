/** 一元一次方程/不等式与区间的程序化求解。只处理可解析的线性表达式。 */
export interface Fraction { n: number; d: number }
export interface Linear { a: Fraction; b: Fraction }
export interface Bound { op: '<' | '<=' | '>' | '>='; value: Fraction }

function gcd(a: number, b: number): number { a = Math.abs(a); b = Math.abs(b); while (b) { [a, b] = [b, a % b]; } return a || 1; }
function frac(n: number, d = 1): Fraction { if (d < 0) { n = -n; d = -d; } const g = gcd(n, d); return { n: Math.round(n / g), d: Math.round(d / g) }; }
function add(a: Fraction, b: Fraction) { return frac(a.n * b.d + b.n * a.d, a.d * b.d); }
function sub(a: Fraction, b: Fraction) { return frac(a.n * b.d - b.n * a.d, a.d * b.d); }
function mul(a: Fraction, b: Fraction) { return frac(a.n * b.n, a.d * b.d); }
function div(a: Fraction, b: Fraction) { if (!b.n) throw new Error('division by zero'); return frac(a.n * b.d, a.d * b.n); }
function eqZero(a: Fraction) { return a.n === 0; }
function fmt(a: Fraction) { return a.d === 1 ? String(a.n) : '\\frac{' + a.n + '}{' + a.d + '}'; }

function normalize(value: string): string {
  return String(value || '')
    .replace(/\\dfrac/g, '\\frac').replace(/\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}/g, '(($1)/($2))')
    .replace(/\\left|\\right/g, '').replace(/\\[()]/g, '').replace(/\\cdot|\\times/g, '*')
    .replace(/\\ge/g, '>=').replace(/\\le/g, '<=').replace(/[≤]/g, '<=').replace(/[≥]/g, '>=').replace(/[＜]/g, '<').replace(/[＞]/g, '>')
    .replace(/\s+/g, '');
}

function evalAt(expression: string, x: number): Fraction {
  const source = normalize(expression); let index = 0;
  const fail = () => { throw new Error('unsupported expression'); };
  const parseExpression = (): Fraction => {
    let value = parseTerm();
    while (index < source.length && (source[index] === '+' || source[index] === '-')) {
      const op = source[index++]; const right = parseTerm();
      value = op === '+' ? add(value, right) : sub(value, right);
    }
    return value;
  };
  const parseTerm = (): Fraction => {
    let value = parseFactor();
    while (index < source.length) {
      if (source[index] === '*' || source[index] === '/') {
        const op = source[index++]; const right = parseFactor();
        value = op === '*' ? mul(value, right) : div(value, right);
        continue;
      }
      if (source[index] === 'x' || source[index] === '(') {
        value = mul(value, parseFactor());
        continue;
      }
      break;
    }
    return value;
  };
  const parseFactor = (): Fraction => {
    if (source[index] === '+') { index++; return parseFactor(); }
    if (source[index] === '-') { index++; return mul(frac(-1), parseFactor()); }
    if (source[index] === '(') { index++; const value = parseExpression(); if (source[index] !== ')') fail(); index++; return value; }
    if (source[index] === 'x') { index++; return frac(x); }
    const start = index; while (index < source.length && /[0-9.]/.test(source[index])) index++;
    if (start === index) fail();
    const raw = source.slice(start, index);
    if (!/^[0-9]+(?:\.[0-9]+)?$/.test(raw)) fail();
    const [whole, decimal = ''] = raw.split('.');
    return frac(Number(whole + (decimal ? decimal : '')), decimal ? 10 ** decimal.length : 1);
  };
  const result = parseExpression();
  if (index !== source.length) fail();
  return result;
}

export function parseLinear(expression: string): Linear {
  const zero = evalAt(expression, 0);
  const one = evalAt(expression, 1);
  return { a: sub(one, zero), b: zero };
}

export function solveLinearInequality(expression: string): Bound | null {
  const normalized = normalize(expression);
  const match = normalized.match(/^(.*?)(<=|>=|<|>)(.*)$/);
  if (!match) return null;
  const left = parseLinear(match[1]);
  const right = parseLinear(match[3]);
  const a = sub(left.a, right.a); const b = sub(left.b, right.b);
  const op = match[2] as '<' | '<=' | '>' | '>=';
  if (eqZero(a)) return null;
  const value = div(mul(frac(-1), b), a);
  const flipped: Bound['op'] = a.n > 0
    ? op
    : ({ '<': '>', '<=': '>=', '>': '<', '>=': '<=' } as const)[op];
  return { op: flipped, value };
}

export function parseBound(text: string): Bound | null {
  const normalized = normalize(text);
  const match = normalized.match(/x(<=|>=|<|>)([0-9.]+|\\\\frac\{[0-9]+\}\{[0-9]+\})/);
  if (!match) return null;
  const valueText = match[2];
  let value: Fraction;
  const fractionMatch = valueText.match(/\\\\frac\{([0-9]+)\}\{([0-9]+)\}/);
  if (fractionMatch) value = frac(Number(fractionMatch[1]), Number(fractionMatch[2]));
  else value = frac(Number(valueText), valueText.includes('.') ? 10 ** valueText.split('.')[1].length : 1);
  return { op: match[1] as Bound['op'], value };
}

function lower(bound: Bound): boolean { return bound.op === '>' || bound.op === '>='; }
export function formatBound(bound: Bound): string { return 'x' + bound.op.replace('<=', '≤').replace('>=', '≥') + fmt(bound.value); }

export function intersectBounds(a: Bound, b: Bound): { empty: boolean; lower?: Bound; upper?: Bound } {
  const bounds = [a, b];
  const lowerBound = bounds.filter(lower).sort((left, right) => left.value.n / left.value.d - right.value.n / right.value.d).pop();
  const upperBound = bounds.filter((item) => !lower(item)).sort((left, right) => left.value.n / left.value.d - right.value.n / right.value.d)[0];
  if (!lowerBound || !upperBound) return { empty: false, lower: lowerBound, upper: upperBound };
  const l = lowerBound.value.n / lowerBound.value.d; const u = upperBound.value.n / upperBound.value.d;
  const empty = l > u || (l === u && (lowerBound.op === '>' || upperBound.op === '<'));
  return { empty, lower: lowerBound, upper: upperBound };
}

export function formatIntersection(result: { empty: boolean; lower?: Bound; upper?: Bound }): string {
  if (result.empty) return '无解';
  if (result.lower && result.upper) return fmt(result.lower.value) + (result.lower.op === '>=' ? '≤' : '<') + 'x' + (result.upper.op === '<=' ? '≤' : '<') + fmt(result.upper.value);
  return result.lower ? formatBound(result.lower) : result.upper ? formatBound(result.upper) : '全体实数';
}

export function correctLinearClaimsLine(line: string): string {
  const expressionMatch = line.match(/解不等式[^：:]*[：:]([^，。]+)[，,]/);
  if (!expressionMatch) return line;
  try {
    const bound = solveLinearInequality(expressionMatch[1]);
    if (!bound) return line;
    return line.replace(/得\s*\\?\(?x(?:<|>|≤|≥)[^）)。\n]*\)?/g, '得 \\($1\\)'.replace('$1', formatBound(bound)));
  } catch {
    return line;
  }
}
