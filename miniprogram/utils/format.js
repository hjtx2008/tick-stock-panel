// 数字格式化。注意口径: enriched 的 change_pct 是**小数制** (0.05 = 5%),
// 与网页端 frontend/src/lib/format.ts 的 fmtPct 一致, 不做「小于 1 就乘 100」的猜测。

function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v)
}

function fmtPrice(v, digits = 2) {
  return isNum(v) ? v.toFixed(digits) : '—'
}

function fmtPct(v, digits = 2) {
  if (!isNum(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${(v * 100).toFixed(digits)}%`
}

function fmtAmount(v) {
  if (!isNum(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (a >= 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toFixed(0)
}

// A 股口径: 涨红跌绿
function colorClass(v) {
  if (!isNum(v) || v === 0) return 'flat'
  return v > 0 ? 'up' : 'down'
}

function pad2(n) {
  return n < 10 ? '0' + n : String(n)
}

// 毫秒时间戳 → 北京时间 MM-DD HH:mm (不依赖手机时区)
function fmtTs(ms) {
  if (!isNum(ms)) return '—'
  const d = new Date(ms + 8 * 3600 * 1000)
  return `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
}

module.exports = { isNum, fmtPrice, fmtPct, fmtAmount, colorClass, fmtTs, pad2 }
