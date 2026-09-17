/**
 * ECharts 图表模块
 * 负责热度柱状图的初始化和更新
 * - 支持迟初始化（容器在布局稳定前可能宽度为 0，更新数据时自动补建实例）
 * - 提供 resize() 供外部在布局稳定后校正尺寸（Vue 挂载瞬间宽度为 0 时图表会塌缩成 100px）
 * - echarts 未加载（CDN 不可用）时优雅降级，不影响页面其他功能
 */

/**
 * HTML 转义（台账 B5）。
 *
 * ECharts 的 `tooltip.formatter` 返回的是 **HTML 字符串**，而里面会拼进
 * `zone_label`（区域名可由任意已登录账号通过 `PUT /api/zones` 修改）——
 * 不转义就是**存储型 XSS**：改个区域名，别人打开看板就中招。
 * 注意：只在"要输出 HTML"的地方用；纯文本渲染不需要（也不该）走这里。
 */
function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

const ChartManager = {
    chart: null,
    domId: null,

    init(chartDomId) {
        this.domId = chartDomId;
        const dom = document.getElementById(chartDomId);
        if (!dom) return;

        if (typeof echarts === 'undefined') {
            console.warn('[Chart] echarts 未加载（CDN 不可用），热度图暂不可用');
            return;
        }

        this.chart = echarts.init(dom, 'dark');
        this._setEmpty();
        this._observe(dom);

        // 响应式
        window.addEventListener('resize', () => {
            if (this.chart) this.chart.resize();
        });
    },

    _observe(dom) {
        // 容器尺寸变化（布局稳定、v-show 模式切换等）时自动校正图表尺寸，
        // 避免在挂载瞬间容器宽度为 0 时图表塌缩成默认 100px
        if (typeof ResizeObserver === 'undefined') return;
        if (this._ro) {
            this._ro.disconnect();
        }
        let raf = null;
        this._ro = new ResizeObserver(() => {
            if (raf) return;
            raf = requestAnimationFrame(() => {
                raf = null;
                if (this.chart) this.chart.resize();
            });
        });
        this._ro.observe(dom);
    },

    ensureInit() {
        if (!this.chart && this.domId && typeof echarts !== 'undefined') {
            const dom = document.getElementById(this.domId);
            if (dom) {
                this.chart = echarts.init(dom, 'dark');
                this._setEmpty();
                this._observe(dom);
            }
        }
        return this.chart;
    },

    resize() {
        if (this.chart) {
            this.chart.resize();
        }
    },

    _setEmpty() {
        const chart = this.chart;
        if (!chart) return;
        chart.setOption({
            title: { text: '等待数据...', left: 'center', top: 'center',
                     textStyle: { color: '#8899aa', fontSize: 14 } },
            xAxis: { show: false },
            yAxis: { show: false },
            series: [],
        });
    },

    updateFromStats(statsData) {
        const chart = this.ensureInit();
        if (!chart) return;

        const zones = statsData.zones || statsData;
        const items = Object.values(zones);
        if (!items.length) {
            this._setEmpty();
            return;
        }

        items.sort((a, b) => (b.heat_score || 0) - (a.heat_score || 0));
        const labels = items.map(z => z.zone_label || z.zone_id);
        const scores = items.map(z => z.heat_score || 0);
        const visits = items.map(z => z.visit_count || 0);
        const dwells = items.map(z => z.total_dwell_seconds || 0);

        const option = {
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'shadow' },
                formatter: function(params) {
                    const i = params[0].dataIndex;
                    // ⚠ params[0].name 就是区域名（可被用户改写）→ 必须转义，否则是存储型 XSS（台账 B5）
                    return `${escapeHtml(params[0].name)}<br/>
                        热度分: <b>${Number(scores[i] || 0).toFixed(1)}</b><br/>
                        到访: ${Number(visits[i] || 0)} 人次<br/>
                        总停留: ${Number(dwells[i] || 0).toFixed(0)} 秒`;
                }
            },
            grid: { left: 8, right: 20, top: 10, bottom: 30 },
            xAxis: {
                type: 'category', data: labels,
                axisLabel: { color: '#8899aa', fontSize: 10, rotate: labels.length > 5 ? 30 : 0 },
                axisLine: { lineStyle: { color: '#2a3f55' } },
            },
            yAxis: {
                type: 'value', name: '热度分',
                nameTextStyle: { color: '#8899aa', fontSize: 10 },
                axisLabel: { color: '#8899aa' },
                splitLine: { lineStyle: { color: '#2a3f55', type: 'dashed' } },
            },
            series: [{
                type: 'bar',
                data: scores.map(v => ({
                    value: v,
                    itemStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: '#4da6ff' },
                            { offset: 1, color: '#1a4a80' },
                        ]),
                        borderRadius: [6, 6, 0, 0],
                    },
                })),
                barWidth: '55%',
            }],
        };
        chart.setOption(option, true);
    },

    showHeatmap(data) {
        // 热力图预留（远期），当前用柱状图
        this.updateFromStats(data);
    },
};
