import { useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, GitBranch, Loader2, Search } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { api, type ChanScreenMatch } from '@/lib/api'

const DAY_OPTIONS = [
  { value: 120, label: '120 日' },
  { value: 250, label: '250 日' },
  { value: 500, label: '500 日' },
]

const SIGNAL_KINDS = [
  { value: 'b1', label: '一买' },
  { value: 'b2', label: '二买' },
  { value: 'b3', label: '三买' },
  { value: 's1', label: '一卖' },
  { value: 's2', label: '二卖' },
  { value: 's3', label: '三卖' },
]

const TREND_OPTIONS = [
  { value: 'up', label: '上涨' },
  { value: 'down', label: '下跌' },
  { value: 'oscillation', label: '盘整' },
]

const SCOPE_OPTIONS = [
  { value: 'trend', label: '趋势背驰' },
  { value: 'consolidation', label: '盘整背驰' },
]

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

const SIGNAL_LABEL: Record<string, string> = {
  b1: '一买',
  b2: '二买',
  b3: '三买',
  s1: '一卖',
  s2: '二卖',
  s3: '三卖',
}

function fmtNum(value: number | undefined | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2 py-1 rounded-btn text-xs transition-colors ${
        active
          ? 'bg-sky-400/[0.12] text-sky-300 border border-sky-400/30'
          : 'text-secondary hover:bg-elevated border border-transparent'
      }`}
    >
      {children}
    </button>
  )
}

/**
 * 缠论选股器 (后端扩展 chan.analysis)。
 *
 * 结构口径与「缠论结构」页完全一致 (默认前复权, 锚点=样本末日) ——
 * 选出来的票点进详情页看到的结构与这里判定的完全一致。
 */
export function ChanScreenPage() {
  const [days, setDays] = useState(250)
  const [signalKinds, setSignalKinds] = useState<string[]>([])
  const [trend, setTrend] = useState<string[]>([])
  const [scope, setScope] = useState<string[]>([])
  const [requireDivergence, setRequireDivergence] = useState(false)
  const [requireSegmentDivergence, setRequireSegmentDivergence] = useState(false)
  const [requireSegmentPivot, setRequireSegmentPivot] = useState(false)
  const [maxSignalAge, setMaxSignalAge] = useState(0)
  const [symbolText, setSymbolText] = useState('')

  const payload = useMemo(
    () => ({
      days,
      limit: 200,
      signal_kinds: signalKinds,
      trend,
      divergence_scope: scope,
      require_divergence: requireDivergence,
      require_segment_divergence: requireSegmentDivergence,
      require_segment_pivot: requireSegmentPivot,
      max_signal_age: maxSignalAge,
      symbols: symbolText
        .split(/[\s,;]+/)
        .map(s => s.trim().toUpperCase())
        .filter(Boolean),
    }),
    [
      days,
      signalKinds,
      trend,
      scope,
      requireDivergence,
      requireSegmentDivergence,
      requireSegmentPivot,
      maxSignalAge,
      symbolText,
    ],
  )

  const scan = useMutation({
    mutationFn: () => api.chanScreen(payload),
  })

  const matches: ChanScreenMatch[] = scan.data?.matches ?? []
  const errorText = scan.error instanceof Error ? scan.error.message : null

  return (
    <>
      <PageHeader
        title="缠论选股"
        subtitle="按缠论结构筛选标的 · 前复权口径 · 与结构页结论一致"
        right={
          <div className="flex items-center gap-2 text-[10px] text-muted">
            <span>全市场扫描约 8 秒</span>
          </div>
        }
      />

      <div className="w-full px-5 py-4 space-y-4">
        {/* 条件栏 */}
        <div className="rounded-card border border-border bg-elevated/30 p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] text-muted w-16">样本</span>
            {DAY_OPTIONS.map(opt => (
              <Chip key={opt.value} active={days === opt.value} onClick={() => setDays(opt.value)}>
                {opt.label}
              </Chip>
            ))}
            <span className="text-[10px] text-muted w-16 ml-2">买卖点</span>
            {SIGNAL_KINDS.map(opt => (
              <Chip
                key={opt.value}
                active={signalKinds.includes(opt.value)}
                onClick={() => setSignalKinds(v => toggle(v, opt.value))}
              >
                {opt.label}
              </Chip>
            ))}
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] text-muted w-16">走势</span>
            {TREND_OPTIONS.map(opt => (
              <Chip
                key={opt.value}
                active={trend.includes(opt.value)}
                onClick={() => setTrend(v => toggle(v, opt.value))}
              >
                {opt.label}
              </Chip>
            ))}
            <span className="text-[10px] text-muted w-16 ml-2">背驰形态</span>
            {SCOPE_OPTIONS.map(opt => (
              <Chip
                key={opt.value}
                active={scope.includes(opt.value)}
                onClick={() => setScope(v => toggle(v, opt.value))}
              >
                {opt.label}
              </Chip>
            ))}
          </div>

          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[10px] text-muted w-16">其它条件</span>
            <label className="flex items-center gap-1 text-xs text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={requireDivergence}
                onChange={e => setRequireDivergence(e.target.checked)}
              />
              必须有笔背驰
            </label>
            <label className="flex items-center gap-1 text-xs text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={requireSegmentDivergence}
                onChange={e => setRequireSegmentDivergence(e.target.checked)}
              />
              必须有段背驰
            </label>
            <label className="flex items-center gap-1 text-xs text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={requireSegmentPivot}
                onChange={e => setRequireSegmentPivot(e.target.checked)}
              />
              必须有段中枢
            </label>
            <label className="flex items-center gap-1 text-xs text-secondary">
              信号新鲜度
              <input
                type="number"
                min={0}
                max={500}
                value={maxSignalAge}
                onChange={e => setMaxSignalAge(Math.max(0, Number(e.target.value) || 0))}
                className="w-16 bg-base border border-border rounded-btn px-1 py-0.5 text-xs font-mono"
              />
              根内 (0 = 不限)
            </label>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] text-muted w-16">股票池</span>
            <input
              type="text"
              value={symbolText}
              onChange={e => setSymbolText(e.target.value)}
              placeholder="留空 = 全市场; 或输入代码, 逗号/空格分隔"
              className="flex-1 min-w-[240px] bg-base border border-border rounded-btn px-2 py-1 text-xs font-mono"
            />
            <button
              type="button"
              onClick={() => scan.mutate()}
              disabled={scan.isPending}
              className="flex items-center gap-1 px-3 py-1 rounded-btn text-xs bg-sky-400/[0.12] text-sky-300 border border-sky-400/30 disabled:opacity-50"
            >
              {scan.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
              {scan.isPending ? '扫描中…' : '开始扫描'}
            </button>
          </div>
        </div>

        {/* 结果 */}
        <div className="rounded-card border border-border bg-elevated/30 p-3">
          <div className="flex items-center gap-2 text-xs text-muted mb-2">
            <GitBranch className="h-3.5 w-3.5" />
            命中 ({matches.length})
            {scan.data && (
              <span className="text-[10px] font-mono">
                扫描 {scan.data.scanned} 只 · 命中 {scan.data.total_matched} 只 · {scan.data.elapsed_ms} ms
                {scan.data.as_of ? ` · 数据截至 ${scan.data.as_of}` : ''}
              </span>
            )}
          </div>

          {errorText ? (
            <div className="flex items-start gap-2 text-sm text-[#E8A33D]">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <div className="text-xs break-words">{errorText}</div>
            </div>
          ) : scan.isPending ? (
            <div className="flex items-center gap-2 text-sm text-muted py-6">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在扫描全市场缠论结构…
            </div>
          ) : !scan.data ? (
            <EmptyState
              icon={Search}
              title="设定条件后点击「开始扫描」"
              hint="全市场扫描约 8 秒; 也可以填入代码缩小股票池。"
            />
          ) : matches.length === 0 ? (
            <div className="text-[11px] text-muted py-4">没有符合条件的标的</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-muted">
                    <th className="text-left font-normal pb-1">标的</th>
                    <th className="text-right font-normal pb-1">现价</th>
                    <th className="text-left font-normal pb-1">走势</th>
                    <th className="text-left font-normal pb-1">买卖点</th>
                    <th className="text-right font-normal pb-1">笔/段</th>
                    <th className="text-right font-normal pb-1">中枢/段中枢</th>
                    <th className="text-left font-normal pb-1">背驰</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {matches.map(m => (
                    <tr key={m.symbol} className="border-t border-border/60 hover:bg-elevated/50">
                      <td className="py-1 pr-2 whitespace-nowrap">
                        <Link
                          to={`/chan?symbol=${encodeURIComponent(m.symbol)}`}
                          className="text-sky-300 hover:underline"
                        >
                          {m.name || m.symbol}
                        </Link>
                        <span className="ml-2 text-muted">{m.symbol}</span>
                      </td>
                      <td className="py-1 pr-2 text-right">{fmtNum(m.last_close)}</td>
                      <td className={`py-1 pr-2 ${TREND_COLOR[m.trend] ?? ''}`}>
                        {TREND_LABEL[m.trend] ?? m.trend}
                      </td>
                      <td className="py-1 pr-2 whitespace-nowrap">
                        {m.signals.length === 0 ? (
                          <span className="text-muted">—</span>
                        ) : (
                          m.signals.slice(-6).map(s => (
                            <span
                              key={`${s.kind}-${s.date}`}
                              className={`mr-1 ${s.kind.startsWith('b') ? 'text-[#C74040]' : 'text-[#2D9B65]'}`}
                              title={`${s.date} @${fmtNum(s.price)}`}
                            >
                              {SIGNAL_LABEL[s.kind] ?? s.kind}
                            </span>
                          ))
                        )}
                      </td>
                      <td className="py-1 pr-2 text-right text-muted">
                        {m.stroke_count}/{m.segment_count}
                      </td>
                      <td className="py-1 pr-2 text-right text-muted">
                        {m.pivot_count}/{m.segment_pivot_count}
                      </td>
                      <td className="py-1 whitespace-nowrap">
                        {m.divergences.filter(d => d.diverged).length > 0 && (
                          <span className="text-[#FACC15] mr-1">笔背</span>
                        )}
                        {m.segment_divergences.filter(d => d.diverged).length > 0 && (
                          <span className="text-[#FB923C]">段背</span>
                        )}
                        {m.divergences.filter(d => d.diverged).length === 0 &&
                          m.segment_divergences.filter(d => d.diverged).length === 0 && (
                            <span className="text-muted">—</span>
                          )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
