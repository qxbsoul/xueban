import { useState } from 'react';
import { useWorkbench } from '../store';

const number = new Intl.NumberFormat('zh-CN');
const money = (value: number) => `¥${value.toFixed(value > 0 && value < 0.01 ? 6 : 4)}`;

type Metric = 'tokens' | 'cost' | 'calls';

/** 学情看板：模型调用用量与刊例成本。 */
export default function DashboardView() {
  const { dashboard, dashboardLoading, refreshDashboard, toast, billing, refreshBilling } = useWorkbench();
  const [metric, setMetric] = useState<Metric>('tokens');
  const values = dashboard.series.map(point => metric === 'tokens' ? point.tokens : metric === 'cost' ? point.costCny : point.calls);
  const max = Math.max(1, ...values);
  const hasUsage = dashboard.breakdown.some(item => item.calls > 0);

  const refresh = () => {
    void Promise.all([refreshDashboard(), refreshBilling()])
      .then(() => toast('已读取最新模型用量'))
      .catch(() => toast('刷新失败，请稍后重试'));
  };

  return (
    <div className="view-inner usage-dashboard dashboard-page">
      <div className="view-head">
        <div>
          <h2>学情看板</h2>
          <p className="view-description">学情诊断、学习规划与教研协作产生的模型调用都在这里统计；成本按刊例价估算，不代表实际账单扣费。</p>
        </div>
        <div className="actions">
          <button className="btn btn-ghost btn-sm" disabled={dashboardLoading} onClick={refresh}>
            {dashboardLoading ? '正在刷新…' : '刷新用量'}
          </button>
        </div>
      </div>

      <div className="settings-shell dashboard-shell">
        <div className="settings-content dashboard-content">
          {billing && (
            <section className="panel panel-pad credit-panel">
              <div className="credit-head">
                <div>
                  <div className="chart-title">积分消耗</div>
                  <div className="chart-sub">{billing.plan_label} · 10,000 Token = 1 积分 · 本周期 {new Date(billing.period_start).toLocaleDateString('zh-CN')} 起</div>
                </div>
                <div className="credit-figure">
                  <b>{billing.credits_used}</b><span>/ {billing.credits_total} 积分</span>
                </div>
              </div>
              <div className="credit-bar" role="progressbar" aria-valuenow={billing.percent_used} aria-valuemin={0} aria-valuemax={100} aria-label="积分已使用百分比">
                <i style={{ width: `${billing.percent_used}%` }} />
              </div>
              <div className="credit-meta">
                <span>已用 {billing.percent_used}%</span>
                <span>剩余 {billing.credits_remaining} 积分</span>
                <span>本周期已消耗 {number.format(billing.tokens_used)} Token</span>
              </div>
            </section>
          )}

          <div className="kpi-grid usage-kpis">
            {dashboard.kpis.map(kpi => <div className="kpi-card" key={kpi.label}>
              <div className="kpi-label">{kpi.label}</div><div className="kpi-value">{kpi.value}</div><div className={'kpi-trend ' + kpi.dir}>{kpi.trend}</div>
            </div>)}
          </div>

          <section className="panel panel-pad usage-chart-panel">
            <div className="usage-panel-head">
              <div><div className="chart-title">近 7 天用量趋势</div><div className="chart-sub">没有诊断或协作调用的日期按 0 显示</div></div>
              <div className="usage-metric-tabs" aria-label="趋势指标">
                {([['tokens', 'Token'], ['cost', '估算成本'], ['calls', '调用次数']] as const).map(([key, label]) => <button key={key} className={metric === key ? 'active' : ''} onClick={() => setMetric(key)}>{label}</button>)}
              </div>
            </div>
            <svg viewBox="0 0 700 250" width="100%" height="250" role="img" aria-label="近 7 天真实模型用量趋势">
              <line x1="45" y1="205" x2="680" y2="205" stroke="var(--line)" />
              {dashboard.series.map((point, index) => {
                const value = values[index] ?? 0;
                const height = value > 0 ? Math.max(2, (value / max) * 160) : 0;
                const x = 72 + index * 88;
                const y = 205 - height;
                return <g key={point.day} fontSize="11" fill="var(--muted)">
                  <rect x={x} y={y} width="44" height={height} rx="3" fill={index === dashboard.series.length - 1 ? 'var(--accent-orange)' : 'var(--navy-soft)'} opacity={value ? 0.9 : 0} />
                  <text x={x + 22} y="226" textAnchor="middle">{point.label}</text>
                  <text x={x + 22} y={Math.max(18, y - 9)} textAnchor="middle" fill="var(--ink)">{metric === 'cost' ? money(value) : number.format(value)}</text>
                </g>;
              })}
            </svg>
          </section>

          <section className="panel panel-pad">
            <div className="chart-title">按模型统计 · 近 30 天</div>
            {hasUsage ? <div className="usage-table-wrap"><table className="usage-table"><thead><tr><th>模型</th><th>调用</th><th>输入 Token</th><th>缓存命中</th><th>输出 Token</th><th>总 Token</th><th>刊例成本</th></tr></thead>
              <tbody>{dashboard.breakdown.map(item => <tr key={item.key}><td>{item.key}</td><td>{number.format(item.calls)}</td><td>{number.format(item.inputTokens)}</td><td>{number.format(item.cachedInputTokens)}</td><td>{number.format(item.outputTokens)}</td><td>{number.format(item.tokens)}</td><td>{money(item.costCny)}</td></tr>)}</tbody></table></div>
              : <div className="empty-tip">还没有真实模型用量。完成一次学情诊断或学习规划对话后，数据会出现在这里。</div>}
          </section>

          {dashboard.pricing && <section className="usage-pricing-note">
            <strong>{dashboard.pricing.source}</strong><span>未缓存输入 ¥{dashboard.pricing.inputPerMillion}／百万 Token</span><span>缓存命中输入 ¥{dashboard.pricing.cachedInputPerMillion}／百万 Token</span><span>输出 ¥{dashboard.pricing.outputPerMillion}／百万 Token</span>
          </section>}
        </div>
      </div>
    </div>
  );
}
