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
}

interface ChartSeries {
  name?: string;
  values: number[];
}

interface ChartData {
  title?: string;
  series: ChartSeries[];
  labels?: string[];
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

  return { data: { title: candidate.title as string | undefined, labels: candidate.labels as string[] | undefined, series } };
}

function formatTick(value: number): string {
  if (value === 0) return '0';
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000 || absolute < 0.01) return value.toExponential(1);
  return Number(value.toFixed(2)).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
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

const components: Components = {
  a: ({ href, children }) => {
    const external = Boolean(href && /^(https?:)?\/\//i.test(href));
    return <a className="sf-rich-link" href={href} target={external ? '_blank' : undefined} rel={external ? 'noopener noreferrer nofollow' : undefined}>{children}</a>;
  },
  table: ({ children }) => <div className="sf-rich-table-scroll" role="region" aria-label="表格，可横向滚动" tabIndex={0}><table className="sf-rich-table">{children}</table></div>,
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children }) => {
    const source = String(children).replace(/\n$/, '');
    const language = /language-([\w-]+)/.exec(className || '')?.[1]?.toLowerCase();
    if (language && CHART_LANGUAGES.has(language)) return <LineChart source={source} />;
    if (language || source.includes('\n')) {
      return <div className="sf-rich-code-block">{language && <div className="sf-rich-code-language">{language}</div>}<pre className="sf-rich-pre"><code className="sf-rich-code">{source}</code></pre></div>;
    }
    return <code className="sf-rich-inline-code">{source}</code>;
  },
};

export default function ChatRichContent({ content, className = '' }: ChatRichContentProps) {
  return (
    <div className={`sf-rich-content${className ? ` ${className}` : ''}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
