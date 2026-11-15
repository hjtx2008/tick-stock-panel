const api = require('../../utils/api')
const fmt = require('../../utils/format')
const chan = require('../../utils/chan')

// 与缠论选股同一窗口 (全市场最近 250 个交易日), 保证选出来的票与这里看到的结构一致
const CHAN_DAYS = 250
// 日 K 取 290 个自然日 (约 200 根): 后端只缓存约 300 天, 再长会慢到十几秒。
// 图上只画这 200 根, 更早的笔/中枢落在窗口外不画, 结构本身仍按 250 日计算。
const KLINE_CALENDAR_DAYS = 290

Page({
  data: {
    symbol: '',
    name: '',
    bars: [],
    overlay: null,
    summary: null,
    signals: [],
    last: null,
    visible: 90,
    windows: [60, 90, 150, 200],
    showChan: true,
    chartHeight: 300,
    loading: false,
    chanLoading: false,
    error: '',
    chanError: '',
  },

  onLoad(query) {
    const symbol = decodeURIComponent(query.symbol || '')
    const name = decodeURIComponent(query.name || '')
    const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()
    this.setData({ symbol, name, chartHeight: Math.round(info.windowWidth * 0.78) })
    wx.setNavigationBarTitle({ title: name || symbol })
    this.load()
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh())
  },

  async load() {
    const { symbol } = this.data
    if (!symbol) return
    this.setData({ loading: true, error: '', chanError: '', chanLoading: true })
    // K 线秒回、缠论结构要十几秒: 两个请求并发, 各自返回就各自渲染, 不互相等待
    const klineTask = api
      .klineDaily(symbol, KLINE_CALENDAR_DAYS)
      .then(k => this.applyKline(k))
      .catch(err => this.setData({ error: err.message }))
      .finally(() => this.setData({ loading: false }))
    const chanTask = api
      .chanStructure(symbol, CHAN_DAYS)
      .then(st => this.applyChan(st))
      .catch(err => this.setData({ chanError: err.message, overlay: null, summary: null, signals: [] }))
      .finally(() => this.setData({ chanLoading: false }))
    await Promise.all([klineTask, chanTask])
  },

  applyKline(k) {
    const rows = k.rows || []
    const bars = rows.map(r => ({
      date: String(r.date).slice(0, 10),
      open: r.open,
      high: r.high,
      low: r.low,
      close: r.close,
    }))
    const lastRow = rows[rows.length - 1]
    const name = this.data.name || k.name || ''
    this.setData({
      bars,
      name,
      last: lastRow
        ? { priceText: fmt.fmtPrice(lastRow.close), pctText: fmt.fmtPct(lastRow.change_pct), color: fmt.colorClass(lastRow.change_pct) }
        : null,
    })
    if (name) wx.setNavigationBarTitle({ title: name })
  },

  applyChan(st) {
    this.setData({
      overlay: {
        strokes: st.strokes || [],
        segments: st.segments || [],
        pivots: st.pivots || [],
        signals: st.signals || [],
      },
      summary: {
        trendText: chan.TREND_LABEL[(st.summary || {}).trend] || '—',
        strokes: (st.strokes || []).length,
        segments: (st.segments || []).length,
        pivots: (st.pivots || []).length,
        divergences: (st.divergences || []).filter(d => d.diverged).length,
      },
      signals: (st.signals || [])
        .slice()
        .reverse()
        .slice(0, 20)
        .map(s => ({
          key: s.kind + s.date,
          label: s.label,
          date: s.date,
          buy: chan.isBuy(s.kind),
          priceText: fmt.fmtPrice(s.price),
        })),
    })
  },

  onWindow(e) {
    this.setData({ visible: Number(e.currentTarget.dataset.n) })
  },

  onToggleChan() {
    this.setData({ showChan: !this.data.showChan })
  },
})
