import type { Components } from 'react-markdown';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import './chat-rich-content.css';

export interface ChatRichContentProps {
  content: string;
  className?: string;
  headingIdPrefix?: string;
  headingIds?: string[];
}

interface ChartSeries {
  name?: string;
  values: number[];
}

type ChartType = 'bar' | 'pie' | 'line' | 'histogram' | 'scatter';

interface ChartData {
  type?: ChartType;
  title?: string;
  series: ChartSeries[];
  labels?: string[];
  xLabel?: string;
  yLabel?: string;
  unit?: string;
  source?: string;
  page?: number | null;
  blockId?: string;
}

const CHART_LANGUAGES = new Set(['chart', 'vega-lite']);
const CHART_COLORS = ['#16283f', '#a44b32', '#397568', '#7a6035', '#65527f', '#3e6e91'];

function parseChart(source: string): { data?: ChartData; error?: string } {
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch (error) {
    return { error: `图表 JSON 无法解析：${error instanceof Error ? error.message : '格式不正确'}` };
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { error: '图表数据必须是包含 series 的 JSON 对象。' };
  }

  const candidate = value as Record<string, unknown>;
  if (!Array.isArray(candidate.series) || candidate.series.length === 0) {
    return { error: '图表数据缺少非空的 series 数组。' };
  }
  if (candidate.labels !== undefined && (!Array.isArray(candidate.labels) || candidate.labels.some(label => typeof label !== 'string'))) {
    return { error: 'labels 必须是字符串数组。' };
  }
  if (candidate.title !== undefined && typeof candidate.title !== 'string') {
    return { error: 'title 必须是字符串。' };
  }
  const chartTypes = new Set(['bar', 'pie', 'line', 'histogram', 'scatter']);
  const type = typeof candidate.type === 'string' && chartTypes.has(candidate.type)
    ? candidate.type as ChartType : 'line';

  const series: ChartSeries[] = [];
  for (let index = 0; index < candidate.series.length; index += 1) {
    const item: unknown = candidate.series[index];
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      return { error: `series[${index}] 必须是对象。` };
    }
    const entry = item as Record<string, unknown>;
    if (!Array.isArray(entry.values) || entry.values.length === 0 || entry.values.some(point => typeof point !== 'number' || !Number.isFinite(point))) {
      return { error: `series[${index}].values 必须是非空的有限数字数组。` };
    }
    if (entry.name !== undefined && typeof entry.name !== 'string') {
      return { error: `series[${index}].name 必须是字符串。` };
    }
    series.push({ name: entry.name as string | undefined, values: entry.values as number[] });
  }

  return { data: {
    type,
    title: candidate.title as string | undefined,
    labels: candidate.labels as string[] | undefined,
    series,
    xLabel: typeof candidate.xLabel === 'string' ? candidate.xLabel : undefined,
    yLabel: typeof candidate.yLabel === 'string' ? candidate.yLabel : undefined,
    unit: typeof candidate.unit === 'string' ? candidate.unit : undefined,
    source: typeof candidate.source === 'string' ? candidate.source : undefined,
    page: typeof candidate.page === 'number' ? candidate.page : undefined,
    blockId: typeof candidate.blockId === 'string' ? candidate.blockId : undefined,
  } };
}

function formatTick(value: number): string {
  if (value === 0) return '0';
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000 || absolute < 0.01) return value.toExponential(1);
  return Number(value.toFixed(2)).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function ChartSource({ data }: { data: ChartData }) {
  if (!data.source && !data.page) return null;
  return <div className="sf-rich-chart-source">{data.source || '教材数据'}{data.page ? ` · 第 ${data.page} 页` : ''}</div>;
}

