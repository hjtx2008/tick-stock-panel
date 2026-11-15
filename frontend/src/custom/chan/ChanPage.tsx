import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { AlertTriangle, GitBranch, Layers, Loader2, Target, TriangleAlert } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import {
  EChartsCandlestick,
  type ChanChartOverlay,
  type ChartMarker,
  type OHLC,
} from '@/components/EChartsCandlestick'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 日历日 ≈ 交易日 × 1.45 —— 后端按交易日裁剪, 前端按日历日取窗口, 这里留一点缓冲 */
const CALENDAR_PER_TRADING_DAY = 1.5

const DAY_OPTIONS = [
  { value: 120, label: '120 日' },
  { value: 250, label: '250 日' },
  { value: 500, label: '500 日' },
]

const PRICE_MODE_OPTIONS = [
  { value: 'qfq', label: '前复权' },
  { value: 'raw', label: '原始价' },
  { value: 'hfq', label: '后复权' },
] as const

type PriceMode = (typeof PRICE_MODE_OPTIONS)[number]['value']

const TREND_LABEL: Record<string, string> = {
  up: '上涨',
  down: '下跌',
  oscillation: '盘整',
}

const TREND_COLOR: Record<string, string> = {
  up: 'text-[#C74040]',
  down: 'text-[#2D9B65]',
  oscillation: 'text-sky-300',
}

