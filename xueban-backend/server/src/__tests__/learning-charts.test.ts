import { describe, expect, it } from 'vitest';
import { extractTeachingCharts } from '../services/teaching-charts';

const table = (id: string, page: number, html: string) => ({ id, block_type: 'table', markdown: html, text: html, page });

describe('教材原生图表数据', () => {
  it('从 B03 身高频数表生成真实直方图数据', () => {
    const html = `<table><tr><td>身高分组</td><td>划记</td><td>频数</td><td>身高分组</td><td>划记</td><td>频数</td></tr>
<tr><td>149≤x<152</td><td></td><td>2</td><td>164≤x<167</td><td>正</td><td>8</td></tr>
<tr><td>152≤x<155</td><td>正</td><td>6</td><td>167≤x<170</td><td></td><td>4</td></tr>
<tr><td>155≤x<158</td><td>正正</td><td>12</td><td>170≤x<173</td><td></td><td>2</td></tr>
<tr><td>158≤x<161</td><td>正正正</td><td>19</td><td>合计</td><td></td><td>63</td></tr>
<tr><td>161≤x<164</td><td>正正</td><td>10</td><td></td><td></td><td></td></tr></table>`;
    const charts = extractTeachingCharts([table('height', 17, html)]);
    const histogram = charts.find((chart) => chart.type === 'histogram');
    expect(histogram?.labels).toEqual(['149≤x<152', '152≤x<155', '155≤x<158', '158≤x<161', '161≤x<164', '164≤x<167', '167≤x<170', '170≤x<173']);
    expect(histogram?.series[0].values).toEqual([2, 6, 12, 19, 10, 8, 4, 2]);
    expect(histogram?.series[0].values.reduce((sum, value) => sum + value, 0)).toBe(63);
  });

  it('从 B03 气温/冷饮表生成成对散点数据', () => {
    const html = `<table><tr><td>最高气温/℃</td><td>12</td><td>13</td><td>17</td><td>19</td><td>20</td><td>22</td><td>24</td><td>25</td><td>28</td></tr>
<tr><td>冷饮杯数</td><td>50</td><td>69</td><td>74</td><td>90</td><td>108</td><td>97</td><td>119</td><td>125</td><td>154</td></tr></table>`;
    const charts = extractTeachingCharts([table('scatter', 22, html)]);
    const scatter = charts.find((chart) => chart.type === 'scatter');
    expect(scatter?.labels).toHaveLength(9);
    expect(scatter?.series[0].values).toEqual([50, 69, 74, 90, 108, 97, 119, 125, 154]);
  });

  it('从 B03 员工体重叙述生成条形图和扇形图，并校验总数 60', () => {
    const text = '这个公司 60 名员工中体重正常的人数最多，有 38 人，所占百分比为 63.3%；体重过低的人数次之，有 10 人，所占百分比为 16.7%；超重的有 7 人，所占百分比为 11.7%；肥胖的人数最少，有 5 人，所占百分比为 8.3%。';
    const charts = extractTeachingCharts([{ id: 'bmi', block_type: 'paragraph', text, page: 13 }]);
    expect(charts.filter((chart) => chart.type === 'bar')).toHaveLength(1);
    expect(charts.filter((chart) => chart.type === 'pie')).toHaveLength(1);
    expect(charts[0].series[0].values).toEqual([38, 10, 7, 5]);
  });

  it('连续数值/错误分组不生成图表', () => {
    const html = '<table><tr><td>分组</td><td>频数</td></tr><tr><td>4.0</td><td>1</td></tr><tr><td>4.3</td><td>1</td></tr><tr><td>4.6</td><td>2</td></tr></table>';
    expect(extractTeachingCharts([table('bad', 20, html)])).toHaveLength(0);
  });
});