function BarChart({ data }: { data: ChartData }) {
  const values = data.series[0]?.values || [];
  const labels = data.labels || values.map((_, index) => String(index + 1));
  if (!values.length) return <div className="sf-rich-chart-error">图表缺少数据</div>;
  const width = 760, height = 350;
  const margin = { top: data.title ? 58 : 28, right: 28, bottom: 78, left: 76 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(...values, 1) * 1.12;
  const yFor = (value: number) => margin.top + plotHeight - (value / maximum) * plotHeight;
  const step = plotWidth / Math.max(values.length, 1);
  const barWidth = Math.min(58, step * 0.62);
  return (
    <figure className="sf-rich-chart">
      {data.title && <figcaption className="sf-rich-chart-title">{data.title}</figcaption>}
      <svg className="sf-rich-chart-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={data.title || '条形图'}>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => (
          <g key={ratio}>
            <line className="sf-rich-chart-grid" x1={margin.left} x2={width - margin.right} y1={yFor(maximum * ratio)} y2={yFor(maximum * ratio)} />
            <text className="sf-rich-chart-axis-label" x={margin.left - 10} y={yFor(maximum * ratio) + 4} textAnchor="end">{formatTick(maximum * ratio)}</text>
          </g>
        ))}
        {values.map((value, index) => {
          const x = margin.left + step * index + (step - barWidth) / 2;
          const y = yFor(value);
          return <g key={index}>
            <rect className="sf-rich-chart-bar" x={x} y={y} width={barWidth} height={Math.max(1, yFor(0) - y)} />
            <text className="sf-rich-chart-value" x={x + barWidth / 2} y={y - 6} textAnchor="middle">{formatTick(value)}</text>
            <text className="sf-rich-chart-axis-label" x={x + barWidth / 2} y={height - 44} textAnchor="middle" transform={`rotate(-18 ${x + barWidth / 2} ${height - 44})`}>{labels[index] || ''}</text>
          </g>;
        })}
        <line className="sf-rich-chart-axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
        <line className="sf-rich-chart-axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
        {data.yLabel && <text className="sf-rich-chart-axis" x={margin.left - 42} y={margin.top - 14}>{data.yLabel}{data.unit ? `（${data.unit}）` : ''}</text>}
      </svg>
      <ChartSource data={data} />
    </figure>
  );
}

function PieChart({ data }: { data: ChartData }) {
  const values = data.series[0]?.values || [];
  const labels = data.labels || values.map((_, index) => String(index + 1));
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (!total) return <div className="sf-rich-chart-error">图表缺少数据</div>;
  const width = 760, height = 350, radius = 118, centerX = 250, centerY = 178;
  let angle = -Math.PI / 2;
  const slices = values.map((value, index) => {
    const next = angle + (Math.max(0, value) / total) * Math.PI * 2;
    const x1 = centerX + radius * Math.cos(angle), y1 = centerY + radius * Math.sin(angle);
    const x2 = centerX + radius * Math.cos(next), y2 = centerY + radius * Math.sin(next);
    const large = next - angle > Math.PI ? 1 : 0;
    const path = `M ${centerX} ${centerY} L ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`;
    angle = next;
    return { path, color: CHART_COLORS[index % CHART_COLORS.length], label: labels[index], value };
  });
  return (
    <figure className="sf-rich-chart">
      {data.title && <figcaption className="sf-rich-chart-title">{data.title}</figcaption>}
      <svg className="sf-rich-chart-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={data.title || '扇形图'}>
        {slices.map((slice, index) => <path key={index} d={slice.path} fill={slice.color} stroke="#fff" strokeWidth="2" />)}
        {slices.map((slice, index) => {
          const angleMid = (-Math.PI / 2) + values.slice(0, index + 1).reduce((sum, value) => sum + Math.max(0, value) / total * Math.PI * 2, 0) - (Math.max(0, values[index]) / total * Math.PI);
          const labelX = centerX + (radius + 28) * Math.cos(angleMid);
          const labelY = centerY + (radius + 28) * Math.sin(angleMid);
          return <text key={`label-${index}`} className="sf-rich-chart-axis-label" x={labelX} y={labelY} textAnchor={labelX < centerX ? 'end' : 'start'}>{labels[index]} {Math.round(slice.value / total * 1000) / 10}%</text>;
        })}
      </svg>
      <div className="sf-rich-chart-legend">
        {slices.map((slice, index) => <span className="sf-rich-chart-legend-item" key={index}><i className="sf-rich-chart-swatch" style={{ backgroundColor: slice.color }} />{labels[index]}</span>)}
      </div>
      <ChartSource data={data} />
    </figure>
  );
}

