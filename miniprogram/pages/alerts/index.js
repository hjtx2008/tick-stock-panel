const api = require('../../utils/api')
const fmt = require('../../utils/format')
const market = require('../../utils/market')

const POLL_MS = 10000
const DAYS = 3

Page({
  data: { alerts: [], days: DAYS, loading: false, error: '', trading: false },

  onShow() {
    this.load()
    this.stopPolling()
    this._timer = setInterval(() => {
      const trading = market.isTradingTime()
      if (trading !== this.data.trading) this.setData({ trading })
      if (trading) this.load()
    }, POLL_MS)
    this.setData({ trading: market.isTradingTime() })
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

  stopPolling() {
    if (this._timer) clearInterval(this._timer)
    this._timer = null
  },

  async load() {
    if (this._inflight) return this._inflight
    this.setData({ loading: true })
    this._inflight = api
      .alerts(DAYS, 200)
      .then(res => {
        const alerts = (res.alerts || []).map((a, i) => ({
          key: `${a.ts}-${a.rule_id || ''}-${a.symbol || ''}-${i}`,
          symbol: a.symbol || '',
          name: a.name || '',
          ruleName: a.rule_name || '',
          message: a.message || '',
          timeText: fmt.fmtTs(a.ts),
          priceText: fmt.isNum(a.price) ? fmt.fmtPrice(a.price) : '',
        }))
        this.setData({ alerts, error: '' })
      })
      .catch(err => this.setData({ error: err.message }))
      .finally(() => {
        this._inflight = null
        this.setData({ loading: false })
      })
    return this._inflight
  },

  onOpen(e) {
    const { symbol, name } = e.currentTarget.dataset
    if (!symbol) return
    wx.navigateTo({
      url: `/pages/stock/index?symbol=${encodeURIComponent(symbol)}&name=${encodeURIComponent(name || '')}`,
    })
  },
})