function fmtNum(value: number | undefined | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

/**
 * 买卖点文字错位排版的价格乘数 (通达信口径)。
 *
 * 买点在 K 线下方、卖点在 K 线上方, 级别越高离 K 线越近 ——
 * 这样一买/二买/三买(及镜像)同时出现时文字不会叠在一起。
 */
const SIGNAL_PRICE_RATIO: Record<string, number> = {
  b1: 0.975,
  b2: 0.985,
  b3: 0.995,
  s1: 1.025,
  s2: 1.015,
  s3: 1.005,
  // 结构一买/一卖 (czsc 口径) 与趋势一买/一卖可能落在同一根 K 线, 再往外错开一档
  b1s: 0.965,
  s1s: 1.035,
}

/**
 * 缠论结构分析页 (后端扩展 chan.analysis)。
 *
 * 图上叠加: 笔(细线) / 线段(粗线) / 中枢(紫色矩形) / 买卖点(箭头)。
 * 价格口径由后端按所选 price_mode 统一计算 —— 图上叠加线的价格与
 * 结构对象价格同源, 不在前端二次复权。
 */
export function ChanPage() {
  // 支持从选股页带 ?symbol=xxx 进来 (选股结果点回详情)
  const [searchParams] = useSearchParams()
  const urlSymbol = searchParams.get('symbol') ?? ''
  const [symbol, setSymbol] = useState(urlSymbol)
  const [name, setName] = useState('')
  const [days, setDays] = useState(250)
  const [priceMode, setPriceMode] = useState<PriceMode>('qfq')

  // URL 变化 (从选股页跳转) 时同步标的
  useEffect(() => {
    if (urlSymbol && urlSymbol !== symbol) {
      setSymbol(urlSymbol)
      setName('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSymbol])

  const kline = useQuery({
    queryKey: QK.chanKline(symbol, days),
    queryFn: () => api.klineDaily(symbol, Math.ceil(days * CALENDAR_PER_TRADING_DAY) + 30),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  const chan = useQuery({
    queryKey: QK.chanStructure(symbol, days, priceMode),
    queryFn: () => api.chanStructure(symbol, days, priceMode),
    enabled: !!symbol,
    staleTime: 60_000,
    retry: false,
  })

  const ohlc: OHLC[] = useMemo(() => {
    const rows = kline.data?.rows ?? []
    // 后端缠论按「交易日数」裁剪, 日 K 接口按「日历日」取窗口 → 两者区间略有出入。
    // 这里以缠论样本起点为准裁剪, 保证叠加层铺满整张图。
    const from = chan.data?.dates?.[0]
    const picked = from ? rows.filter(row => row.date >= from) : rows
    // 日 K 接口固定返回前复权价; 结构按所选 price_mode 计算。原始价 / 后复权时
    // 用同日「结构收盘 / 前复权收盘」比例换算蜡烛 (同一交易日内复权是等比缩放),
    // 让蜡烛与笔、线段、中枢使用同一套价格; 结构里没有的日期不画, 避免错位。
    const modeClose = new Map<string, number>()
    if (priceMode !== 'qfq' && chan.data?.price_mode === priceMode) {
      chan.data.dates.forEach((d, i) => modeClose.set(d, chan.data!.closes[i]))
    }
    return picked.flatMap(row => {
      let k = 1
      if (priceMode !== 'qfq') {
        const target = modeClose.get(row.date)
        if (target == null || !row.close) return []
        k = target / row.close
      }
      return [{
        date: row.date,
        open: row.open * k,
        high: row.high * k,
        low: row.low * k,
        close: row.close * k,
        volume: row.volume,
      }]
    })
  }, [kline.data, chan.data, priceMode])

  // 叠加层用 useMemo 固化引用 —— 否则每次渲染都会触发 ECharts 全量 setOption
  const overlay: ChanChartOverlay | undefined = useMemo(() => {
    const data = chan.data
    if (!data) return undefined
    return {
      strokes: (data.strokes ?? []).map(s => ({
        startDate: s.start_date,
        startPrice: s.start_price,
        endDate: s.end_date,
        endPrice: s.end_price,
        direction: s.direction,
      })),
      segments: (data.segments ?? []).map(s => ({
        startDate: s.start_date,
        startPrice: s.start_price,
        endDate: s.end_date,
        endPrice: s.end_price,
        direction: s.direction,
        confirmed: s.confirmed,
      })),
      pivots: [
        ...(data.pivots ?? []).map(p => ({
          startDate: p.start_date,
          endDate: p.end_date,
          zg: p.zg,
          zd: p.zd,
          direction: p.direction,
          nestedLevel: p.nested_level,
        })),
        // 段级中枢复用同一条绘制通路, 用 nestedLevel=1 走青色画法
        ...(data.segment_pivots ?? []).map(p => ({
          startDate: p.start_date,
          endDate: p.end_date,
          zg: p.zg,
          zd: p.zd,
          direction: p.direction,
          nestedLevel: 1,
        })),
      ],
      fractals: (data.fractals ?? []).map(f => ({
        date: f.date,
        price: f.price,
        kind: f.kind === 'top' ? 'top' : 'bottom',
      })),
      divergences: (data.divergences ?? [])
        .filter(d => d.diverged)
        .map(d => ({
          date: d.date,
          price: d.price,
          direction: d.direction,
          scope: d.scope,
          diverged: d.diverged,
        })),
      segmentDivergences: (data.segment_divergences ?? [])
        .filter(d => d.diverged)
        .map(d => ({
          date: d.date,
          price: d.price,
          direction: d.direction,
          scope: d.scope,
          diverged: d.diverged,
        })),
    }
  }, [chan.data])

  // 买卖点文字错位排版 (通达信口径):
  //   买 一买 LOW×0.975 / 二买 ×0.985 / 三买 ×0.995
  //   卖 一卖 HIGH×1.025 / 二卖 ×1.015 / 三卖 ×1.005
  // 目的: 不同级别的买卖点落在不同高度, 文字不会互相压盖。
  const markers: ChartMarker[] = useMemo(() => {
    const ohlcByDate = new Map(ohlc.map(o => [o.date, o]))
    return (chan.data?.signals ?? []).map(sig => {
      const isBuy = sig.kind.startsWith('b')
      const ratio = SIGNAL_PRICE_RATIO[sig.kind] ?? (isBuy ? 0.98 : 1.02)
      const bar = ohlcByDate.get(sig.date)
      const base = isBuy ? (bar?.low ?? sig.price) : (bar?.high ?? sig.price)
      return {
        date: sig.date,
        kind: isBuy ? 'buy' : 'sell',
        label: sig.label,
        price: base * ratio,
      }
    })
  }, [chan.data, ohlc])

  const summary = chan.data?.summary
  const signals = chan.data?.signals ?? []
  const pivots = chan.data?.pivots ?? []
  const segmentPivots = chan.data?.segment_pivots ?? []
  const strokes = chan.data?.strokes ?? []
  const divergences = (chan.data?.divergences ?? []).filter(d => d.diverged)
  const segmentDivergences = (chan.data?.segment_divergences ?? []).filter(d => d.diverged)

  const loading = !!symbol && (chan.isLoading || kline.isLoading)
  const errorText = chan.error instanceof Error ? chan.error.message : null

  return (
    <>
      <PageHeader
        title="缠论结构"
        subtitle="日线级别 · 包含处理 / 分型 / 成笔 / 线段 / 中枢 / 背驰 / 买卖点"
        right={
          <div className="flex items-center gap-2 text-[10px] font-mono text-muted">
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-[2px] bg-[#C74040]" />上涨笔</span>
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-[2px] bg-[#2D9B65]" />下跌笔</span>
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-[3px] bg-[#C74040]" />线段</span>
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-2 bg-violet-500/30 border border-violet-500/60" />中枢</span>
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-2 bg-cyan-400/20 border border-cyan-400/60" />段中枢</span>
            <span className="flex items-center gap-1"><i className="inline-block w-3 h-0 border-t border-dashed border-violet-500/70" />中轴</span>
            <span className="flex items-center gap-1"><i className="inline-block w-2 h-2 rotate-45 bg-[#FACC15]" />背驰</span>
            <span className="flex items-center gap-1"><i className="inline-block w-2 h-2 rounded-full bg-[#FB923C]" />段背驰</span>
          </div>
        }
      />

      <div className="w-full px-5 py-4 space-y-4">
        {/* 控制栏 */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="w-72">
            <StockFinancialSearch
              onSelect={(sym, nm) => { setSymbol(sym); setName(nm) }}
              assetTypes="stock,etf,index"
            />
          </div>
          {symbol && (
            <span className="flex items-baseline gap-2">
              <span className="text-sm text-foreground">{name || symbol}</span>
              <span className="text-[10px] font-mono text-muted">{symbol}</span>
            </span>
          )}
          <div className="flex items-center gap-1 ml-auto">
            <span className="text-[10px] text-muted">周期</span>
            {DAY_OPTIONS.map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setDays(opt.value)}
                className={`px-2 py-1 rounded-btn text-xs transition-colors ${
                  days === opt.value
                    ? 'bg-sky-400/[0.12] text-sky-300 border border-sky-400/30'
                    : 'text-secondary hover:bg-elevated border border-transparent'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1">
            <span className="text-[10px] text-muted">口径</span>
            {PRICE_MODE_OPTIONS.map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setPriceMode(opt.value)}
                className={`px-2 py-1 rounded-btn text-xs transition-colors ${
                  priceMode === opt.value
                    ? 'bg-sky-400/[0.12] text-sky-300 border border-sky-400/30'
                    : 'text-secondary hover:bg-elevated border border-transparent'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* 主体: 左图 + 右摘要 */}
        <div className="grid grid-cols-[1fr_320px] gap-4 items-start">
          <div className="min-w-0 rounded-card border border-border bg-elevated/30 p-3 relative">
            {/* 图表容器常驻渲染 (铁律 #2): loading/empty 用覆盖层, 不做分支切换 */}
            <EChartsCandlestick
              data={ohlc}
              markers={markers}
              chanOverlay={overlay}
              height={520}
              visibleBars="all"
              showMA={false}
            />
            {!symbol && (
              <div className="absolute inset-0 grid place-items-center bg-base/70">
                <EmptyState
                  icon={GitBranch}
                  title="选择一只标的开始缠论结构分析"
                  hint="搜索代码或名称, 查看包含处理后的分型、笔、线段、中枢与买卖点。"
                />
              </div>
            )}
            {loading && (
              <div className="absolute inset-0 grid place-items-center bg-base/60">
                <div className="flex items-center gap-2 text-sm text-muted">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在计算缠论结构…
                </div>
              </div>
            )}
            {errorText && (
              <div className="absolute inset-0 grid place-items-center bg-base/80 px-6">
                <div className="flex items-start gap-2 text-sm text-[#E8A33D] max-w-md">
                  <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                  <div>
                    <div className="font-medium">缠论结构接口不可用</div>
                    <div className="mt-1 text-xs text-muted break-words">{errorText}</div>
                    <div className="mt-1 text-xs text-muted">
                      请确认后端已加载 chan.analysis 扩展 (backend/app/custom/chan)。
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* 右栏: 摘要 + 中枢 + 买卖点 */}
          <div className="space-y-3">
            <div className="rounded-card border border-border bg-elevated/30 p-3 space-y-2">
              <div className="flex items-center gap-2 text-xs text-muted">
                <Target className="h-3.5 w-3.5" />
                结构摘要
              </div>
              {summary ? (
                <>
                  <div className="flex items-center gap-3">
                    <span className={`text-base font-semibold ${TREND_COLOR[summary.trend] ?? 'text-foreground'}`}>
                      {TREND_LABEL[summary.trend] ?? summary.trend}
                    </span>
                    <span className="text-[10px] font-mono text-muted">
                      {summary.status === 'ok' ? '口径正常' : summary.status}
                    </span>
                  </div>
                  <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                    <Stat label="K 线" value={`${summary.bar_count} 根`} />
                    <Stat label="分型" value={`${summary.fractal_count} 个`} />
                    <Stat label="笔" value={`${summary.stroke_count} 笔`} />
                    <Stat label="线段" value={`${summary.segment_count} 段`} />
                    <Stat label="中枢" value={`${summary.pivot_count} 个`} />
                    <Stat label="段中枢" value={`${summary.segment_pivot_count ?? 0} 个`} />
                    <Stat label="信号" value={`${summary.signal_count} 个`} />
                    <Stat label="口径" value={summary.price_mode} />
                    <Stat label="锚点" value={summary.adj_anchor_date ?? '—'} />
                  </dl>
                  {(summary.warnings ?? []).length > 0 && (
                    <div className="text-[10px] text-[#E8A33D] leading-relaxed">
                      {summary.warnings.map(w => <div key={w}>· {w}</div>)}
                    </div>
                  )}
                </>
              ) : (
                <div className="text-xs text-muted">—</div>
              )}
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="flex items-center gap-2 text-xs text-muted mb-2">
                <Layers className="h-3.5 w-3.5" />
                中枢 ({pivots.length})
              </div>
              {pivots.length === 0 ? (
                <div className="text-[11px] text-muted">未构成中枢</div>
              ) : (
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-muted">
                      <th className="text-left font-normal pb-1">区间</th>
                      <th className="text-right font-normal pb-1">ZG / ZD</th>
                      <th className="text-right font-normal pb-1">笔数</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {pivots.map((p, i) => (
                      <tr key={`${p.start_date}-${i}`} className="border-t border-border/60">
                        <td className="py-1 pr-2 whitespace-nowrap">{p.start_date.slice(5)} ~ {p.end_date.slice(5)}</td>
                        <td className="py-1 pl-2 text-right whitespace-nowrap">{fmtNum(p.zg)} / {fmtNum(p.zd)}</td>
                        <td className="py-1 pl-2 text-right">{p.stroke_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="flex items-center gap-2 text-xs text-muted mb-2">
                <Layers className="h-3.5 w-3.5" />
                段中枢 ({segmentPivots.length})
              </div>
              {segmentPivots.length === 0 ? (
                <div className="text-[11px] text-muted">未构成段中枢</div>
              ) : (
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-muted">
                      <th className="text-left font-normal pb-1">区间</th>
                      <th className="text-right font-normal pb-1">ZG / ZD</th>
                      <th className="text-right font-normal pb-1">段数</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {segmentPivots.map((p, i) => (
                      <tr key={`${p.start_date}-${i}`} className="border-t border-border/60">
                        <td className="py-1 pr-2 whitespace-nowrap">{p.start_date.slice(5)} ~ {p.end_date.slice(5)}</td>
                        <td className="py-1 pl-2 text-right whitespace-nowrap">{fmtNum(p.zg)} / {fmtNum(p.zd)}</td>
                        <td className="py-1 pl-2 text-right">{p.stroke_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="flex items-center gap-2 text-xs text-muted mb-2">
                <GitBranch className="h-3.5 w-3.5" />
                买卖点 ({signals.length})
              </div>
              {signals.length === 0 ? (
                <div className="text-[11px] text-muted">当前样本无买卖点</div>
              ) : (
                <ul className="space-y-1">
                  {signals.map((sig, i) => (
                    <li key={`${sig.kind}-${sig.date}-${i}`} className="text-[11px] flex items-baseline gap-2">
                      <span className={sig.kind.startsWith('b') ? 'text-[#C74040]' : 'text-[#2D9B65]'}>
                        {sig.label}
                      </span>
                      <span className="font-mono text-muted">{sig.date}</span>
                      <span className="font-mono text-muted">@{fmtNum(sig.price)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="flex items-center gap-2 text-xs text-muted mb-2">
                <TriangleAlert className="h-3.5 w-3.5" />
                背驰 ({divergences.length})
              </div>
              {divergences.length === 0 ? (
                <div className="text-[11px] text-muted">当前样本无背驰</div>
              ) : (
                <ul className="space-y-1">
                  {divergences.map((d, i) => (
                    <li key={`${d.date}-${i}`} className="text-[11px] flex items-baseline gap-2">
                      <span className={d.scope === 'trend' ? 'text-[#FACC15]' : 'text-muted'}>
                        {d.scope === 'trend' ? '趋势背驰' : '盘整背驰'}
                      </span>
                      <span className="font-mono text-muted">{d.date}</span>
                      <span className="font-mono text-muted">@{fmtNum(d.price)}</span>
                      <span className="font-mono text-muted text-[10px]">
                        {d.direction === 'down' ? '底' : '顶'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-2 text-[10px] text-muted leading-relaxed">
                盘整背驰只作结构标记, 不产生买卖点。
              </div>
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="flex items-center gap-2 text-xs text-muted mb-2">
                <TriangleAlert className="h-3.5 w-3.5" />
                段背驰 ({segmentDivergences.length})
              </div>
              {segmentDivergences.length === 0 ? (
                <div className="text-[11px] text-muted">当前样本无段背驰</div>
              ) : (
                <ul className="space-y-1">
                  {segmentDivergences.map((d, i) => (
                    <li key={`${d.date}-${i}`} className="text-[11px] flex items-baseline gap-2">
                      <span className={d.scope === 'trend' ? 'text-[#FB923C]' : 'text-muted'}>
                        {d.scope === 'trend' ? '段趋势背驰' : '段盘整背驰'}
                      </span>
                      <span className="font-mono text-muted">{d.date}</span>
                      <span className="font-mono text-muted">@{fmtNum(d.price)}</span>
                      <span className="font-mono text-muted text-[10px]">
                        {d.direction === 'down' ? '底' : '顶'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-2 text-[10px] text-muted leading-relaxed">
                段背驰与笔背驰同构, 比较单元是线段。
              </div>
            </div>

            <div className="rounded-card border border-border bg-elevated/30 p-3">
              <div className="text-xs text-muted mb-2">最近 5 笔</div>
              {strokes.length === 0 ? (
                <div className="text-[11px] text-muted">未构成笔</div>
              ) : (
                <table className="w-full text-[11px]">
                  <tbody className="font-mono">
                    {strokes.slice(-5).map((s, i) => (
                      <tr key={`${s.start_date}-${i}`} className="border-t border-border/60">
                        <td className="py-1 pr-2 whitespace-nowrap">{s.start_date.slice(5)} ~ {s.end_date.slice(5)}</td>
                        <td className={`py-1 pl-2 text-right ${s.direction === 'up' ? 'text-[#C74040]' : 'text-[#2D9B65]'}`}>
                          {s.direction === 'up' ? '涨' : '跌'}
                        </td>
                        <td className="py-1 pl-2 text-right text-muted">{s.k_count}K</td>
                        <td className="py-1 pl-2 text-right text-muted">力度 {fmtNum(s.strength, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-muted">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  )
}
