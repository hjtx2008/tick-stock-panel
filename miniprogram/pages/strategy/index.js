const api = require('../../utils/api')
const fmt = require('../../utils/format')

Page({
  data: { id: '', rows: [], asOf: '', loading: false, empty: false, error: '' },

  onLoad(query) {
    const id = decodeURIComponent(query.id || '')
    const name = decodeURIComponent(query.name || '')
    this.setData({ id })
    wx.setNavigationBarTitle({ title: name || id })
    this.load()
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh())
  },

  async load() {
    this.setData({ loading: true })
    try {
      const res = await api.cachedResult(this.data.id)
      const result = res.result
      if (!result) {
        this.setData({ rows: [], empty: true, error: '' })
        return
      }
      // 策略行字段随策略而变: 价格 / 涨跌幅 / 评分缺失时显示「—」
      const rows = (result.rows || []).map(r => ({
        symbol: r.symbol,
        name: r.name,
        priceText: fmt.fmtPrice(r.close),
        pctText: fmt.fmtPct(r.change_pct),
        color: fmt.colorClass(r.change_pct),
        scoreText: fmt.isNum(r.score) ? r.score.toFixed(2) : '',
      }))
      this.setData({ rows, asOf: result.as_of || '', empty: false, error: '' })
    } catch (err) {
      this.setData({ error: err.message })
    } finally {
      this.setData({ loading: false })
    }
  },

  onOpen(e) {
    const { symbol, name } = e.currentTarget.dataset
    wx.navigateTo({
      url: `/pages/stock/index?symbol=${encodeURIComponent(symbol)}&name=${encodeURIComponent(name || '')}`,
    })
  },
})
