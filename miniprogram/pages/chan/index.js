const api = require('../../utils/api')
const fmt = require('../../utils/format')
const chan = require('../../utils/chan')

const DEFAULT_KINDS = ['b1', 'b2', 'b3']

Page({
  data: {
    kinds: chan.SIGNAL_KINDS.map(k => ({ ...k, active: DEFAULT_KINDS.includes(k.value) })),
    ages: [
      { value: 3, label: '3 根内' },
      { value: 5, label: '5 根内' },
      { value: 10, label: '10 根内' },
      { value: 0, label: '不限' },
    ],
    maxAge: 5,
    running: false,
    ran: false,
    matches: [],
    scanned: 0,
    totalMatched: 0,
    elapsedText: '',
    error: '',
  },

  onToggleKind(e) {
    const value = e.currentTarget.dataset.value
    this.setData({
      kinds: this.data.kinds.map(k => (k.value === value ? { ...k, active: !k.active } : k)),
    })
  },

  onAge(e) {
    this.setData({ maxAge: Number(e.currentTarget.dataset.value) })
  },

  async onRun() {
    if (this.data.running) return
    const signalKinds = this.data.kinds.filter(k => k.active).map(k => k.value)
    if (!signalKinds.length) {
      wx.showToast({ title: '至少选一种买卖点', icon: 'none' })
      return
    }
    this.setData({ running: true, error: '' })
    try {
      const res = await api.chanScreen({
        signal_kinds: signalKinds,
        max_signal_age: this.data.maxAge,
        limit: 200,
      })
      const wanted = new Set(signalKinds)
      const matches = (res.matches || []).map(m => {
        // 展示被条件命中的最近一个买卖点
        const sig = (m.signals || []).filter(s => wanted.has(s.kind)).slice(-1)[0]
        return {
          symbol: m.symbol,
          name: m.name,
          trendText: chan.TREND_LABEL[m.trend] || '—',
          closeText: fmt.fmtPrice(m.last_close),
          signal: sig ? { label: sig.label, date: sig.date, buy: chan.isBuy(sig.kind) } : null,
        }
      })
      this.setData({
        ran: true,
        matches,
        scanned: res.scanned || 0,
        totalMatched: res.total_matched || 0,
        elapsedText: fmt.isNum(res.elapsed_ms) ? `${(res.elapsed_ms / 1000).toFixed(1)} 秒` : '',
      })
    } catch (err) {
      this.setData({ error: err.message })
    } finally {
      this.setData({ running: false })
    }
  },

  onOpen(e) {
    const { symbol, name } = e.currentTarget.dataset
    wx.navigateTo({
      url: `/pages/stock/index?symbol=${encodeURIComponent(symbol)}&name=${encodeURIComponent(name || '')}`,
    })
  },
})
