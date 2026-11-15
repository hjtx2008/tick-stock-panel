// A 股交易时段判断 (北京时间, 不依赖手机时区)。只用来决定是否轮询,
// 不判断节假日 —— 节假日轮询拿到的只是不变的收盘数据, 无害。

function beijingNow() {
  const now = new Date()
  return new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 8 * 3600000)
}

function isTradingTime(date) {
  const d = date || beijingNow()
  const day = d.getDay()
  if (day === 0 || day === 6) return false
  const m = d.getHours() * 60 + d.getMinutes()
  // 9:15 集合竞价起 ~ 11:30, 13:00 ~ 15:00
  return (m >= 555 && m <= 690) || (m >= 780 && m <= 900)
}

module.exports = { beijingNow, isTradingTime }
