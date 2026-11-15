// 缠论买卖点类型 (与后端 app/custom/chan VALID_SIGNAL_KINDS 一致)
const SIGNAL_KINDS = [
  { value: 'b1', label: '一买' },
  { value: 'b2', label: '二买' },
  { value: 'b3', label: '三买' },
  { value: 'b1s', label: '结构一买' },
  { value: 's1', label: '一卖' },
  { value: 's2', label: '二卖' },
  { value: 's3', label: '三卖' },
  { value: 's1s', label: '结构一卖' },
]

const TREND_LABEL = { up: '上涨', down: '下跌', oscillation: '盘整' }

function isBuy(kind) {
  return String(kind || '').startsWith('b')
}

module.exports = { SIGNAL_KINDS, TREND_LABEL, isBuy }