function ScatterChart({ data }: { data: ChartData }) {
  const yValues = data.series[0]?.values || [];
  const xValues = (data.labels || []).map((label) => Number(label)).filter((value) => Number.isFinite(value));
  if (xValues.length !== yValues.length || xValues.length < 2) return <div className="sf-rich-chart-error">散点图数据不完整</div>;
  const width = 760, height = 350;
  const margin = { top: data.title ? 58 : 28, right: 28, bottom: 62, left: 76 };
  const plotWidth = width - margin.left - margin.right, plotHeight = height - margin.top - margin.bottom;
  const minX = Math.min(...xValues), maxX = Math.max(...xValues);
  const minY = Math.min(...yValues), maxY = Math.max(...yValues);
  const sx = (value: number) => margin.left + (value - minX) / Math.max(maxX - minX, 1) * plotWidth;
  const sy = (value: number) => margin.top + (maxY - value) / Math.max(maxY - minY, 1) * plotHeight;
  const meanX = xValues.reduce((sum, value) => sum + value, 0) / xValues.length;
  const meanY = yValues.reduce((sum, value) => sum + value, 0) / yValues.length;
  const slope = xValues.reduce((sum, value, index) => sum + (value - meanX) * (yValues[index] - meanY), 0)
    / Math.max(xValues.reduce((sum, value) => sum + (value - meanX) ** 2, 0), 1e-9);
  const intercept = meanY - slope * meanX;
  return (
    <figure className="sf-rich-chart">
      {data.title && <figcaption className="sf-rich-chart-title">{data.title}</figcaption>}
      <svg className="sf-rich-chart-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={data.title || '散点图'}>
        <line className="sf-rich-chart-axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
        <line className="sf-rich-chart-axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
        <line className="sf-rich-chart-trend" x1={sx(minX)} y1={sy(slope * minX + intercept)} x2={sx(maxX)} y2={sy(slope * maxX + intercept)} />
        {xValues.map((x, index) => <circle key={index} className="sf-rich-chart-point" cx={sx(x)} cy={sy(yValues[index])} r="5"><title>{`${data.xLabel || 'x'} ${x} / ${data.yLabel || 'y'} ${yValues[index]}`}</title></circle>)}
        <text className="sf-rich-chart-axis" x={width - margin.right} y={height - 24} textAnchor="end">{data.xLabel || 'x'}</text>
        <text className="sf-rich-chart-axis" x={margin.left - 42} y={margin.top - 14}>{data.yLabel || 'y'}{data.unit ? `（${data.unit}）` : ''}</text>
      </svg>
      <ChartSource data={data} />
    </figure>
  );
}

function ChartRenderer({ source }: { source: string }) {
  const { data, error } = parseChart(source);
  if (error || !data) return <div className="sf-rich-chart-error" role="alert"><strong>图表暂时无法绘制</strong><span>{error}</span></div>;
  if (data.type === 'bar' || data.type === 'histogram') return <BarChart data={data} />;
  if (data.type === 'pie') return <PieChart data={data} />;
  if (data.type === 'scatter') return <ScatterChart data={data} />;
  return <LineChart source={source} />;
}

