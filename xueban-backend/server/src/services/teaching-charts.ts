export type TeachingChartType = 'bar' | 'pie' | 'line' | 'histogram' | 'scatter';

export interface TeachingChart {
  type: TeachingChartType;
  title: string;
  labels: string[];
  series: Array<{ name: string; values: number[] }>;
  xLabel: string;
  yLabel: string;
  unit: string;
  source: string;
  page: number | null;
  blockId: string;
}

interface TableRow { cells: string[] }

function text(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function decodeCell(value: string): string {
  return text(value
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&'));
}

function tableRows(markdown: string): TableRow[] {
  return [...markdown.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)].map((match) => ({
    cells: [...match[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((cell) => decodeCell(cell[1])),
  })).filter((row) => row.cells.length > 0);
}

function number(value: string): number | null {
  const match = value.match(/-?\d+(?:\.\d+)?/);
  if (!match) return null;
  const parsed = Number(match[0]);
  return Number.isFinite(parsed) ? parsed : null;
}

function sourceOf(block: { id: string; page: number | null }): Pick<TeachingChart, 'source' | 'page' | 'blockId'> {
  return { source: '教材原文', page: block.page, blockId: block.id };
}

function extractTableCategory(rows: TableRow[], block: { id: string; page: number | null }): TeachingChart[] {
  if (rows.length < 3) return [];
  const header = rows[0].cells;
  const body = rows.slice(1);
  const categoryIndex = 0;
  const valueIndex = header.findIndex((cell, index) => index > 0 && /人数|数量|频数|销售额|销售量|次数/.test(cell));
  if (valueIndex < 0) return [];
  const effectiveValue = valueIndex;
  if (body.length > 30) return [];
  if (body.every((row) => number(row.cells[0] || '') !== null)) return [];
  const entries = body.map((row) => ({
    label: text(row.cells[categoryIndex]),
    value: number(row.cells[effectiveValue] || ''),
  })).filter((entry) => entry.label && entry.value !== null && !/合计|总计/.test(entry.label)) as Array<{ label: string; value: number }>;
  if (entries.some((entry) => entry.label.length > 30)) return [];
  if (entries.length < 2 || new Set(entries.map((entry) => entry.label)).size !== entries.length) return [];
  const total = entries.reduce((sum, entry) => sum + entry.value, 0);
  if (!total) return [];
  const title = /体重|BMI/i.test(header.join(' ')) ? '员工体重指数分类' : `教材数据分类统计（第 ${block.page || 1} 页）`;
  return [
    { type: 'bar', title, labels: entries.map((entry) => entry.label), series: [{ name: '人数', values: entries.map((entry) => entry.value) }], xLabel: '分类', yLabel: '人数', unit: '人', ...sourceOf(block) },
    { type: 'pie', title: `${title}占比`, labels: entries.map((entry) => entry.label), series: [{ name: '占比', values: entries.map((entry) => entry.value) }], xLabel: '分类', yLabel: '占比', unit: '%', ...sourceOf(block) },
  ];
}

function extractHistogram(rows: TableRow[], block: { id: string; page: number | null }): TeachingChart[] {
  const bins: Array<{ lower: number; upper: number; frequency: number }> = [];
  rows.forEach((row) => {
    for (const offset of [0, 3]) {
      if (row.cells.length < offset + 3) continue;
      const numbers = (row.cells[offset].match(/\d+(?:\.\d+)?/g) || []).map(Number);
      const frequency = number(row.cells[offset + 2] || '');
      if (numbers.length >= 2 && frequency !== null && numbers[0] < numbers[1]) {
        bins.push({ lower: numbers[0], upper: numbers[1], frequency });
      }
    }
  });
  if (bins.length < 3 || !bins.some((bin) => bin.frequency > 0)) return [];
  const unique = [...new Map(bins.map((bin) => [`${bin.lower}-${bin.upper}`, bin])).values()]
    .sort((left, right) => left.lower - right.lower);
  if (unique.length < 3 || unique.some((bin, index) => index > 0 && bin.lower < unique[index - 1].upper)) return [];
  return [{
    type: 'histogram', title: '教材频数分布', labels: unique.map((bin) => `${bin.lower}≤x<${bin.upper}`),
    series: [{ name: '频数', values: unique.map((bin) => bin.frequency) }], xLabel: '分组', yLabel: '频数', unit: '个',
    ...sourceOf(block),
  }];
}

function extractScatter(rows: TableRow[], block: { id: string; page: number | null }): TeachingChart[] {
  if (rows.length < 2 || rows[0].cells.length < 3 || rows[1].cells.length < 3) return [];
  const xCells = rows[0].cells.slice(1);
  const yCells = rows[1].cells.slice(1);
  if (!/气温|温度|x|年份|时间/i.test(rows[0].cells[0]) || !/销售|销量|冷饮|杯数|y|数量/i.test(rows[1].cells[0])) return [];
  const points = xCells.map((cell, index) => ({ x: number(cell), y: number(yCells[index] || '') })).filter((point) => point.x !== null && point.y !== null) as Array<{ x: number; y: number }>;
  if (points.length < 3) return [];
  return [{
    type: 'scatter', title: '教材数据关系', labels: points.map((point) => String(point.x)),
    series: [{ name: rows[1].cells[0], values: points.map((point) => point.y) }],
    xLabel: rows[0].cells[0], yLabel: rows[1].cells[0], unit: '',
    ...sourceOf(block),
  }];
}

function extractNarrativeCategory(blocks: Array<{ id: string; text: string; page: number | null }>): TeachingChart[] {
  for (const block of blocks) {
    if (!/员工|体重指数|BMI/.test(block.text) || !/百分比/.test(block.text)) continue;
    const matches = [...block.text.matchAll(/(体重[^；。]{0,14}|超重|肥胖)[^；。]{0,32}?有\s*(\d+)\s*人[^；。]{0,32}?百分比为\s*([\d.]+)%/g)];
    if (matches.length < 2) continue;
    const entries = matches.map((match) => {
      const label = match[1].replace(/的人.*$/, '').replace(/人数.*$/, '').replace(/[，,].*$/, '').trim();
      return { label, value: Number(match[2]), percent: Number(match[3]) };
    }).filter((entry) => entry.label);
    const total = entries.reduce((sum, entry) => sum + entry.value, 0);
    const percentage = entries.reduce((sum, entry) => sum + entry.percent, 0);
    if (total !== 60 || Math.abs(percentage - 100) > 1.5) continue;
    const chartTitle = '员工体重指数分类';
    return [
      { type: 'bar', title: chartTitle, labels: entries.map((entry) => entry.label), series: [{ name: '人数', values: entries.map((entry) => entry.value) }], xLabel: '分类', yLabel: '人数', unit: '人', ...sourceOf(block) },
      { type: 'pie', title: `${chartTitle}占比`, labels: entries.map((entry) => entry.label), series: [{ name: '占比', values: entries.map((entry) => entry.value) }], xLabel: '分类', yLabel: '占比', unit: '%', ...sourceOf(block) },
    ];
  }
  return [];
}

export function extractTeachingCharts(blocks: Array<{ id: string; block_type: string; markdown?: string; text: string; page: number | null }>): TeachingChart[] {
  const charts: TeachingChart[] = [];
  blocks.forEach((block) => {
    if (block.block_type !== 'table') return;
    const rows = tableRows(block.markdown || block.text);
    if (!rows.length) return;
    charts.push(...extractHistogram(rows, block));
    if (!charts.some((chart) => chart.blockId === block.id && chart.type === 'histogram')) {
      charts.push(...extractScatter(rows, block));
      charts.push(...extractTableCategory(rows, block));
    }
  });
  charts.push(...extractNarrativeCategory(blocks));
  const seen = new Set<string>();
  return charts.filter((chart) => {
    const key = `${chart.type}:${chart.title}:${chart.labels.join('|')}:${chart.blockId}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function chartMarkdown(charts: TeachingChart[]): string {
  if (!charts.length) return '';
  return [
    '## 教材统计图',
    '',
    ...charts.flatMap((chart) => ['```chart', JSON.stringify(chart, null, 2), '```', '']),
  ].join('\n');
}
