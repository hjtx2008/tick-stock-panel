"""缠论 HTTP 接口 (M1)。

契约见 docs/chan-theory-spec.md §5.2。

三条纪律:
1. **薄层**: 这里只做参数校验、数据取数、结果序列化; 全部算法在 ``core.py``。
2. **复用既有数据读取**: 日 K 走 ``repository.get_daily_asset``, 复权因子走
   DuckDB 视图 ``adj_factor`` / ``adj_factor_etf`` —— 不另建一套 parquet 读取。
3. **fail-closed**: 未实现的级别返回 501, 数据不足返回 ``status=insufficient_data``,
   不返回空数组冒充「没有信号」。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.custom.chan.core import (
    ChanLevel,
    ChanParams,
    ChanStructure,
    analyze,
)
from app.custom.chan.screener import (
    ChanScreenConditions,
    ChanScreenQuery,
    market_window_dates,
    scan_market,
)
from app.extensions import ExtensionContext

# 由 startup(context) 注入 (扩展契约: setup() 阶段拿不到运行时上下文)
_CONTEXT: ExtensionContext | None = None

# 交易日 ≈ 日历日 * 1.45 (含周末); 再留一点缓冲
_CALENDAR_PER_TRADING_DAY = 1.5
_MIN_BARS = 30
_MAX_DAYS = 500

router = APIRouter(prefix="/api/chan", tags=["chan"])


def bind_context(context: ExtensionContext) -> None:
    """绑定运行时上下文 (startup 钩子调用)。"""
    global _CONTEXT
    _CONTEXT = context


def _repository():
    if _CONTEXT is None:
        raise HTTPException(status_code=503, detail="缠论扩展上下文尚未初始化")
    return _CONTEXT.repository


def _parse_anchor(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"adj_anchor_date 非法: {raw}") from exc


def _load_bars(repo, symbol: str, days: int) -> tuple[pl.DataFrame, str]:
    """取原始价日 K (raw_high/raw_low/raw_close 优先, 缺失时退回复权价列)。"""
    asset_type = repo.resolve_asset_type(symbol)
    # 样本窗口 = 全市场最近 days 个交易日 (与选股器 scan_market 同一窗口):
    # 停牌股在窗口内的 K 线会少于 days 根, 但结构与选股结论一致。
    window = market_window_dates(repo.store.data_dir / "kline_daily_enriched", days)
    if window:
        start, end = date.fromisoformat(window[0]), date.fromisoformat(window[-1])
    else:
        # 无股票交易日历 (仅有 ETF/指数数据等) → 退回按该标的自身最近 days 根
        end = repo.enriched_latest_date() or date.today()
        start = end - timedelta(days=int(days * _CALENDAR_PER_TRADING_DAY) + 30)

    wanted = ["date", "raw_high", "raw_low", "raw_close", "high", "low", "close", "volume"]
    df = repo.get_daily_asset(asset_type, symbol, start, end, columns=wanted)
    if df is None or df.is_empty():
        raise HTTPException(status_code=404, detail=f"没有 {symbol} 的日 K 数据")

    have = set(df.columns)
    if "raw_high" not in have or "raw_low" not in have or "raw_close" not in have:
        # ETF / 指数的 enriched 表可能没有 raw_* 列 → 用复权价当原始价
        if not {"high", "low", "close"} <= have:
            raise HTTPException(status_code=404, detail=f"{symbol} 缺少可用价格列")
        df = df.with_columns(
            [
                pl.col("high").alias("raw_high"),
                pl.col("low").alias("raw_low"),
                pl.col("close").alias("raw_close"),
            ]
        )
    # volume: 背驰的成交量力度 (czsc 口径); 缺列时 core 只按价差与长度判定并给出告警
    keep = ["date", "raw_high", "raw_low", "raw_close"] + (["volume"] if "volume" in have else [])
    df = df.select(keep).sort("date")
    # days 是**交易日数**: 日历窗口只是取数范围, 这里裁剪到最近 N 个交易日
    if df.height > days:
        df = df.tail(days)
    return df, asset_type


def _load_factors(repo, symbol: str, asset_type: str) -> pl.DataFrame:
    """取该标的的除权因子序列 (事件型 ex_factor, 非累积)。"""
    view = "adj_factor_etf" if asset_type == "etf" else "adj_factor"
    try:
        rows = repo.execute_all(
            f"SELECT trade_date, ex_factor FROM {view} WHERE symbol = ?",
            [symbol],
        )
    except Exception:
        return pl.DataFrame()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "trade_date": [r[0] for r in rows],
            "ex_factor": [float(r[1]) for r in rows],
        }
    )


def _fractal_payload(f) -> dict:
    return {
        "kind": f.kind,
        "date": f.date,
        "price": round(f.price, 6),
        "bar_index": f.bar_index,
        "confirmed_date": f.confirmed_date,
    }


def _stroke_payload(s) -> dict:
    return {
        "direction": s.direction,
        "start_date": s.start.date,
        "end_date": s.end.date,
        "start_price": round(s.start.price, 6),
        "end_price": round(s.end.price, 6),
        "k_count": s.k_count,
        "amplitude": round(s.amplitude, 6),
        "strength": round(s.strength, 6),
        "confirmed_date": s.confirmed_date,
    }


def _segment_payload(s) -> dict:
    return {
        "direction": s.direction,
        "start_date": s.start_date,
        "end_date": s.end_date,
        "start_price": round(s.start_price, 6),
        "end_price": round(s.end_price, 6),
        "stroke_count": s.stroke_count,
        "k_count": s.k_count,
        "amplitude": round(s.amplitude, 6),
        "strength": round(s.strength, 6),
        "confirmed_date": s.confirmed_date,
        "confirmed": s.confirmed,
    }


def _pivot_payload(p) -> dict:
    return {
        "direction": p.direction,
        "zg": round(p.zg, 6),
        "zd": round(p.zd, 6),
        "start_date": p.start_date,
        "end_date": p.end_date,
        "stroke_count": p.stroke_count,
        "nested_level": p.nested_level,
        "confirmed_date": p.end_date,
    }


def _segment_divergence_payload(d, segments) -> dict:
    """段背驰标记 (段级别)。

    与笔背驰同构, 但比较单元是线段 —— ``segment_index`` 是**线段下标**。
    """
    cur = segments[d.segment_index]
    return {
        "segment_index": d.segment_index,
        "prev_index": d.prev_index,
        "date": cur.end_date,
        "price": round(cur.end_price, 6),
        "direction": cur.direction,
        "diverged": d.diverged,
        "scope": d.scope,
        "detail": d.detail(segments),
    }


def _signal_payload(s) -> dict:
    return {
        "kind": s.kind,
        "label": s.label,
        "date": s.date,
        "price": round(s.price, 6),
        "bar_index": s.bar_index,
        "confirmed_date": s.confirmed_date,
        "detail": s.detail,
    }


def _divergence_payload(d, strokes) -> dict:
    """背驰标记。

    ``diverged=False`` 的候选也一并吐出 (带 ``diverged`` 标志), 便于前端
    区分「力度衰减但未满足完整判据」与「确认背驰」。
    """
    cur = strokes[d.stroke_index]
    return {
        "stroke_index": d.stroke_index,
        "prev_index": d.prev_index,
        "date": cur.end.date,
        "price": round(cur.end.price, 6),
        "direction": cur.direction,
        "diverged": d.diverged,
        "scope": d.scope,
        "detail": d.detail(strokes),
    }


def _serialize(structure: ChanStructure, include: set[str]) -> dict:
    payload: dict = {
        "symbol": structure.symbol,
        "price_mode": structure.price_mode,
        "adj_anchor_date": structure.adj_anchor_date,
        "level": structure.level.value,
        "as_of": structure.dates[-1] if structure.dates else None,
        "dates": structure.dates,
        "closes": [round(c, 6) for c in structure.closes],
        "raw_closes": [round(c, 6) for c in structure.raw_closes],
        "summary": structure.summary(),
        "warnings": structure.warnings,
    }
    if "structure" in include:
        payload["fractals"] = [_fractal_payload(f) for f in structure.fractals]
        payload["strokes"] = [_stroke_payload(s) for s in structure.strokes]
        payload["segments"] = [_segment_payload(s) for s in structure.segments]
        payload["pivots"] = [_pivot_payload(p) for p in structure.pivots]
        payload["segment_pivots"] = [
            _pivot_payload(p) for p in structure.segment_pivots
        ]
    if "signals" in include:
        payload["signals"] = [_signal_payload(s) for s in structure.signals]
    if "divergences" in include:
        payload["divergences"] = [
            _divergence_payload(d, structure.strokes) for d in structure.divergences
        ]
        payload["segment_divergences"] = [
            _segment_divergence_payload(d, structure.segments)
            for d in structure.segment_divergences
        ]
    return payload


@router.get("/structure")
def get_structure(
    symbol: str = Query(..., min_length=4, max_length=16),
    days: int = Query(250, ge=30, le=_MAX_DAYS),
    price_mode: str = Query("qfq"),
    adj_anchor_date: str | None = Query(None),
    level: str = Query("1d"),
    include: str = Query("structure,signals,divergences"),
) -> dict:
    """缠论结构分析。

    ``price_mode``: qfq(默认) / raw / hfq —— 复权口径, 响应回显实际锚点。
    ``level``: 目前只实现 1d, 其余级别返回 501 (fail-closed)。
    """
    if level != ChanLevel.DAY.value:
        raise HTTPException(
            status_code=501,
            detail=f"级别 {level} 尚未实现 (当前只支持 {ChanLevel.DAY.value})",
        )
    if price_mode not in ("qfq", "raw", "hfq"):
        raise HTTPException(status_code=400, detail=f"未知 price_mode: {price_mode}")

    repo = _repository()
    bars, asset_type = _load_bars(repo, symbol, days)
    factors = _load_factors(repo, symbol, asset_type)

    params = ChanParams(
        level=ChanLevel.DAY,
        price_mode=price_mode,  # type: ignore[arg-type]
        adj_anchor_date=_parse_anchor(adj_anchor_date),
    )
    try:
        structure = analyze(bars, params, factors=factors, symbol=symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    include_set = {part.strip() for part in include.split(",") if part.strip()}
    payload = _serialize(structure, include_set)
    payload["asset_type"] = asset_type
    if len(structure.dates) < _MIN_BARS:
        payload["summary"]["status"] = "insufficient_data"
    return payload


# ================================================================
# 选股器
# ================================================================


class ChanScreenRequest(BaseModel):
    """选股请求体 (条件之间是「与」关系, 留空 = 该维度不限)。"""

    days: int = Field(250, ge=30, le=_MAX_DAYS)
    symbols: list[str] | None = Field(None, description="股票池; 缺省 = 全市场")
    limit: int = Field(200, ge=1, le=1000)
    signal_kinds: list[str] = Field(default_factory=list)
    """命中的买卖点类型 (b1/b2/b3/s1/s2/s3)。"""
    trend: list[str] = Field(default_factory=list)
    """走势方向 (up/down/oscillation)。"""
    divergence_scope: list[str] = Field(default_factory=list)
    """笔背驰形态 (trend/consolidation)。"""
    require_divergence: bool = False
    require_segment_divergence: bool = False
    require_segment_pivot: bool = False
    max_signal_age: int = Field(0, ge=0, le=500)
    """买卖点距样本末日不超过 N 根 K 线; 0 = 不限。"""


@router.post("/screen")
def screen(request: ChanScreenRequest) -> dict:
    """缠论选股。

    结构口径与 ``/api/chan/structure`` 完全一致 (默认前复权, 锚点=样本末日)。
    全市场扫描耗时数秒 (内部按标的分块并行), 命中结果按「最近买卖点」排序。
    """
    repo = _repository()
    conditions = ChanScreenConditions(
        signal_kinds=tuple(request.signal_kinds),
        trend=tuple(request.trend),
        divergence_scope=tuple(request.divergence_scope),
        require_divergence=request.require_divergence,
        require_segment_divergence=request.require_segment_divergence,
        require_segment_pivot=request.require_segment_pivot,
        max_signal_age=request.max_signal_age,
    )
    query = ChanScreenQuery(
        days=request.days,
        symbols=tuple(request.symbols) if request.symbols else None,
        conditions=conditions,
        limit=request.limit,
    )
    try:
        conditions.validate()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = scan_market(repo, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    names = repo.get_name_map([m["symbol"] for m in result["matches"]])
    for m in result["matches"]:
        m["name"] = names.get(m["symbol"])
    return result
