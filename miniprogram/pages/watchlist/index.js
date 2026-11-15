const api = require('../../utils/api')
const fmt = require('../../utils/format')
const market = require('../../utils/market')

const POLL_MS = 5000

function toRow(r) {
  return {
    symbol: r.symbol,
    name: r.name,
    priceText: fmt.fmtPrice(r.close),
    pctText: fmt.fmtPct(r.change_pct),
    amountText: fmt.fmtAmount(r.amount),
    color: fmt.colorClass(r.change_pct),
  }
}

Page({
  data: { rows: [], asOf: '', loading: false, error: '', trading: false },

  onShow() {
    this.load()
    this.startPolling()
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

  startPolling() {
    this.stopPolling()
    this._timer = setInterval(() => {
      const trading = market.isTradingTime()
      if (trading !== this.data.trading) this.setData({ trading })
      if (trading) this.load()
    }, POLL_MS)
    this.setData({ trading: market.isTradingTime() })
  },

  stopPolling() {
    if (this._timer) clearInterval(this._timer)
    this._timer = null
  },

  async load() {
    // 上一轮未返回时跳过, 避免慢网络下请求堆积
    if (this._inflight) return this._inflight
    this.setData({ loading: true })
    this._inflight = api
      .watchlistEnriched()
      .then(res => {
        this.setData({ rows: (res.rows || []).map(toRow), asOf: res.as_of || '', error: '' })
      })
      .catch(err => {
        // 保留上一份有效数据, 只提示错误
        this.setData({ error: err.message })
      })
      .finally(() => {
        this._inflight = null
        this.setData({ loading: false })
      })
    return this._inflight
  },

  onOpen(e) {
    const { symbol, name } = e.currentTarget.dataset
    wx.navigateTo({
      url: `/pages/stock/index?symbol=${encodeURIComponent(symbol)}&name=${encodeURIComponent(name || '')}`,
    })
  },
})