function LineChart({ source }: { source: string }) {
  const { data, error } = parseChart(source);
  if (error || !data) {
    return <div className="sf-rich-chart-error" role="alert"><strong>图表暂时无法绘制</strong><span>{error}</span></div>;
  }

  const width = 760;
  const height = 350;
  const margin = { top: data.title ? 58 : 28, right: 28, bottom: 62, left: 76 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = data.series.flatMap(item => item.values);
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (minimum === maximum) {
    const padding = Math.abs(minimum) * 0.1 || 1;
    minimum -= padding;
    maximum += padding;
  } else {
    const padding = (maximum - minimum) * 0.08;
    minimum -= padding;
    maximum += padding;
  }
  const maxPoints = Math.max(...data.series.map(item => item.values.length));
  const xFor = (index: number) => margin.left + (maxPoints === 1 ? plotWidth / 2 : index * plotWidth / (maxPoints - 1));
  const yFor = (value: number) => margin.top + (maximum - value) * plotHeight / (maximum - minimum);
  const ticks = Array.from({ length: 6 }, (_, index) => minimum + (maximum - minimum) * index / 5).reverse();

  return (
    <figure className="sf-rich-chart">
      {data.title && <figcaption className="sf-rich-chart-title">{data.title}</figcaption>}
      <svg className="sf-rich-chart-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={data.title || '折线图'}>
        {ticks.map(tick => (
          <g className="sf-rich-chart-tick" key={tick}>
            <line className="sf-rich-chart-grid" x1={margin.left} x2={width - margin.right} y1={yFor(tick)} y2={yFor(tick)} />
            <text className="sf-rich-chart-axis-label" x={margin.left - 12} y={yFor(tick) + 4} textAnchor="end">{formatTick(tick)}</text>
          </g>
        ))}
        <line className="sf-rich-chart-axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
        <line className="sf-rich-chart-axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
        {Array.from({ length: maxPoints }, (_, index) => {
          const showLabel = maxPoints <= 12 || index % Math.ceil(maxPoints / 12) === 0 || index === maxPoints - 1;
          return showLabel ? <text className="sf-rich-chart-axis-label" key={index} x={xFor(index)} y={height - margin.bottom + 25} textAnchor="middle">{data.labels?.[index] ?? String(index + 1)}</text> : null;
        })}
        {data.series.map((item, seriesIndex) => {
          const color = CHART_COLORS[seriesIndex % CHART_COLORS.length];
          const points = item.values.map((value, index) => `${xFor(index)},${yFor(value)}`).join(' ');
          return (
            <g className="sf-rich-chart-series" key={seriesIndex}>
              <polyline className="sf-rich-chart-line" style={{ stroke: color }} points={points} />
              {item.values.map((value, index) => <circle className="sf-rich-chart-point" style={{ fill: color }} key={index} cx={xFor(index)} cy={yFor(value)} r="3.5"><title>{`${item.name || `序列 ${seriesIndex + 1}`} · ${data.labels?.[index] ?? index + 1}: ${value}`}</title></circle>)}
            </g>
          );
        })}
      </svg>
      <div className="sf-rich-chart-legend" aria-label="图例">
        {data.series.map((item, index) => <span className="sf-rich-chart-legend-item" key={index}><i className="sf-rich-chart-swatch" style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />{item.name || `序列 ${index + 1}`}</span>)}
      </div>
    </figure>
  );
}

function AnswerDisclosure({ source, label }: { source: string; label: string }) {
  return (
    <details className="sf-rich-answer">
      <summary>{label}</summary>
      <div className="sf-rich-answer-body">
        <ChatRichContent content={source} />
      </div>
    </details>
  );
}

const components: Components = {
  a: ({ href, children }) => {
    const external = Boolean(href && /^(https?:)?\/\//i.test(href));
    return <a className="sf-rich-link" href={href} target={external ? '_blank' : undefined} rel={external ? 'noopener noreferrer nofollow' : undefined}>{children}</a>;
  },
  img: ({ src, alt }) => {
    const value = typeof src === 'string' ? src : '';
    if (/^(pdf|docx|image):\/\//i.test(value)) {
      return <span className="sf-rich-source-image" role="img" aria-label={alt || '教材图片'}>{alt || '教材图片'}</span>;
    }
    return <img className="sf-rich-image" src={value} alt={alt || ''} loading="lazy" />;
  },
  table: ({ children }) => <div className="sf-rich-table-scroll" role="region" aria-label="表格，可横向滚动" tabIndex={0}><table className="sf-rich-table">{children}</table></div>,
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children }) => {
    const source = String(children).replace(/\n$/, '');
    const language = /language-([\w-]+)/.exec(className || '')?.[1]?.toLowerCase();
    if (language === 'answer') return <AnswerDisclosure source={source} label="查看解题过程" />;
    if (language === 'check-answer') return <AnswerDisclosure source={source} label="查看参考答案" />;
    if (language && CHART_LANGUAGES.has(language)) return <ChartRenderer source={source} />;
    if (language || source.includes('\n')) {
      return <div className="sf-rich-code-block">{language && <div className="sf-rich-code-language">{language}</div>}<pre className="sf-rich-pre"><code className="sf-rich-code">{source}</code></pre></div>;
    }
    return <code className="sf-rich-inline-code">{source}</code>;
  },
};

function normalizeMathDelimiters(content: string): string {
  // 兼容模型常见的 LaTeX 行内/独立公式写法；remark-math 只识别 $...$ / $$...$$。
  return content
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `$$\n${body.trim()}\n$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${body.trim()}$`)
    // remark-math 对“$$|a|=...$$”这种同一行的 display math 容易把后面的
    // Markdown 一起吞进公式；统一把块公式拆成独立三行。
    .replace(/\$\$([\s\S]*?)\$\$/g, (_match, body: string) => `\n$$\n${String(body).trim()}\n$$\n`);
}

export default function ChatRichContent({ content, className = '', headingIdPrefix, headingIds }: ChatRichContentProps) {
  let headingIndex = 0;
  const headingComponents: Components = headingIdPrefix
    ? {
        ...components,
        h3: ({ children }) => {
          const id = headingIds?.[headingIndex] || `${headingIdPrefix}-${headingIndex}`;
          headingIndex += 1;
          return <h3 id={id}>{children}</h3>;
        },
      }
    : components;
  return (
    <div className={`sf-rich-content${className ? ` ${className}` : ''}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={headingComponents}>
        {normalizeMathDelimiters(content)}
      </ReactMarkdown>
    </div>
  );
}
