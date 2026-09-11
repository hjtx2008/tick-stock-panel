import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

/**
 * 沪深两市日成交额趋势 (单位: 亿元) — 主图大曲线版
 *
 * 数据取自指数日K的 amount 字段 (元), 换算为亿元:
 *   沪市 = 000001.SH 上证指数 (成分=全部沪市股票, amount 即沪市总成交额)
 *   深市 = 399106.SZ 深证综指 (成分=全部深市股票, amount 即深市总成交额)
 * 两者均非抽样指数, 故成交额可代表各自市场全量。
 */

export const MARKET_VOLUME = '__MARKET_VOLUME__'
const SYM_SH = '000001.SH'
const SYM_SZ = '399106.SZ'

export const COLOR_SH = '#F59E0B'   // 沪市 — 琥珀
export const COLOR_SZ = '#38BDF8'   // 深市 — 天蓝
export const COLOR_TOTAL = '#A78BFA' // 两市合计 — 紫罗兰 (中性色, 不与涨跌红绿混淆)

export interface VolumePoint {
  date: string
  sh: number | null
  sz: number | null
  /** 两市合计 (沪+深); 两侧均无数据为 null */
  total: number | null
}

function toAmountMap(rows: any[] | undefined): Map<string, number> {
  const m = new Map<string, number>()
  for (const r of rows ?? []) {
    const d = String(r?.date ?? '').slice(0, 10)
    const a = Number((r as any)?.amount ?? 0)
    if (!d || !Number.isFinite(a) || a <= 0) continue
    m.set(d, a / 1e8)
  }
  return m
}

/** 合并两市成交额为按日期对齐的序列 */
export function buildVolumePoints(shRows: any[] | undefined, szRows: any[] | undefined): VolumePoint[] {
  const shMap = toAmountMap(shRows)
  const szMap = toAmountMap(szRows)
  const dates = Array.from(new Set([...shMap.keys(), ...szMap.keys()])).sort()
  return dates.map(d => {
    const sh = shMap.get(d) ?? null
    const sz = szMap.get(d) ?? null
    // 两侧都有值时为真实合计; 仅单侧有值时退化为该侧值 (保证曲线连续, 非静默造数)
    const total = sh == null && sz == null ? null : (sh ?? 0) + (sz ?? 0)
    return { date: d, sh, sz, total }
  })
}

