// 日 K 蜡烛图 + 缠论叠加层 (笔 / 线段 / 中枢 / 买卖点), Canvas 2D 自绘。
// 价格口径由调用方保证一致: 蜡烛与叠加层都用前复权价 (缠论接口 price_mode=qfq)。
const COLOR = {
  up: '#C74040',
  down: '#2D9B65',
  grid: '#262a33',
  text: '#8a8f98',
  pivot: 'rgba(167, 139, 250, 0.18)',
  pivotLine: 'rgba(167, 139, 250, 0.7)',
  stroke: '#d9b65a',
  segment: '#5aa9e6',
}
const PAD = { left: 6, right: 52, top: 22, bottom: 20 }
const TAP_SLOP = 6

Component({
  properties: {
    bars: { type: Array, value: [] },
    overlay: { type: Object, value: null },
    visible: { type: Number, value: 90 },
    height: { type: Number, value: 300 },
  },

  data: { tip: '' },

  observers: {
    'bars, overlay, visible'() {
      this._offset = 0
      this._selected = -1
      this.setData({ tip: '' })
      this.draw()
    },
  },

  lifetimes: {
    attached() {
      this._offset = 0
      this._selected = -1
    },
    ready() {
      this.createSelectorQuery()
        .select('#kc')
        .fields({ node: true, size: true })
        .exec(res => {
          const info = res && res[0]
          if (!info || !info.node) return
          const dpr = wx.getWindowInfo ? wx.getWindowInfo().pixelRatio : wx.getSystemInfoSync().pixelRatio
          const canvas = info.node
          canvas.width = info.width * dpr
          canvas.height = info.height * dpr
          const ctx = canvas.getContext('2d')
          ctx.scale(dpr, dpr)
          this._ctx = ctx
          this._w = info.width
          this._h = info.height
          this.draw()
        })
    },
  },

  methods: {
    _view() {
      const n = this.data.bars.length
      const count = Math.max(10, Math.min(this.data.visible, n))
      const maxOffset = Math.max(0, n - count)
      this._offset = Math.max(0, Math.min(this._offset, maxOffset))
      const end = n - this._offset
      const start = Math.max(0, end - count)
      const plotW = this._w - PAD.left - PAD.right
      return { n, start, end, count, step: plotW / count }
    },

    draw() {
      const ctx = this._ctx
      if (!ctx) return
      const W = this._w
      const H = this._h
      ctx.clearRect(0, 0, W, H)
      const bars = this.data.bars || []
      if (!bars.length) return

      const { start, end, step } = this._view()
      const visibleBars = bars.slice(start, end)
      const indexOf = {}
      bars.forEach((b, i) => { indexOf[b.date] = i })
      const ov = this.data.overlay || {}
      const inView = (d) => indexOf[d] !== undefined && indexOf[d] >= start && indexOf[d] < end

      // 价格区间: 可见蜡烛 + 端点落在窗口内的叠加层价格
      let hi = -Infinity
      let lo = Infinity
      visibleBars.forEach(b => { hi = Math.max(hi, b.high); lo = Math.min(lo, b.low) })
      ;[...(ov.strokes || []), ...(ov.segments || [])].forEach(s => {
        if (inView(s.start_date)) { hi = Math.max(hi, s.start_price); lo = Math.min(lo, s.start_price) }
        if (inView(s.end_date)) { hi = Math.max(hi, s.end_price); lo = Math.min(lo, s.end_price) }
      })
      if (!(hi > lo)) { hi += 1; lo -= 1 }
      const margin = (hi - lo) * 0.06
      hi += margin
      lo -= margin

      const top = PAD.top
      const plotH = H - PAD.top - PAD.bottom
      const xOf = (i) => PAD.left + (i - start + 0.5) * step
      const yOf = (p) => top + (hi - p) / (hi - lo) * plotH

      // 网格与价格刻度
      ctx.strokeStyle = COLOR.grid
      ctx.lineWidth = 1
      ctx.fillStyle = COLOR.text
      ctx.font = '10px sans-serif'
      for (let k = 0; k <= 4; k++) {
        const y = top + plotH * k / 4
        ctx.beginPath()
        ctx.moveTo(PAD.left, y)
        ctx.lineTo(W - PAD.right, y)
        ctx.stroke()
        ctx.fillText((hi - (hi - lo) * k / 4).toFixed(2), W - PAD.right + 4, y + 3)
      }
      ctx.fillText(visibleBars[0].date, PAD.left, H - 5)
      const lastLabel = visibleBars[visibleBars.length - 1].date
      ctx.fillText(lastLabel, W - PAD.right - ctx.measureText(lastLabel).width, H - 5)

      ctx.save()
      ctx.beginPath()
      ctx.rect(PAD.left, top, W - PAD.left - PAD.right, plotH)
      ctx.clip()

      // 中枢 (矩形)
      ;(ov.pivots || []).forEach(p => {
        const a = indexOf[p.start_date]
        const b = indexOf[p.end_date]
        if (a === undefined || b === undefined || b < start || a >= end) return
        const x1 = xOf(a) - step / 2
        const x2 = xOf(b) + step / 2
        ctx.fillStyle = COLOR.pivot
        ctx.fillRect(x1, yOf(p.zg), x2 - x1, yOf(p.zd) - yOf(p.zg))
        ctx.strokeStyle = COLOR.pivotLine
        ctx.strokeRect(x1, yOf(p.zg), x2 - x1, yOf(p.zd) - yOf(p.zg))
      })

      // 蜡烛
      const bodyW = Math.max(1, step * 0.7)
      for (let i = start; i < end; i++) {
        const b = bars[i]
        const color = b.close >= b.open ? COLOR.up : COLOR.down
        const x = xOf(i)
        ctx.strokeStyle = color
        ctx.fillStyle = color
        ctx.beginPath()
        ctx.moveTo(x, yOf(b.high))
        ctx.lineTo(x, yOf(b.low))
        ctx.stroke()
        const y1 = yOf(Math.max(b.open, b.close))
        const y2 = yOf(Math.min(b.open, b.close))
        ctx.fillRect(x - bodyW / 2, y1, bodyW, Math.max(1, y2 - y1))
      }

      // 笔 / 线段
      const drawLines = (list, color, width) => {
        ctx.strokeStyle = color
        ctx.lineWidth = width
        list.forEach(s => {
          const a = indexOf[s.start_date]
          const b = indexOf[s.end_date]
          if (a === undefined || b === undefined || b < start || a >= end) return
          ctx.setLineDash(s.confirmed === false ? [4, 3] : [])
          ctx.beginPath()
          ctx.moveTo(xOf(a), yOf(s.start_price))
          ctx.lineTo(xOf(b), yOf(s.end_price))
          ctx.stroke()
        })
        ctx.setLineDash([])
        ctx.lineWidth = 1
      }
      drawLines(ov.strokes || [], COLOR.stroke, 1)
      drawLines(ov.segments || [], COLOR.segment, 2.5)

      // 买卖点: 买点标在 K 线下方 (红), 卖点标在上方 (绿)
      ctx.font = '10px sans-serif'
      ;(ov.signals || []).forEach(sig => {
        const i = indexOf[sig.date]
        if (i === undefined || i < start || i >= end) return
        const b = bars[i]
        const buy = String(sig.kind).startsWith('b')
        const text = sig.label
        const tw = ctx.measureText(text).width
        const y = buy ? yOf(b.low) + 12 : yOf(b.high) - 4
        ctx.fillStyle = buy ? COLOR.up : COLOR.down
        ctx.fillText(text, xOf(i) - tw / 2, y)
      })

      // 选中十字线
      if (this._selected >= start && this._selected < end) {
        ctx.strokeStyle = COLOR.text
        ctx.setLineDash([3, 3])
        const x = xOf(this._selected)
        ctx.beginPath()
        ctx.moveTo(x, top)
        ctx.lineTo(x, top + plotH)
        ctx.stroke()
        ctx.setLineDash([])
      }
      ctx.restore()
    },

    onTouchStart(e) {
      const t = e.touches[0]
      this._touch = { x: t.x, startX: t.x, offset: this._offset }
    },

    onTouchMove(e) {
      if (!this._touch) return
      const t = e.touches[0]
      const { step } = this._view()
      // 手指右拖 → 看更早的 K 线 (offset 增大)
      const delta = Math.round((t.x - this._touch.startX) / step)
      const next = this._touch.offset + delta
      if (next !== this._offset) {
        this._offset = next
        this.draw()
      }
      this._touch.x = t.x
    },

    onTouchEnd() {
      const touch = this._touch
      this._touch = null
      if (!touch || Math.abs(touch.x - touch.startX) > TAP_SLOP) return
      const { start, end, step } = this._view()
      const i = start + Math.floor((touch.startX - PAD.left) / step)
      if (i < start || i >= end) return
      this._selected = i
      const b = this.data.bars[i]
      const pct = i > 0 ? (b.close / this.data.bars[i - 1].close - 1) * 100 : null
      const pctText = pct === null ? '' : ` ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
      this.setData({
        tip: `${b.date} 开${b.open.toFixed(2)} 高${b.high.toFixed(2)} 低${b.low.toFixed(2)} 收${b.close.toFixed(2)}${pctText}`,
      })
      this.draw()
    },
  },
})
