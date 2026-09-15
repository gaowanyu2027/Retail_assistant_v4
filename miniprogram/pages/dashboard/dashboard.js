// 看板页 — 热度排行 + 告警汇总 + 转化率四象限 + 定期汇报
const api = require('../../utils/api');

const QUADRANT_CN = {
  'cold': '冷区',
  'healthy': '健康区',
  'high_heat_high_sales': '健康区',
  'high_heat_low_sales': '高热度低转化',
  'low_heat_high_sales': '低曝光高转化',
  'high_heat_no_sales': '高热度无销量',
  'low_heat_no_sales': '低曝光无销量',
  'no_data': '暂无数据'
};
const QUADRANT_COLOR = {
  'cold': '#9aa5b5',
  'healthy': '#22c55e',
  'high_heat_high_sales': '#22c55e',
  'high_heat_low_sales': '#f59e0b',
  'low_heat_high_sales': '#3b82f6',
  'high_heat_no_sales': '#f59e0b',
  'low_heat_no_sales': '#9aa5b5'
};

Page({
  data: {
    loading: true,
    error: '',
    ranking: [],
    totalVisitors: 0,
    topZone: '',
    anomaly: { total_alerts: 0, high_risk_count: 0, watch_count: 0 },
    zones: [],
    summary: '',
    reports: []
  },

  onLoad() {
    this.loadAll();
  },

  onPullDownRefresh() {
    this.loadAll().finally(() => wx.stopPullDownRefresh());
  },

  loadAll() {
    this.setData({ loading: true, error: '' });
    return Promise.all([
      api.getDashboard(),
      api.getHotVsSales(1),
      api.getHeatReports(5)
    ])
      .then((results) => {
        const dash = results[0] || {};
        const hv = results[1] || {};
        const hr = results[2] || {};
        const pop = dash.popularity || {};
        const zones = (hv.zones || []).map((z) => ({
          zone_id: z.zone_id || '',
          zone_label: z.zone_label || z.zone_id || '',
          visit_count: z.visit_count || 0,
          sold_count: z.sold_count || 0,
          conversion_rate: z.conversion_rate || 0,
          quadrant: z.quadrant || '',
          quadrantCN: QUADRANT_CN[z.quadrant] || z.quadrant || '',
          quadrantColor: QUADRANT_COLOR[z.quadrant] || '#9aa5b5',
          diagnosis: z.diagnosis || '',
          suggestion: z.suggestion || ''
        }));
        this.setData({
          loading: false,
          ranking: pop.ranking || [],
          totalVisitors: pop.total_visitors || 0,
          topZone: pop.top_zone || '',
          anomaly: dash.anomaly || { total_alerts: 0, high_risk_count: 0, watch_count: 0 },
          zones: zones,
          summary: hv.summary || '',
          reports: (hr.reports || []).slice(0, 5)
        });
      })
      .catch((err) => {
        this.setData({ loading: false, error: err.message || '加载失败，请确认后端已启动' });
      });
  }
});