export function fmtYi(v: number | null | undefined, digits = 0) {
  if (v == null || !Number.isFinite(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export interface VolumeRange {
  days?: number
  start?: string
  end?: string
}

/** 两市成交额序列查询 (同 key 自动去重; days 版 / range 版分开缓存) */
export function useMarketVolume({ days = 120, start, end }: VolumeRange = {}, enabled = true) {
  const isRange = !!(start && end)
  return useQuery({
    queryKey: isRange
      ? ['market-volume-range', start, end]
      : QK.marketVolumeTrend(days ?? 120),
    queryFn: async () => {
      const [a, b] = isRange
        ? await Promise.all([
            api.indexDaily(SYM_SH, 250, { start: start!, end: end! }),
            api.indexDaily(SYM_SZ, 250, { start: start!, end: end! }),
          ])
        : await Promise.all([
            api.indexDaily(SYM_SH, days ?? 120),
            api.indexDaily(SYM_SZ, days ?? 120),
          ])
      return { sh: a, sz: b }
    },
    enabled,
    placeholderData: (prev: any) => prev,
  })
}

interface ChartProps {
  start: string
  end: string
  /** 整卡高度 (与K线区一致) */
  height?: number
}

/** 主图区大曲线: 沪深两市日成交额趋势 */
export function MarketVolumeChart({ start, end, height = 620 }: ChartProps) {
  const theme = useChartTheme()
  // 注意: 曲线容器在 loading/empty 态不渲染, 必须用 callback ref (state 化),
  // 否则空依赖 init effect 在容器出现后不会重跑 → ECharts 永不挂载 → 曲线空白
  const [boxEl, setBoxEl] = useState<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)

  const q = useMarketVolume({ start, end }, true)
  const points = useMemo(
    () => buildVolumePoints(q.data?.sh?.rows, q.data?.sz?.rows),
    [q.data],
  )

  const last = points[points.length - 1]
  const prev = points[points.length - 2]
  const deltaPct = (cur?: number | null, base?: number | null) => {
    if (cur == null || base == null || base === 0) return null
    return ((cur - base) / base) * 100
  }
  const shDelta = deltaPct(last?.sh, prev?.sh)
  const szDelta = deltaPct(last?.sz, prev?.sz)
  const totalDelta = deltaPct(last?.total, prev?.total)
  const total = last?.total ?? (last?.sh ?? 0) + (last?.sz ?? 0)

  const option = useMemo<EChartsOption>(() => {
    const dates = points.map(p => p.date)
    const shVals = points.map(p => p.sh)
    const szVals = points.map(p => p.sz)
    const totalVals = points.map(p => p.total)
    const tipLine = (color: string, name: string, v: number | null) =>
      `<div style="display:flex;align-items:center;gap:6px;margin-top:3px">
         <span style="width:7px;height:7px;border-radius:50%;background:${color}"></span>
         <span style="flex:1">${name}</span>
         <span style="font-variant-numeric:tabular-nums;font-weight:600">${fmtYi(v)} 亿</span>
       </div>`
    return {
      animation: false,
      grid: { left: 52, right: 16, top: 18, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: theme.tooltipBg,
        borderColor: theme.tooltipBorder,
        borderWidth: 1,
        padding: [8, 10],
        textStyle: { color: theme.tooltipText, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: theme.crosshair, width: 1 } },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const p = points[idx]
          if (!p) return ''
          const t = p.total ?? (p.sh ?? 0) + (p.sz ?? 0)
          return (
            `<div style="font-size:12px;min-width:150px"><div style="font-weight:600">${p.date}</div>` +
            tipLine(COLOR_SH, '沪市', p.sh) +
            tipLine(COLOR_SZ, '深市', p.sz) +
            `<div style="margin-top:5px;padding-top:4px;border-top:1px solid ${theme.border};
                         display:flex;align-items:center;gap:6px">
               <span style="width:7px;height:7px;border-radius:50%;background:${COLOR_TOTAL}"></span>
               <span style="flex:1">两市合计</span>
               <span style="font-variant-numeric:tabular-nums;font-weight:600">${fmtYi(t)} 亿</span>
             </div></div>`
          )
        },
      },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 14,
        itemHeight: 2,
        icon: 'rect',
        textStyle: { color: theme.text, fontSize: 11 },
        data: ['沪市', '深市', '两市合计'],
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: theme.border } },
        axisTick: { show: false },
        axisLabel: { color: theme.text, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        splitNumber: 5,
        axisLabel: {
          color: theme.text,
          fontSize: 10,
          formatter: (v: number) => v.toLocaleString('zh-CN'),
        },
        splitLine: { lineStyle: { color: theme.grid, type: 'dashed' } },
      },
      series: [
        {
          name: '沪市',
          type: 'line',
          data: shVals,
          smooth: true,
          symbol: 'none',
          connectNulls: true,
          lineStyle: { width: 2, color: COLOR_SH },
          itemStyle: { color: COLOR_SH },
          emphasis: { focus: 'series' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(245,158,11,0.30)' },
              { offset: 1, color: 'rgba(245,158,11,0.02)' },
            ]),
          },
        },
        {
          name: '深市',
          type: 'line',
          data: szVals,
          smooth: true,
          symbol: 'none',
          connectNulls: true,
          lineStyle: { width: 2, color: COLOR_SZ },
          itemStyle: { color: COLOR_SZ },
          emphasis: { focus: 'series' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(56,189,248,0.28)' },
              { offset: 1, color: 'rgba(56,189,248,0.02)' },
            ]),
          },
        },
        {
          // 两市合计 = 沪市 + 深市, 叠加在同一 Y 轴 (量纲相同: 亿元)
          // 不给面积填充, 避免与沪深两条面积叠加后糊成一团
          name: '两市合计',
          type: 'line',
          data: totalVals,
          smooth: true,
          symbol: 'none',
          connectNulls: true,
          z: 3,
          lineStyle: { width: 2.4, color: COLOR_TOTAL },
          itemStyle: { color: COLOR_TOTAL },
          emphasis: { focus: 'series' },
        },
      ],
    }
  }, [points, theme])

  // 初始化 + 绘制合并到同一 effect: 依赖 [boxEl, option]
  // 切走再切回时, 若 boxEl 与 option 均未变, 独立 effect 不会重跑 → 画布空白。
  // 这里每次都校验实例存活, 必要时重建, 并强制 resize 后重绘。
  useEffect(() => {
    const el = boxEl
    if (!el) return
    let chart = chartRef.current
    if (!chart || chart.isDisposed()) {
      // 清掉可能残留的实例标记: echarts.init 在 DOM 带 _echarts_instance_ 时
      // 会返回已 dispose 的死实例 (无 canvas), 必须显式清理
      const stale = echarts.getInstanceByDom(el)
      if (stale) stale.dispose()
      el.removeAttribute('_echarts_instance_')
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
    }
    // 切换瞬间容器可能刚完成布局, 先按实际尺寸重算再绘制
    chart.resize()
    chart.setOption(option, true)
    // 下一帧再兜底一次 (首帧尺寸为 0 时画不出内容)
    const raf = requestAnimationFrame(() => {
      const c = chartRef.current
      if (c && !c.isDisposed()) {
        c.resize()
        c.setOption(option, true)
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [boxEl, option])

  // 尺寸监听 (与绘制解耦, 避免实例重建时监听器丢失)
  useEffect(() => {
    const el = boxEl
    if (!el) return
    const ro = new ResizeObserver(() => {
      const c = chartRef.current
      if (c && !c.isDisposed()) c.resize()
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [boxEl])

  // 卸载/容器变化时销毁实例
  useEffect(() => {
    return () => {
      const c = chartRef.current
      if (c && !c.isDisposed()) c.dispose()
      chartRef.current = null
    }
  }, [boxEl])

  const loading = q.isLoading
  const empty = !loading && points.length === 0

  return (
    <div className="flex h-full flex-col" style={{ height }}>
      {/* 顶部信息条 */}
      <div className="mb-1 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-3">
          <span className="text-xs font-semibold text-foreground">
            沪深成交量<span className="ml-1.5 font-mono text-[10px] font-normal text-muted">亿元</span>
          </span>
          {loading && <span className="text-[11px] text-muted">加载中…</span>}
          {empty && <span className="text-[11px] text-muted">暂无数据</span>}
          {!loading && !empty && (
            <span className="font-mono text-[10px] text-muted">
              {points[0]?.date} ~ {last?.date} · {points.length} 个交易日
            </span>
          )}
        </div>
        {!loading && !empty && (
          <div className="flex items-center gap-3 text-[11px]">
            <span className="flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: COLOR_SH }} />
              <span className="text-secondary">沪市</span>
              <span className="font-mono tabular-nums font-medium text-foreground">{fmtYi(last?.sh)}</span>
              {shDelta != null && (
                <span className={`font-mono tabular-nums ${shDelta >= 0 ? 'text-bull' : 'text-bear'}`}>
                  {shDelta >= 0 ? '+' : ''}{shDelta.toFixed(1)}%
                </span>
              )}
            </span>
            <span className="flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: COLOR_SZ }} />
              <span className="text-secondary">深市</span>
              <span className="font-mono tabular-nums font-medium text-foreground">{fmtYi(last?.sz)}</span>
              {szDelta != null && (
                <span className={`font-mono tabular-nums ${szDelta >= 0 ? 'text-bull' : 'text-bear'}`}>
                  {szDelta >= 0 ? '+' : ''}{szDelta.toFixed(1)}%
                </span>
              )}
            </span>
            <span className="flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: COLOR_TOTAL }} />
              <span className="text-muted">合计</span>
              <span className="font-mono tabular-nums font-medium text-foreground">{fmtYi(total)}</span>
              {totalDelta != null && (
                <span className={`font-mono tabular-nums ${totalDelta >= 0 ? 'text-bull' : 'text-bear'}`}>
                  {totalDelta >= 0 ? '+' : ''}{totalDelta.toFixed(1)}%
                </span>
              )}
            </span>
          </div>
        )}
      </div>
      {/* 曲线区: 容器常驻渲染 (ref 必须在挂载时就绪, 否则 ECharts init 时序不稳),
          loading/empty 提示用绝对定位覆盖层 */}
      <div className="relative min-h-0 flex-1">
        <div ref={setBoxEl} className="h-full w-full" />
        {(loading || empty) && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface/40 text-xs text-muted">
            {loading ? '成交额数据加载中…' : '暂无成交额数据，请先同步指数日K'}
          </div>
        )}
      </div>
    </div>
  )
}
