const api = require('../../utils/api')

const POLL_MS = 3000
const POLL_MAX_MS = 5 * 60 * 1000

Page({
  data: { items: [], asOf: '', loading: false, running: false, pendingCount: 0, error: '' },

  onShow() {
    this.load()
  },

  onHide() {
    this.stopPolling()
  },

  onUnload() {
    this.stopPolling()
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh())
  },

  async load() {
    this.setData({ loading: true })
    try {
      if (!this._presets) {
        const res = await api.strategies()
        this._presets = (res.presets || []).filter(p => !p.research_only)
      }
      const summary = await api.cachedSummary()
      this.render(summary)
      this.setData({ error: '' })
    } catch (err) {
      this.setData({ error: err.message })
    } finally {
      this.setData({ loading: false })
    }
  },

  render(summary) {
    const results = (summary && summary.results) || {}
    const ever = (summary && summary.today_ever_counts) || {}
    const pending = new Set(this._pending || [])
    const items = (this._presets || []).map(p => {
      const r = results[p.id]
      const sub = []
      if (r && r.error) sub.push('本次运行失败, 已跳过')
      else if (ever[p.id] !== undefined && r && ever[p.id] > r.total) sub.push(`今日曾命中 ${ever[p.id]} 只`)
      if (p.description && !sub.length) sub.push(p.description)
      return {
        id: p.id,
        name: p.name || p.id,
        sub: sub.join(' · '),
        total: r && !r.error ? r.total : null,
        error: !!(r && r.error),
        pending: pending.has(p.id) && !r,
      }
    })
    this.setData({ items, asOf: (summary && summary.as_of) || '', pendingCount: pending.size })
  },

  async onRunAll() {
    if (this.data.running) return
    this.setData({ running: true, error: '' })
    try {
      const res = await api.runAllStrategies()
      this._pending = res.pending || []
      const summary = await api.cachedSummary()
      this.render(summary)
      if (this._pending.length) this.startPolling()
      else this.setData({ running: false })
      if (res.failed && Object.keys(res.failed).length) {
        wx.showToast({ title: `${Object.keys(res.failed).length} 个策略运行失败`, icon: 'none' })
      }
    } catch (err) {
      this.setData({ running: false, error: err.message })
    }
  },

  startPolling() {
    this.stopPolling()
    const started = Date.now()
    this._timer = setInterval(async () => {
      try {
        const summary = await api.cachedSummary()
        const results = summary.results || {}
        this._pending = (this._pending || []).filter(id => !results[id])
        this.render(summary)
        if (!this._pending.length || Date.now() - started > POLL_MAX_MS) {
          this.stopPolling()
          this.setData({ running: false })
        }
      } catch (err) {
        this.stopPolling()
        this.setData({ running: false, error: err.message })
      }
    }, POLL_MS)
  },

  stopPolling() {
    if (this._timer) clearInterval(this._timer)
    this._timer = null
  },

  onOpen(e) {
    const { id, name } = e.currentTarget.dataset
    wx.navigateTo({ url: `/pages/strategy/index?id=${encodeURIComponent(id)}&name=${encodeURIComponent(name)}` })
  },
})
