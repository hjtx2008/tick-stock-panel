"""缠论算法内核 — 包含处理 / 分型 / 成笔 / 线段 / 中枢 / 走势 / 背驰买卖点。

设计依据: docs/chan-theory-spec.md (M0 阶段)。

三条不可动摇的原则:
1. **口径参数化**: 成笔条件、中枢构造、背驰力度全部通过 ``ChanParams`` 暴露,
   不在代码里写死流派差异; 未实现的参数组合必须 fail-closed, 不静默降级。
2. **原始价 + 复权因子序列**: 存储层永远稳定, 复权口径 (``price_mode`` +
   ``adj_anchor_date``) 是计算层参数; qfq 锚点固定后历史价格不随新除权事件漂移。
3. **确认滞后显式化**: 每个结构都带 ``confirmed_date``, 前 N 根 K 线算出的结构
   必须与前 N+M 根算出的前 N 根完全一致 (截断测试 T7.1)。

本模块只依赖 polars 与标准库, 不依赖 FastAPI / 仓库其它子系统, 可单测。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date as _date
from enum import StrEnum
from itertools import pairwise
from typing import Literal

import polars as pl

# ================================================================
# 级别 / 参数
# ================================================================


class ChanLevel(StrEnum):
    """缠论级别。取值与前端 K 线周期标签保持一致。"""

    DAY = "1d"
    M30 = "30m"
    M5 = "5m"
    M1 = "1m"


def sub_level_of(level: ChanLevel) -> ChanLevel | None:
    """返回日线之下的次级别; 已是最低级别时返回 None。

    预留的递归接口 (§4.8): 日线内的次级别分析走同一个 ``analyze`` 入口,
    只是喂入的 K 线粒度不同。
    """
    mapping = {
        ChanLevel.DAY: ChanLevel.M30,
        ChanLevel.M30: ChanLevel.M5,
        ChanLevel.M5: ChanLevel.M1,
        ChanLevel.M1: None,
    }
    return mapping.get(level)


@dataclass(frozen=True)
class ChanParams:
    """缠论计算参数 — 全部流派分歧点都在这里暴露。"""

    level: ChanLevel = ChanLevel.DAY

    # ── 复权口径 (§2.1) ──────────────────────────────────────────
    price_mode: Literal["raw", "qfq", "hfq"] = "qfq"
    adj_anchor_date: _date | None = None

    # ── 成笔条件 (§4.3) ───────────────────────────────────────────
    stroke_version: Literal["new", "old"] = "new"
    min_gap_bars: int = 1
    min_k_count: int = 5
    strict_no_shared_kline: bool = True

    # ── 中枢构造 (§4.5) ───────────────────────────────────────────
    min_strokes_per_pivot: int = 3
    min_segments_per_pivot: int = 3
    """段级中枢所需的连续段数 (缠论口径: 中枢由三个次级别走势类型重叠构成)。

    段级中枢 = 用**线段**而非笔构造的中枢 (``nested_level=1``);
    笔级中枢仍用 ``min_strokes_per_pivot``。
    """
    strict_overlap: bool = True
    overlap_mode: Literal["three_strokes"] = "three_strokes"
    pivot_level_mode: Literal["flat", "extended"] = "flat"

    # ── 背驰 (§4.7) ───────────────────────────────────────────────
    divergence_mode: Literal["czsc"] = "czsc"
    """背驰力度口径: czsc ``check_first_buy`` 公式 (5C, 2026-11-15 取代 D6 笔力度公式)。"""
    strength_scale: int = 100

    # ── 线段 (§4.4) ───────────────────────────────────────────────
    min_strokes_per_segment: int = 3
    # 特征序列分型的判定口径:
    #   "strict"  = 严格原文版: 中间元素的高点与低点**同时**最极端;
    #   "extreme" = 实务通行版: 只比较与线段同向的那个极值 (顶分型只比高点)。
    feature_fractal_mode: Literal["strict", "extreme"] = "extreme"

    # ── 分型细节 (§4.2) ───────────────────────────────────────────
    require_follow_through: bool = False

    def validate(self) -> None:
        """参数非法直接抛错, 不做静默兜底。"""
        if self.price_mode not in ("raw", "qfq", "hfq"):
            raise ValueError(f"未知 price_mode: {self.price_mode}")
        if self.stroke_version not in ("new", "old"):
            raise ValueError(f"未知 stroke_version: {self.stroke_version}")
        if self.overlap_mode != "three_strokes":
            raise ValueError(f"未实现的 overlap_mode: {self.overlap_mode}")
        if self.divergence_mode != "czsc":
            raise ValueError(f"未实现的 divergence_mode: {self.divergence_mode}")
        if self.pivot_level_mode not in ("flat", "extended"):
            raise ValueError(f"未知 pivot_level_mode: {self.pivot_level_mode}")
        if self.feature_fractal_mode not in ("strict", "extreme"):
            raise ValueError(f"未知 feature_fractal_mode: {self.feature_fractal_mode}")
        if self.min_gap_bars < 0:
            raise ValueError("min_gap_bars 不能为负")
        if self.min_k_count < 3:
            raise ValueError("min_k_count 至少为 3 (一个分型占 3 根 K)")
        if self.min_strokes_per_pivot < 3:
            raise ValueError("min_strokes_per_pivot 至少为 3 (中枢需三个次级别走势)")
        if self.min_segments_per_pivot < 3:
            raise ValueError("min_segments_per_pivot 至少为 3 (段中枢需三个次级别走势)")
        if self.min_strokes_per_segment < 3:
            raise ValueError("min_strokes_per_segment 至少为 3")
        if self.strength_scale <= 0:
            raise ValueError("strength_scale 必须为正")


# ================================================================
# 数据结构 (§5.1)
# ================================================================


@dataclass(frozen=True)
class MergedBar:
    """包含处理后的合并 K 线。"""

    high: float
    low: float
    start_index: int  # 参与合并的首根原始 K 线索引
    end_index: int    # 参与合并的末根原始 K 线索引
    high_index: int   # 最高点所在的原始 K 线索引
    low_index: int    # 最低点所在的原始 K 线索引
    high_date: str
    low_date: str
    end_date: str     # 合并区间末根原始 K 线的日期 (= 分型的确认日)


@dataclass(frozen=True)
class Fractal:
    """分型。"""

    kind: Literal["top", "bottom"]
    price: float          # 顶分型取 high, 底分型取 low
    date: str
    bar_index: int        # 极值所在的原始 K 线索引
    merged_index: int     # 极值所在的合并 K 线索引
    start_index: int      # 分型三 K 组合的首根原始 K 线索引
    end_index: int        # 分型三 K 组合的末根原始 K 线索引
    confirmed_date: str   # 分型被确认的日期 (三 K 组合走完的那根 K 线)
    # 分型中间合并 K 的高低点 —— 成笔时判断两分型区间是否互相包含 (czsc 口径)。
    # None = 调用方未提供 (如直接构造的测试分型), 此时跳过包含校验。
    high: float | None = None
    low: float | None = None

    @property
    def is_top(self) -> bool:
        return self.kind == "top"


@dataclass(frozen=True)
class Stroke:
    """笔。"""

    direction: Literal["up", "down"]
    start: Fractal
    end: Fractal
    k_count: int        # 笔跨越的 K 线根数 (交易日口径)
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY
    bar_count: int = 0
    """笔长度 (czsc ``BI.get_length``): 起点分型首根至终点分型末根的**合并 K** 根数。"""
    volume: float = 0.0
    """笔成交量 (czsc ``get_power_volume``): 两端分型中间合并 K 之间的原始成交量之和。
    0 = 无成交量数据, 背驰的成交量条件不成立。"""

    @property
    def amplitude(self) -> float:
        """笔幅度: 绝对价格差。"""
        return abs(self.end.price - self.start.price)

    @property
    def strength(self) -> float:
        """笔力度 := 笔幅度 / 笔天数 * 100。"""
        if self.k_count <= 0:
            return 0.0
        return self.amplitude / self.k_count * 100.0

    @property
    def start_date(self) -> str:
        return self.start.date

    @property
    def end_date(self) -> str:
        return self.end.date

    @property
    def start_price(self) -> float:
        return self.start.price

    @property
    def end_price(self) -> float:
        return self.end.price


@dataclass(frozen=True)
class Segment:
    """线段。"""

    direction: Literal["up", "down"]
    start_price: float
    end_price: float
    start_date: str
    end_date: str
    start_index: int   # 起始笔在 strokes 中的索引
    end_index: int     # 结束笔在 strokes 中的索引
    stroke_count: int
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY
    confirmed: bool = True
    """False = 末段尚未被破坏确认 (仍在延伸中)。

    spec §4.4 的边界是「末段未被破坏 → 不输出」; 但静默丢弃会让图上线段
    只画到全图中段, 看起来像「算法后半段失效」。这里改为**显式输出未确认末段**
    并置 ``confirmed=False``, 前端据此画虚线/淡色区分「已确认」与「延伸中」。
    未确认末段**不参与**任何买卖点判定 (build_signals 也不使用 segments)。
    """
    k_count: int = 0
    """线段跨越的 K 线根数 (交易日口径) —— 段级别背驰的长度口径用它。"""
    volume: float = 0.0
    """段成交量: 段起点至段终点原始 K 线的成交量之和 (0 = 无成交量数据)。"""

    @property
    def amplitude(self) -> float:
        """段幅度: 绝对价格差。"""
        return abs(self.end_price - self.start_price)

    @property
    def strength(self) -> float:
        """段力度 := 段幅度 / 段天数 * 100 (与笔力度同口径)。"""
        if self.k_count <= 0:
            return 0.0
        return self.amplitude / self.k_count * 100.0


@dataclass(frozen=True)
class Pivot:
    """中枢。"""

    direction: Literal["up", "down"]
    zg: float          # 中枢上沿
    zd: float          # 中枢下沿
    start_date: str
    end_date: str
    start_index: int   # 起始笔索引
    end_index: int     # 结束笔索引
    stroke_count: int
    level: ChanLevel = ChanLevel.DAY
    nested_level: int = 0


@dataclass(frozen=True)
class ChanSignal:
    """买卖点。"""

    kind: Literal["b1", "b2", "b3", "s1", "s2", "s3", "b1s", "s1s"]
    """b1s / s1s = 结构一买 / 结构一卖 (czsc 口径, 不要求趋势中枢), 与 b1 / s1 并列。"""
    label: str
    date: str
    price: float
    bar_index: int
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChanStructure:
    """缠论结构 — 对外的唯一产物。"""

    symbol: str
    price_mode: str
    adj_anchor_date: str | None
    level: ChanLevel
    dates: list[str]
    closes: list[float]           # 用于结构计算的价格序列 (复权口径后)
    raw_closes: list[float]       # 原始收盘价
    highs: list[float]
    lows: list[float]
    fractals: list[Fractal]
    strokes: list[Stroke]
    segments: list[Segment]
    pivots: list[Pivot]
    signals: list[ChanSignal]
    trend: str                    # "up" / "down" / "oscillation"
    status: str                   # "ok" / "insufficient_data"
    adj_factor_missing: bool = False
    warnings: list[str] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    """笔背驰判定结果 (全部候选, 含未成立者)。

    单独一个字段而不是塞进 ``signals``: 背驰是**几何事实**, 买卖点是
    **趋势语境下的结论** —— 盘整背驰不产生买卖点, 但必须能被看到、画出、
    供选股器使用 (spec §4.7 盘整背驰处置口径)。
    """
    segment_pivots: list[Pivot] = field(default_factory=list)
    """段级别中枢 (``nested_level=1``)。

    与 ``pivots`` (笔级中枢) 分开存放: 段级中枢的 ``start_index/end_index``
    是**线段下标**, 与笔级中枢不同坐标系, 混在一起会算错。
    """
    segment_divergences: list[SegmentDivergence] = field(default_factory=list)
    """段级别背驰判定结果 (全部候选, 含未成立者)。"""

    def summary(self) -> dict:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "price_mode": self.price_mode,
            "adj_anchor_date": self.adj_anchor_date,
            "level": self.level.value,
            "trend": self.trend,
            "bar_count": len(self.dates),
            "fractal_count": len(self.fractals),
            "stroke_count": len(self.strokes),
            "segment_count": len(self.segments),
            "pivot_count": len(self.pivots),
            "segment_pivot_count": len(self.segment_pivots),
            "signal_count": len(self.signals),
            "divergence_count": len(self.divergences),
            "confirmed_divergence_count": sum(1 for d in self.divergences if d.diverged),
            "segment_divergence_count": len(self.segment_divergences),
            "confirmed_segment_divergence_count": sum(
                1 for d in self.segment_divergences if d.diverged
            ),
            "adj_factor_missing": self.adj_factor_missing,
            "warnings": list(self.warnings),
        }


# ================================================================
# 复权口径 (§2.1)
# ================================================================


@dataclass(frozen=True)
class AdjustedSeries:
    """复权后的价格序列 + 口径回显。"""

    dates: list[str]
    high: list[float]
    low: list[float]
    close: list[float]
    raw_close: list[float]
    adj_anchor_date: str | None
    adj_factor_missing: bool
    ex_rights: list[bool]


def _as_date_str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, _date):
        return value.isoformat()
    return str(value)


def _to_date_series(series: pl.Series) -> pl.Series:
    """把日期列统一转成 Date (兼容 str / Date 两种输入, 不触发 polars 弃用告警)。"""
    if series.dtype == pl.Date:
        return series
    if series.dtype == pl.Datetime:
        return series.cast(pl.Date)
    return series.cast(pl.String, strict=False).str.to_date("%Y-%m-%d", strict=False)


def _factor_cum_series(factors: pl.DataFrame, dates: list[_date]) -> list[float]:
    """按日线日期取每根 K 线对应的累积复权因子 cum(D) = Π ex_factor(事件日 <= D)。

    ``factors`` 需含 ``trade_date`` / ``ex_factor`` 两列 (可含多只标的, 调用方负责
    先按 symbol 过滤)。累积在**全量因子表**上计算, 与样本区间无关 ——
    这是"库里数据永远稳定"的技术前提。
    """
    n = len(dates)
    if factors is None or factors.is_empty() or n == 0:
        return [1.0] * n
    if any(d is None for d in dates):
        return [1.0] * n
    f = pl.DataFrame(
        {
            "trade_date": _to_date_series(factors["trade_date"]),
            "ex_factor": factors["ex_factor"].cast(pl.Float64, strict=False),
        }
    ).drop_nulls()
    if f.is_empty():
        return [1.0] * n
    f = f.sort("trade_date").unique(subset=["trade_date"], keep="last")
    f = f.with_columns(pl.col("ex_factor").cum_prod().alias("cum_factor"))
    left = pl.DataFrame({"date": dates}).sort("date")
    joined = left.join_asof(
        f.select("trade_date", "cum_factor"),
        left_on="date",
        right_on="trade_date",
        strategy="backward",
    )
    return [float(v) for v in joined["cum_factor"].fill_null(1.0).to_list()]


def apply_price_mode(
    bars: pl.DataFrame,
    factors: pl.DataFrame | None,
    params: ChanParams,
) -> AdjustedSeries:
    """按 ``params.price_mode`` 把原始价换算成结构计算用的价格序列。

    - ``raw``: 直接用原始价;
    - ``qfq``: ``price * cum(D) / F``, F = cum(锚点日), 锚点默认样本末日;
    - ``hfq``: ``price * cum(D)``。

    锚点固定后, 追加新的除权事件**不会**改变锚点及之前的价格 (§2.1 / T8.4)。
    """
    params.validate()
    if bars is None or bars.is_empty():
        raise ValueError("bars 为空")

    cols = set(bars.columns)
    if "date" not in cols:
        raise ValueError("bars 缺少 date 列")

    # 原始价优先取 raw_* 列, 否则用 high/low/close 视作原始价
    def _pick(raw_name: str, fallback: str) -> str:
        return raw_name if raw_name in cols else fallback

    for base in ("high", "low", "close"):
        if base not in cols and f"raw_{base}" not in cols:
            raise ValueError(f"bars 缺少 {base} / raw_{base} 列")

    raw_high = bars[_pick("raw_high", "high")]
    raw_low = bars[_pick("raw_low", "low")]
    raw_close = bars[_pick("raw_close", "close")]

    dates_raw = [_as_date_str(v) for v in bars["date"].to_list()]
    dates = [_as_date_str(v) for v in bars["date"].to_list()]
    high_raw = [float(v) for v in raw_high.to_list()]
    low_raw = [float(v) for v in raw_low.to_list()]
    close_raw = [float(v) for v in raw_close.to_list()]

    n = len(dates_raw)
    warnings: list[str] = []
    adj_factor_missing = False

    if params.price_mode == "raw":
        return AdjustedSeries(
            dates=dates,
            high=high_raw,
            low=low_raw,
            close=close_raw,
            raw_close=close_raw,
            adj_anchor_date=None,
            adj_factor_missing=False,
            ex_rights=[False] * n,
        )

    if factors is None or factors.is_empty():
        # 无因子数据 → 退化为 raw, 但显式标记, 不静默
        warnings.append("adj_factor_missing: 无复权因子数据, 已退化为原始价")
        return AdjustedSeries(
            dates=dates,
            high=high_raw,
            low=low_raw,
            close=close_raw,
            raw_close=close_raw,
            adj_anchor_date=dates[-1],
            adj_factor_missing=True,
            ex_rights=[False] * n,
        )

    date_objs = _to_date_series(bars["date"]).to_list()
    cum = _factor_cum_series(factors, date_objs)

    # 锚点: 指定优先, 否则样本末日; 锚点不在样本内 → 回退样本末日并回显
    anchor_date = params.adj_anchor_date
    if anchor_date is None:
        anchor_idx = n - 1
    else:
        if isinstance(anchor_date, str):
            anchor_date = _date.fromisoformat(anchor_date)
        else:
            anchor_date = _date.fromisoformat(anchor_date.isoformat())
        matches = [i for i, d in enumerate(date_objs) if d == anchor_date]
        if matches:
            anchor_idx = matches[0]
        else:
            anchor_idx = n - 1
            warnings.append(
                f"adj_anchor_date 不在样本区间内, 已回退到样本末日 {dates[-1]}"
            )
    anchor_str = dates[anchor_idx]
    f_anchor = cum[anchor_idx]
    if f_anchor <= 0:
        warnings.append("锚点累积因子非正, 已退化为原始价")
        return AdjustedSeries(
            dates=dates,
            high=high_raw,
            low=low_raw,
            close=close_raw,
            raw_close=close_raw,
            adj_anchor_date=anchor_str,
            adj_factor_missing=True,
            ex_rights=[False] * n,
        )

    ratio = [c / f_anchor for c in cum] if params.price_mode == "qfq" else list(cum)

    high = [h * r for h, r in zip(high_raw, ratio, strict=True)]
    low = [low_v * r for low_v, r in zip(low_raw, ratio, strict=True)]
    close = [c * r for c, r in zip(close_raw, ratio, strict=True)]

    # 除权日标记: 因子表里的事件日
    ex_days = set(
        _as_date_str(v)
        for v in factors.select("trade_date").to_series().to_list()
        if v is not None
    )
    ex_rights = [d in ex_days for d in dates]

    return AdjustedSeries(
        dates=dates,
        high=high,
        low=low,
        close=close,
        raw_close=close_raw,
        adj_anchor_date=anchor_str,
        adj_factor_missing=adj_factor_missing,
        ex_rights=ex_rights,
    )


# ================================================================
# 包含处理 (§4.1)
# ================================================================


def merge_klines(highs: Sequence[float], lows: Sequence[float], dates: Sequence[str]) -> list[MergedBar]:
    """包含处理: 把相互包含的 K 线合并成一根。

    合并方向按缠论惯例: 上升方向取"高高", 下降方向取"低低"。

    方向口径对齐 czsc ``remove_include``: 每次都由**前两根合并 K** 的高点比较决定
    (后者高 → 向上, 后者低 → 向下); 两者高点相等时方向不明, 不做合并。
    因此前两根 K 线永不合并 (没有可供定向的前一根)。
    """
    if not (len(highs) == len(lows) == len(dates)):
        raise ValueError("highs/lows/dates 长度不一致")

    merged: list[MergedBar] = []

    for i in range(len(highs)):
        h = float(highs[i])
        low_v = float(lows[i])
        direction: str | None = None
        if len(merged) >= 2:
            k1, k2 = merged[-2], merged[-1]
            if k1.high < k2.high:
                direction = "up"
            elif k1.high > k2.high:
                direction = "down"
        if direction is None:
            merged.append(
                MergedBar(
                    high=h,
                    low=low_v,
                    start_index=i,
                    end_index=i,
                    high_index=i,
                    low_index=i,
                    high_date=dates[i],
                    low_date=dates[i],
                    end_date=dates[i],
                )
            )
            continue

        prev = merged[-1]
        contains = (h <= prev.high and low_v >= prev.low) or (
            h >= prev.high and low_v <= prev.low
        )
        if contains:
            if direction == "up":
                if h > prev.high:
                    new_high, new_high_index, new_high_date = h, i, dates[i]
                else:
                    new_high, new_high_index, new_high_date = (
                        prev.high,
                        prev.high_index,
                        prev.high_date,
                    )
                if low_v > prev.low:
                    new_low, new_low_index, new_low_date = low_v, i, dates[i]
                else:
                    new_low, new_low_index, new_low_date = (
                        prev.low,
                        prev.low_index,
                        prev.low_date,
                    )
            else:
                if h < prev.high:
                    new_high, new_high_index, new_high_date = h, i, dates[i]
                else:
                    new_high, new_high_index, new_high_date = (
                        prev.high,
                        prev.high_index,
                        prev.high_date,
                    )
                if low_v < prev.low:
                    new_low, new_low_index, new_low_date = low_v, i, dates[i]
                else:
                    new_low, new_low_index, new_low_date = (
                        prev.low,
                        prev.low_index,
                        prev.low_date,
                    )
            merged[-1] = MergedBar(
                high=new_high,
                low=new_low,
                start_index=prev.start_index,
                end_index=i,
                high_index=new_high_index,
                low_index=new_low_index,
                high_date=new_high_date,
                low_date=new_low_date,
                end_date=dates[i],
            )
        else:
            merged.append(
                MergedBar(
                    high=h,
                    low=low_v,
                    start_index=i,
                    end_index=i,
                    high_index=i,
                    low_index=i,
                    high_date=dates[i],
                    low_date=dates[i],
                    end_date=dates[i],
                )
            )

    return merged


# ================================================================
# 分型 (§4.2)
# ================================================================


def find_fractals(merged: Sequence[MergedBar], params: ChanParams) -> list[Fractal]:
    """在合并 K 线上找顶分型 / 底分型。

    严格定义: 中间 K 线的高点最高**且**低点也最高 → 顶分型 (底分型反之)。
    """
    fractals: list[Fractal] = []
    n = len(merged)
    for i in range(1, n - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        is_top = b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low
        is_bottom = b.low < a.low and b.low < c.low and b.high < a.high and b.high < c.high
        if is_top:
            fractals.append(
                Fractal(
                    kind="top",
                    price=b.high,
                    date=b.high_date,
                    bar_index=b.high_index,
                    merged_index=i,
                    start_index=a.start_index,
                    end_index=c.end_index,
                    confirmed_date=c.end_date,
                    high=b.high,
                    low=b.low,
                )
            )
        elif is_bottom:
            fractals.append(
                Fractal(
                    kind="bottom",
                    price=b.low,
                    date=b.low_date,
                    bar_index=b.low_index,
                    merged_index=i,
                    start_index=a.start_index,
                    end_index=c.end_index,
                    confirmed_date=c.end_date,
                    high=b.high,
                    low=b.low,
                )
            )
    if params.require_follow_through:
        fractals = _apply_follow_through(fractals, merged)
    return fractals


def _apply_follow_through(fractals: list[Fractal], merged: Sequence[MergedBar]) -> list[Fractal]:
    """顶分型需其后有下跌才成立 —— 可选过滤 (默认关闭)。"""
    kept: list[Fractal] = []
    for f in fractals:
        if f.kind == "top":
            idx = next(
                (i for i, m in enumerate(merged) if m.high_index == f.bar_index),
                None,
            )
            if idx is None or idx + 1 >= len(merged):
                continue
            if merged[idx + 1].low >= merged[idx].low:
                continue
        else:
            idx = next(
                (i for i, m in enumerate(merged) if m.low_index == f.bar_index),
                None,
            )
            if idx is None or idx + 1 >= len(merged):
                continue
            if merged[idx + 1].high <= merged[idx].high:
                continue
        kept.append(f)
    return kept


# ================================================================
# 成笔 (§4.3)
# ================================================================


def _stroke_gap_ok(
    a: Fractal,
    b: Fractal,
    params: ChanParams,
) -> bool:
    """判断两个异型分型能否成笔。"""
    delta = b.merged_index - a.merged_index
    if delta <= 0:
        return False

    # 价格方向 (czsc check_bi): 向上笔终点顶必须高于起点底, 向下笔反之。
    # 缺这一条时, 跳空下行中的反弹顶 (低于前底) 会被连成一笔"向下走的向上笔"。
    if a.kind == "bottom" and not b.price > a.price:
        return False
    if a.kind == "top" and not b.price < a.price:
        return False
    # 两分型区间互相包含 → 不成笔 (czsc check_bi 的 ab_include)
    if None not in (a.high, a.low, b.high, b.low):
        if (a.high > b.high and a.low < b.low) or (a.high < b.high and a.low > b.low):
            return False

    if params.stroke_version == "old":
        # 老笔: 两分型可共用一根 K 线 (距离 >= 2), 且原始 K 线跨度 >= 5
        if delta < 2:
            return False
    else:
        # 新笔: 至少 min_gap_bars 根独立 K 线夹在两极值之间
        if delta < params.min_gap_bars + 1:
            return False
        # 分型三 K 组合不得重叠 → 距离至少 3
        if params.strict_no_shared_kline and delta < 3:
            return False

    # 原始 K 线跨度下限 (一个分型占 3 根)
    span = b.bar_index - a.bar_index + 1
    return span >= params.min_k_count


def build_strokes(fractals: Sequence[Fractal], params: ChanParams) -> list[Stroke]:
    """由分型序列构成笔。

    规则:
    - 相邻笔必须异型 (顶→底 / 底→顶), 同型取更极端者;
    - 成笔还要满足 ``_stroke_gap_ok`` 的间隔条件;
    - 笔的 ``k_count`` = 首尾极值之间跨越的原始 K 线根数 (交易日口径)。
    """
    selected: list[Fractal] = []
    for f in fractals:
        if not selected:
            selected.append(f)
            continue
        last = selected[-1]
        if f.kind == last.kind:
            # 同型分型: 取更极端的
            if (f.kind == "top" and f.price > last.price) or (
                f.kind == "bottom" and f.price < last.price
            ):
                selected[-1] = f
            continue
        if _stroke_gap_ok(last, f, params):
            selected.append(f)
        # 间隔不够 → 跳过该分型, 等待后面的分型

    strokes: list[Stroke] = []
    for a, b in pairwise(selected):
        direction = "down" if a.kind == "top" else "up"
        strokes.append(
            Stroke(
                direction=direction,
                start=a,
                end=b,
                k_count=b.bar_index - a.bar_index + 1,
                confirmed_date=b.confirmed_date,
                level=params.level,
                # 起点分型首根 ~ 终点分型末根的合并 K 根数 (czsc BI.get_length)
                bar_count=b.merged_index - a.merged_index + 3,
            )
        )
    return strokes


# ================================================================
# 线段 (§4.4) — 特征序列法
# ================================================================


def _trailing_segment(
    strokes: Sequence[Stroke],
    start: int,
    seg_dir: Literal["up", "down"],
    params: ChanParams,
) -> Segment | None:
    """构造「延伸中」的未确认末段。

    起点取 ``strokes[start]`` 的起点, 终点取该段方向上的**极值点**(与已确认
    线段「终点 = 特征序列分型元素的极值」口径一致), 覆盖到最后一笔。

    返回 None 表示剩余笔数不足以成段 (少于 ``min_strokes_per_segment``)。
    """
    n = len(strokes)
    if start >= n:
        return None
    end_index = n - 1
    if end_index - start + 1 < params.min_strokes_per_segment:
        return None

    # 终点 = 段内该方向上的极值点
    if seg_dir == "up":
        best = max(range(start, n), key=lambda j: strokes[j].end.price)
        end_price = strokes[best].end.price
    else:
        best = min(range(start, n), key=lambda j: strokes[j].end.price)
        end_price = strokes[best].end.price

    return Segment(
        direction=seg_dir,
        start_price=strokes[start].start.price,
        end_price=end_price,
        start_date=strokes[start].start.date,
        end_date=strokes[best].end.date,
        start_index=start,
        end_index=end_index,
        stroke_count=end_index - start + 1,
        confirmed_date=strokes[end_index].confirmed_date,
        level=params.level,
        confirmed=False,
        k_count=strokes[best].end.bar_index - strokes[start].start.bar_index + 1,
    )


def build_segments(strokes: Sequence[Stroke], params: ChanParams) -> list[Segment]:
    """由笔构成线段 (特征序列分型 + 第一元素破坏确认)。

    上行线段: 特征序列取向下笔; 元素高点 = 该笔起点(顶)价, 低点 = 该笔终点(底)价。
    特征序列出现顶分型后, 需后续向上笔突破该顶分型**第一元素**的高点,
    线段才算结束 (缠论"特征序列分型被破坏"确认)。
    """
    segments: list[Segment] = []
    n = len(strokes)
    if n < params.min_strokes_per_segment:
        return segments

    i = 0
    while i < n - 1:
        seg_dir = strokes[i].direction
        # 特征序列元素: 与线段方向相反的笔
        elems = [j for j in range(i + 1, n) if strokes[j].direction != seg_dir]
        end_stroke: int | None = None
        for k in range(1, len(elems) - 1):
            a, b, c = elems[k - 1], elems[k], elems[k + 1]
            if seg_dir == "up":
                # 上行线段: 元素为向下笔, 顶分型比高点 (严格版还要比低点)
                formed = (
                    strokes[b].start.price > strokes[a].start.price
                    and strokes[b].start.price > strokes[c].start.price
                )
                if params.feature_fractal_mode == "strict":
                    formed = formed and (
                        strokes[b].end.price > strokes[a].end.price
                        and strokes[b].end.price > strokes[c].end.price
                    )
            else:
                # 下行线段: 元素为向上笔, 底分型比低点 (严格版还要比高点)
                formed = (
                    strokes[b].start.price < strokes[a].start.price
                    and strokes[b].start.price < strokes[c].start.price
                )
                if params.feature_fractal_mode == "strict":
                    formed = formed and (
                        strokes[b].end.price < strokes[a].end.price
                        and strokes[b].end.price < strokes[c].end.price
                    )
            if not formed:
                continue
            nxt = b + 1
            if nxt >= n:
                break
            # 确认: 特征序列分型的**第一元素**被后续笔破坏
            # (元素 a 的极值 = 该笔的起点价: 上行线段取顶, 下行线段取底)
            if seg_dir == "up":
                confirmed = strokes[nxt].end.price > strokes[a].start.price
            else:
                confirmed = strokes[nxt].end.price < strokes[a].start.price
            if confirmed:
                end_stroke = b
                # 成段要用到特征序列第三元素 c 与破坏笔 nxt, 取二者中较晚的确认日;
                # 用 b 自己的确认日会让线段 (及依赖它的段背驰) 提前"确认"。
                confirm_date = max(strokes[c].confirmed_date, strokes[nxt].confirmed_date)
                break

        if end_stroke is None:
            # 末段尚未被破坏确认 → 显式输出「延伸中」的未确认线段 (confirmed=False),
            # 而不是静默丢弃 (静默丢弃会让图上线段只画到全图中段, 看着像算法后半段失效)。
            trailing = _trailing_segment(strokes, i, seg_dir, params)
            if trailing is not None:
                segments.append(trailing)
            break
        stroke_count = end_stroke - i + 1
        if stroke_count < params.min_strokes_per_segment:
            # 线段笔数不足 → 该段不成立; 其后的笔同样处于「尚未成段」状态
            trailing = _trailing_segment(strokes, i, seg_dir, params)
            if trailing is not None:
                segments.append(trailing)
            break

        # 线段终点 = 特征序列分型元素的极值点:
        #   上行线段 → 向下笔的起点(顶); 下行线段 → 向上笔的起点(底)
        end_price = strokes[end_stroke].start.price
        end_date = strokes[end_stroke].start.date

        segments.append(
            Segment(
                direction=seg_dir,
                start_price=strokes[i].start.price,
                end_price=end_price,
                start_date=strokes[i].start.date,
                end_date=end_date,
                start_index=i,
                end_index=end_stroke,
                stroke_count=stroke_count,
                confirmed_date=confirm_date,
                level=params.level,
                k_count=strokes[end_stroke].start.bar_index - strokes[i].start.bar_index + 1,
            )
        )
        i = end_stroke

    return segments


# ================================================================
# 中枢 (§4.5)
# ================================================================


def _stroke_range(stroke: Stroke) -> tuple[float, float]:
    lo = min(stroke.start.price, stroke.end.price)
    hi = max(stroke.start.price, stroke.end.price)
    return lo, hi


def _segment_range(segment: Segment) -> tuple[float, float]:
    lo = min(segment.start_price, segment.end_price)
    hi = max(segment.start_price, segment.end_price)
    return lo, hi


@dataclass(frozen=True)
class _Range:
    """中枢构造的通用输入单元 —— 笔或线段都可以喂进来。

    中枢构造只关心「方向 + 价格区间 + 起止日期」, 与单元是笔还是段无关;
    笔级中枢 (``nested_level=0``) 与段级中枢 (``nested_level=1``) 共用同一套逻辑。
    """

    direction: Literal["up", "down"]
    lo: float
    hi: float
    start_date: str
    end_date: str


def _build_pivots_from_ranges(
    ranges: Sequence[_Range],
    params: ChanParams,
    min_units: int,
    nested_level: int,
) -> list[Pivot]:
    """由价格区间单元序列构造中枢 (笔级 / 段级共用)。

    划分口径对齐 czsc ``get_zs_seq``: 按顺序把每个单元归入当前中枢, 直到某个单元
    **完全离开**当前中枢区间 (向上单元的高点 < ZD, 或向下单元的低点 > ZG),
    就以该单元开始一个新中枢。突破中枢的那一笔 (起点仍在区间内) 归入原中枢,
    其后完全不回区间的那一笔才开启新中枢。

    - ZG / ZD = 中枢**前 ``min_units`` 个单元**的最低高点 / 最高低点
      (czsc ``ZS``: 前 3 笔); ``pivot_level_mode='extended'`` 时取全体单元的
      最高点 / 最低点 (区间随延伸扩张)。
    - 只输出单元数 >= ``min_units`` 且有效的中枢 (czsc ``ZS.is_valid``: ZG >= ZD,
      每个单元都触及区间); ``strict_overlap`` 要求 ZD < ZG 严格成立。
    - 方向 = 进入中枢的那一段 (中枢首单元之前的单元) 的方向, 首个中枢取首单元方向。
      趋势判定 (两个同向中枢不重叠) 依赖这一口径。
    """
    def _zone(group: list[int]) -> tuple[float, float]:
        head = [ranges[k] for k in group[:min_units]]
        return min(r.hi for r in head), max(r.lo for r in head)

    groups: list[list[int]] = []
    for idx, r in enumerate(ranges):
        if not groups:
            groups.append([idx])
            continue
        zg, zd = _zone(groups[-1])
        if (r.direction == "up" and r.hi < zd) or (r.direction == "down" and r.lo > zg):
            groups.append([idx])
        else:
            groups[-1].append(idx)

    pivots: list[Pivot] = []
    for group in groups:
        if len(group) < min_units:
            continue
        zg, zd = _zone(group)
        if zg < zd or (params.strict_overlap and not zd < zg):
            continue
        units = [ranges[k] for k in group]
        if not all(
            (zd <= u.hi <= zg) or (zd <= u.lo <= zg) or (u.hi >= zg and u.lo <= zd)
            for u in units
        ):
            continue
        if params.pivot_level_mode == "extended":
            zg = max(u.hi for u in units)
            zd = min(u.lo for u in units)
        start, end = group[0], group[-1]
        direction = ranges[start - 1].direction if start > 0 else ranges[start].direction
        pivots.append(
            Pivot(
                direction=direction,
                zg=zg,
                zd=zd,
                start_date=ranges[start].start_date,
                end_date=ranges[end].end_date,
                start_index=start,
                end_index=end,
                stroke_count=end - start + 1,
                level=params.level,
                nested_level=nested_level,
            )
        )
    return pivots


def build_pivots(strokes: Sequence[Stroke], params: ChanParams) -> list[Pivot]:
    """由笔构造中枢 (笔级别中枢, ``nested_level=0``)。

    中枢 = 连续 ``min_strokes_per_pivot`` 笔价格区间的重叠部分;
    ZG = 区间上沿, ZD = 区间下沿。重叠区间为空则不成中枢 (``strict_overlap``)。
    ``pivot_level_mode='flat'`` 时中枢区间在延伸过程中不扩张。
    """
    ranges = [
        _Range(
            direction=s.direction,
            lo=min(s.start.price, s.end.price),
            hi=max(s.start.price, s.end.price),
            start_date=s.start.date,
            end_date=s.end.date,
        )
        for s in strokes
    ]
    return _build_pivots_from_ranges(ranges, params, params.min_strokes_per_pivot, 0)


def build_segment_pivots(segments: Sequence[Segment], params: ChanParams) -> list[Pivot]:
    """由线段构造中枢 (段级别中枢, ``nested_level=1``)。

    与笔级中枢同一套构造逻辑, 只是输入单元换成线段:
    段中枢 = 连续 ``min_segments_per_pivot`` 段价格区间的重叠部分。

    ⚠️ **start_index / end_index 的含义随之改变** —— 在段级中枢里它们是
    **线段下标** (不是笔下标)。段级中枢只用于段级别背驰与段级别结构展示,
    不参与笔级买卖点判定。
    """
    ranges = [
        _Range(
            direction=s.direction,
            lo=min(s.start_price, s.end_price),
            hi=max(s.start_price, s.end_price),
            start_date=s.start_date,
            end_date=s.end_date,
        )
        for s in segments
    ]
    return _build_pivots_from_ranges(ranges, params, params.min_segments_per_pivot, 1)


# ================================================================
# 走势类型 (§4.6)
# ================================================================


def same_direction_trend(pivots: Sequence[Pivot], direction: str) -> bool:
    """同向中枢是否构成趋势 (取**最后两个**同向中枢)。

    下跌趋势: 后一个下跌中枢的上沿低于前一个的下沿 (ZG2 < ZD1);
    上涨趋势: 后一个上涨中枢的下沿高于前一个的上沿 (ZD2 > ZG1)。

    ⚠️ 必须取「最后两个」而不是「最前两个」: 判据要描述**当前**结构,
    若用最前两个, 一只先涨后跌的票会永远保留开头的上涨结构,
    导致买点被误杀、卖点被放宽 (实测 400 只样本: 一买 37→46, 一卖 28→18)。
    """
    ps = [p for p in pivots if p.direction == direction]
    if len(ps) < 2:
        return False
    prev, last = ps[-2], ps[-1]
    if direction == "down":
        return last.zg < prev.zd
    return last.zd > prev.zg


def classify_trend(pivots: Sequence[Pivot], strokes: Sequence[Stroke]) -> str:
    """趋势 / 盘整判定。

    趋势: **最后两个**同向中枢不重叠 (向上 ZD2 > ZG1, 向下 ZG2 < ZD1)。
    取「最后两个」而非「最前两个」: 判据要描述**当前**结构;
    若用最前两个, 先涨后跌的票会永远保留开头的上涨结构,
    导致买点被误杀、卖点被放宽 (实测 400 只样本: 一买 37→46, 一卖 28→18)。

    方向优先级: 先看最近一个中枢的方向, 不成立再看另一方向。
    """
    if len(pivots) < 2:
        return "oscillation"
    last_dir = pivots[-1].direction
    if same_direction_trend(pivots, last_dir):
        return last_dir
    other = "up" if last_dir == "down" else "down"
    if same_direction_trend(pivots, other):
        return other
    return "oscillation"


# ================================================================
# 背驰与买卖点 (§4.7)
# ================================================================


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _beyond(cur_end: float, prev_end: float, direction: str) -> bool:
    """cur 是否比 prev 创出新极值 (向下笔创新低 / 向上笔创新高)。"""
    return cur_end < prev_end if direction == "down" else cur_end > prev_end


def _key_chain(units: Sequence, i: int, end_price) -> list:
    """第 i 个单元之前、同向且连续创新极值的单元链 (含前一同向单元), czsc 的「关键笔」。

    czsc ``check_first_buy`` 在固定窗口里取「首笔 + 之后每个创新低的同向笔」;
    逐笔检测没有窗口, 这里取 i-2 往前连续创新极值的那一串作为等价的关键笔集合。
    """
    direction = units[i].direction
    chain = [units[i - 2]]
    k = i - 2
    while (
        k - 2 >= 0
        and units[k - 2].direction == direction
        and _beyond(end_price(units[k]), end_price(units[k - 2]), direction)
    ):
        chain.append(units[k - 2])
        k -= 2
    return chain


def _czsc_weaker(cur: float, prev: float, keys: Sequence[float]) -> bool:
    """czsc 力度衰减判据: 当前值 < max(前一同向单元, 关键单元均值)。"""
    return cur < max(prev, _mean(keys))


@dataclass(frozen=True)
class Divergence:
    """笔背驰判定结果 (czsc ``check_first_buy`` 力度口径)。"""

    stroke_index: int
    prev_index: int
    bi_ok: bool
    """该笔成立且相对前一同向笔创出新极值 (向下创新低 / 向上创新高); 不创新极值无背驰可言。"""
    price_ok: bool
    """价差力度衰减: 笔幅度 < max(前一同向笔幅度, 关键笔幅度均值)。"""
    volume_ok: bool
    """成交量衰减: 笔成交量 < max(前一同向笔, 关键笔均值); 无成交量数据时恒为 False。"""
    length_ok: bool
    """长度缩短: 笔长度 (合并 K 根数) < max(前一同向笔, 关键笔均值)。"""
    scope: Literal["trend", "consolidation"] = "trend"
    """背驰所处形态: ``trend`` = 趋势背驰, ``consolidation`` = 盘整背驰。

    盘整背驰 (TDX 标记 17) 的处置口径 (spec §4.7): **不产生买卖点**,
    只作为结构标记展示 —— 中枢震荡里的一笔背驰没有趋势方向可言,
    拿它当买点会凭空造出「下跌趋势中的底背驰」。
    """

    @property
    def diverged(self) -> bool:
        return self.bi_ok and self.price_ok and (self.volume_ok or self.length_ok)

    def detail(self, strokes: Sequence[Stroke]) -> dict:
        cur = strokes[self.stroke_index]
        prev = strokes[self.prev_index]
        return {
            "cur_stroke_index": self.stroke_index,
            "prev_stroke_index": self.prev_index,
            "cur_amplitude": round(cur.amplitude, 6),
            "prev_amplitude": round(prev.amplitude, 6),
            "cur_volume": round(cur.volume, 2),
            "prev_volume": round(prev.volume, 2),
            "cur_length": cur.bar_count,
            "prev_length": prev.bar_count,
            "cur_days": cur.k_count,
            "prev_days": prev.k_count,
            "bi_ok": self.bi_ok,
            "price_ok": self.price_ok,
            "volume_ok": self.volume_ok,
            "length_ok": self.length_ok,
            "scope": self.scope,
        }


def detect_bi_divergence(
    strokes: Sequence[Stroke],
    params: ChanParams,
) -> list[Divergence]:
    """逐笔检测背驰 (5C: 采用 czsc ``check_first_buy`` 力度公式, 取代 D6)::

        背驰 := 创新极值
                AND 价差 < max(前笔价差, 关键笔价差均值)
                AND ( 成交量 < max(前笔, 关键笔均值)  OR  长度 < max(前笔, 关键笔均值) )

    ``前笔`` = 上一笔**同方向**的笔; ``关键笔`` 见 ``_key_chain``。
    价差 = 笔幅度 (绝对价格差); 长度 = 合并 K 根数; 成交量 = 笔内原始成交量之和。
    """
    out: list[Divergence] = []
    for i in range(2, len(strokes)):
        cur, prev = strokes[i], strokes[i - 2]
        if prev.direction != cur.direction:
            continue
        keys = _key_chain(strokes, i, lambda u: u.end.price)
        out.append(
            Divergence(
                stroke_index=i,
                prev_index=i - 2,
                bi_ok=_beyond(cur.end.price, prev.end.price, cur.direction),
                price_ok=_czsc_weaker(cur.amplitude, prev.amplitude, [k.amplitude for k in keys]),
                volume_ok=_czsc_weaker(cur.volume, prev.volume, [k.volume for k in keys]),
                length_ok=_czsc_weaker(cur.bar_count, prev.bar_count, [k.bar_count for k in keys]),
            )
        )
    return out


def classify_divergence_scope(
    strokes: Sequence[Stroke],
    pivots: Sequence[Pivot],
    divergences: Sequence[Divergence],
) -> list[Divergence]:
    """给每个背驰标注形态归属: ``trend`` (趋势背驰) / ``consolidation`` (盘整背驰)。

    判据 (spec §4.7 盘整背驰处置口径):

    - 取**在该笔之前已经完成**的同向中枢;
    - 若最后两个同向中枢构成该方向的趋势 (向下: 后者 ZG < 前者 ZD;
      向上: 后者 ZD > 前者 ZG) → 该笔背驰是**趋势背驰**;
    - 否则 (中枢不足两个, 或同向中枢彼此重叠 → 盘整) → **盘整背驰**。

    盘整背驰**不产生买卖点** —— 盘整里的一笔力度衰减没有方向含义,
    把它当一买会凭空造出并不存在的"下跌趋势中的底背驰"。
    """
    out: list[Divergence] = []
    for d in divergences:
        cur = strokes[d.stroke_index]
        direction = cur.direction
        same_dir = [p for p in pivots if p.direction == direction and p.end_index < d.stroke_index]
        scope = "consolidation"
        if len(same_dir) >= 2:
            prev_p, last_p = same_dir[-2], same_dir[-1]
            trending = last_p.zg < prev_p.zd if direction == "down" else last_p.zd > prev_p.zg
            if trending:
                scope = "trend"
        out.append(replace(d, scope=scope))
    return out


@dataclass(frozen=True)
class SegmentDivergence:
    """段背驰判定结果 (段级别)。

    与笔背驰 (``Divergence``) 判定式同构, 比较单元换成**线段**;
    段长度用原始 K 线根数 (``k_count``), 段成交量为段内原始成交量之和。
    """

    segment_index: int
    prev_index: int
    duan_ok: bool
    """该段成立且相对前一同向段创出新极值。"""
    price_ok: bool
    volume_ok: bool
    length_ok: bool
    scope: Literal["trend", "consolidation"] = "trend"
    """段背驰的形态归属, 判据同笔级 (见 ``classify_divergence_scope``)。"""

    @property
    def diverged(self) -> bool:
        return self.duan_ok and self.price_ok and (self.volume_ok or self.length_ok)

    def detail(self, segments: Sequence[Segment]) -> dict:
        cur = segments[self.segment_index]
        prev = segments[self.prev_index]
        return {
            "cur_segment_index": self.segment_index,
            "prev_segment_index": self.prev_index,
            "cur_amplitude": round(cur.amplitude, 6),
            "prev_amplitude": round(prev.amplitude, 6),
            "cur_volume": round(cur.volume, 2),
            "prev_volume": round(prev.volume, 2),
            "cur_days": cur.k_count,
            "prev_days": prev.k_count,
            "duan_ok": self.duan_ok,
            "price_ok": self.price_ok,
            "volume_ok": self.volume_ok,
            "length_ok": self.length_ok,
            "scope": self.scope,
        }


def detect_segment_divergence(
    segments: Sequence[Segment],
    params: ChanParams,
) -> list[SegmentDivergence]:
    """逐段检测背驰 (段级别), 判定式与笔级同构 (见 ``detect_bi_divergence``)。

    ``前段`` = 上一段**同方向**的线段。未确认的末段 (``confirmed=False``) 仍在延伸,
    幅度/长度都可能继续变化, 因此**不参与**段背驰判定 —— 否则每次刷新都会抖出新的段背驰。
    """
    out: list[SegmentDivergence] = []
    for i in range(2, len(segments)):
        cur, prev = segments[i], segments[i - 2]
        if not cur.confirmed or not prev.confirmed or prev.direction != cur.direction:
            continue
        keys = [k for k in _key_chain(segments, i, lambda u: u.end_price) if k.confirmed]
        out.append(
            SegmentDivergence(
                segment_index=i,
                prev_index=i - 2,
                duan_ok=_beyond(cur.end_price, prev.end_price, cur.direction),
                price_ok=_czsc_weaker(cur.amplitude, prev.amplitude, [k.amplitude for k in keys]),
                volume_ok=_czsc_weaker(cur.volume, prev.volume, [k.volume for k in keys]),
                length_ok=_czsc_weaker(cur.k_count, prev.k_count, [k.k_count for k in keys]),
            )
        )
    return out


def classify_segment_divergence_scope(
    segments: Sequence[Segment],
    segment_pivots: Sequence[Pivot],
    divergences: Sequence[SegmentDivergence],
) -> list[SegmentDivergence]:
    """给每个段背驰标注形态归属 (趋势 / 盘整), 判据同笔级。

    段级中枢 (``nested_level=1``) 的 ``start_index/end_index`` 是**线段下标**,
    与段背驰的 ``segment_index`` 同一坐标系, 可直接比较。
    """
    out: list[SegmentDivergence] = []
    for d in divergences:
        cur = segments[d.segment_index]
        direction = cur.direction
        same_dir = [
            p for p in segment_pivots if p.direction == direction and p.end_index < d.segment_index
        ]
        scope = "consolidation"
        if len(same_dir) >= 2:
            prev_p, last_p = same_dir[-2], same_dir[-1]
            trending = last_p.zg < prev_p.zd if direction == "down" else last_p.zd > prev_p.zg
            if trending:
                scope = "trend"
        out.append(replace(d, scope=scope))
    return out


def _pivot_direction_after(pivots: Sequence[Pivot], stroke_index: int) -> str | None:
    """返回包含该笔的最新中枢方向。"""
    for p in pivots:
        if p.start_index <= stroke_index <= p.end_index:
            return p.direction
    return None


_FIRST_POINT_WINDOWS = (21, 19, 17, 15, 13, 11, 9, 7, 5)
_THIRD_POINT_REACH = 1.618


def _stroke_high(s: Stroke) -> float:
    return max(s.start.price, s.end.price)


def _stroke_low(s: Stroke) -> float:
    return min(s.start.price, s.end.price)


def _check_first_point(bis: Sequence[Stroke], last_direction: str) -> bool:
    """czsc ``check_first_buy`` / ``check_first_sell`` 的结构判定 (一买以向下笔收尾)。

    - 奇数笔, 首末同向且与 ``last_direction`` 一致;
    - 一买: 首笔高点是窗口最高、末笔低点是窗口最低 (一卖镜像);
    - 关键笔 = 首笔 + 之后每个比前一同向笔创新低 (新高) 的同向笔;
    - 价差衰减 AND (成交量衰减 OR 长度缩短), 比较基准 = max(倒数第三笔, 关键笔均值)。
    """
    if len(bis) % 2 != 1 or bis[0].direction != last_direction or bis[-1].direction != last_direction:
        return False
    down = last_direction == "down"
    highs = [_stroke_high(b) for b in bis]
    lows = [_stroke_low(b) for b in bis]
    if down and (max(highs) != highs[0] or min(lows) != lows[-1]):
        return False
    if not down and (min(lows) != lows[0] or max(highs) != highs[-1]):
        return False
    keys = [bis[0]]
    for k in range(2, len(bis) - 2, 2):
        prev_extreme = _stroke_low(bis[k - 2]) if down else _stroke_high(bis[k - 2])
        extreme = _stroke_low(bis[k]) if down else _stroke_high(bis[k])
        if (extreme < prev_extreme) if down else (extreme > prev_extreme):
            keys.append(bis[k])
    last, prev = bis[-1], bis[-3]
    price_ok = _czsc_weaker(last.amplitude, prev.amplitude, [k.amplitude for k in keys])
    volume_ok = _czsc_weaker(last.volume, prev.volume, [k.volume for k in keys])
    length_ok = _czsc_weaker(last.bar_count, prev.bar_count, [k.bar_count for k in keys])
    return price_ok and (volume_ok or length_ok)


def _third_point_within_reach(
    strokes: Sequence[Stroke], start: int, pull: int, breakout: str
) -> bool:
    """7B: 三买回抽不能离中枢太远 (czsc ``cxt_third_buy_V230228`` 的 tb_price 条件)。

    三买: 回抽低点 < 区间最低点 + 1.618 × 关键向上笔幅度均值;
    关键向上笔 = 中枢起点以来首个向上笔 + 之后每个创新高的向上笔。三卖镜像。
    区间 = 中枢首笔至回抽笔。
    """
    window = strokes[start : pull + 1]
    keys: list[Stroke] = []
    for st in window[:-1]:
        if st.direction != breakout:
            continue
        if not keys or (
            _stroke_high(st) > _stroke_high(keys[-1])
            if breakout == "up"
            else _stroke_low(st) < _stroke_low(keys[-1])
        ):
            keys.append(st)
    if not keys:
        return False
    reach = _THIRD_POINT_REACH * _mean([k.amplitude for k in keys])
    if breakout == "up":
        return strokes[pull].end.price < min(_stroke_low(st) for st in window) + reach
    return strokes[pull].end.price > max(_stroke_high(st) for st in window) - reach


def build_signals(
    strokes: Sequence[Stroke],
    pivots: Sequence[Pivot],
    segments: Sequence[Segment],
    divergences: Sequence[Divergence],
    params: ChanParams,
) -> list[ChanSignal]:
    """构造一/二/三类买卖点。

    规则 (与 §4.7 一致, 全部基于笔级几何, 不依赖 MACD 等指标列):

    - **一类买点**: 下跌趋势 (最后两个下跌中枢依次降低) 中, 最后一笔
      出现底背驰 (笔背离成立) → 该笔终点为一买;
    - **二类买点**: 一买之后的回抽笔不跌破一买低点 → 二买
      (回抽笔自身背驰只作「加强」标注, 不是必要条件);
    - **三类买点**: 向上离开中枢 (笔终点突破中枢上沿 ZG) 后, 回抽笔的低点
      不回中枢 (低点 > ZG) → 三买;
    - 卖点取镜像规则。
    """
    signals: list[ChanSignal] = []
    n = len(strokes)
    if n == 0:
        return signals

    # 背驰形态归属在此统一判定, 调用方不必预先分类
    divergences = classify_divergence_scope(strokes, pivots, divergences)
    div_by_index = {d.stroke_index: d for d in divergences}

    # ── 一买 / 一卖 ────────────────────────────────────────────
    for d in divergences:
        if not d.diverged:
            continue
        # 盘整背驰只作结构标记, 不产生买卖点 (spec §4.7 盘整背驰处置口径)。
        # scope == "trend" 已按「该笔之前完成的最后两个同向中枢」判定趋势;
        # 这里不能再用全体中枢 (含背驰笔之后才形成的中枢), 否则后续行情会改写历史一买/一卖。
        if d.scope != "trend":
            continue
        cur = strokes[d.stroke_index]
        prior_same_dir = [
            p for p in pivots if p.direction == cur.direction and p.end_index < d.stroke_index
        ]
        if not prior_same_dir:
            continue
        if cur.direction == "down":
            # 一买: 下跌趋势中的底背驰 (取趋势内最后一笔的背驰)
            last_down_pivot = prior_same_dir[-1]
            if cur.start.date < last_down_pivot.end_date:
                continue
            signals.append(
                ChanSignal(
                    kind="b1",
                    label="一买",
                    date=cur.end.date,
                    price=cur.end.price,
                    bar_index=cur.end.bar_index,
                    confirmed_date=cur.confirmed_date,
                    level=params.level,
                    detail={**d.detail(strokes), "rule": "下跌趋势底背驰"},
                )
            )
        else:
            last_up_pivot = prior_same_dir[-1]
            if cur.start.date < last_up_pivot.end_date:
                continue
            signals.append(
                ChanSignal(
                    kind="s1",
                    label="一卖",
                    date=cur.end.date,
                    price=cur.end.price,
                    bar_index=cur.end.bar_index,
                    confirmed_date=cur.confirmed_date,
                    level=params.level,
                    detail={**d.detail(strokes), "rule": "上涨趋势顶背驰"},
                )
            )

    # ── 二买 / 二卖 ────────────────────────────────────────────
    b1s = [s for s in signals if s.kind == "b1"]
    for b1 in b1s:
        b1_stroke = next(
            (i for i, s in enumerate(strokes) if s.end.date == b1.date and s.direction == "down"),
            None,
        )
        if b1_stroke is None:
            continue
        pullback = b1_stroke + 2  # 一买后的回抽笔
        if pullback >= n or strokes[pullback].direction != "down":
            continue
        if strokes[pullback].end.price <= strokes[b1_stroke].end.price:
            continue  # 跌破一买低点 → 不是二买
        div = div_by_index.get(pullback)
        # 缠论原文的「二买」= 一买之后的第一次回调**不破一买低点**;
        # 回抽笔自身是否背驰只是「加强」, 不是必要条件
        # (旧实现把背驰当必要条件 → 实测 400 只样本二买恒为 0, 买点几乎绝迹)。
        signals.append(
            ChanSignal(
                kind="b2",
                label="二买",
                date=strokes[pullback].end.date,
                price=strokes[pullback].end.price,
                bar_index=strokes[pullback].end.bar_index,
                confirmed_date=strokes[pullback].confirmed_date,
                level=params.level,
                detail={
                    **({"cur_stroke_index": pullback} if div else {}),
                    "rule": "一买后回抽不破前低",
                    "pullback_diverged": bool(div and div.diverged),
                    "b1_date": b1.date,
                    "b1_price": b1.price,
                },
            )
        )

    s1s = [s for s in signals if s.kind == "s1"]
    for s1 in s1s:
        s1_stroke = next(
            (i for i, s in enumerate(strokes) if s.end.date == s1.date and s.direction == "up"),
            None,
        )
        if s1_stroke is None:
            continue
        pullback = s1_stroke + 2
        if pullback >= n or strokes[pullback].direction != "up":
            continue
        if strokes[pullback].end.price >= strokes[s1_stroke].end.price:
            continue
        div = div_by_index.get(pullback)
        # 与二买镜像: 反抽不破一卖高点即为二卖, 顶背驰只是「加强」
        signals.append(
            ChanSignal(
                kind="s2",
                label="二卖",
                date=strokes[pullback].end.date,
                price=strokes[pullback].end.price,
                bar_index=strokes[pullback].end.bar_index,
                confirmed_date=strokes[pullback].confirmed_date,
                level=params.level,
                detail={
                    **({"cur_stroke_index": pullback} if div else {}),
                    "rule": "一卖后反抽不破前高",
                    "pullback_diverged": bool(div and div.diverged),
                    "s1_date": s1.date,
                    "s1_price": s1.price,
                },
            )
        )

    # ── 三买 / 三卖 ────────────────────────────────────────────
    # 向上突破中枢 ZG 之后的第一次回抽不回到中枢 → 三买 (向下镜像 → 三卖)。
    # 从中枢最后一笔起扫: 突破笔若与中枢区间有重叠会被延伸并入中枢 (end_index 即突破笔),
    # 紧随其后的离开笔才是第一次回抽; 从 end_index+1 起扫会跳过它, 错拿下一组笔。
    for p in pivots:
        for i in range(p.end_index, n):
            if strokes[i].direction == "up" and strokes[i].end.price > p.zg:
                pull = next(
                    (
                        j
                        for j in range(i + 1, n)
                        if strokes[j].direction == "down"
                    ),
                    None,
                )
                if (
                    pull is not None
                    and strokes[pull].end.price > p.zg
                    and _third_point_within_reach(strokes, p.start_index, pull, "up")
                ):
                    signals.append(
                        ChanSignal(
                            kind="b3",
                            label="三买",
                            date=strokes[pull].end.date,
                            price=strokes[pull].end.price,
                            bar_index=strokes[pull].end.bar_index,
                            confirmed_date=strokes[pull].confirmed_date,
                            level=params.level,
                            detail={
                                "rule": "上破中枢 ZG 后回抽不回中枢",
                                "pivot_zg": round(p.zg, 6),
                                "pivot_zd": round(p.zd, 6),
                                "break_stroke_index": i,
                            },
                        )
                    )
                break
            if strokes[i].direction == "down" and strokes[i].end.price < p.zd:
                pull = next(
                    (
                        j
                        for j in range(i + 1, n)
                        if strokes[j].direction == "up"
                    ),
                    None,
                )
                if (
                    pull is not None
                    and strokes[pull].end.price < p.zd
                    and _third_point_within_reach(strokes, p.start_index, pull, "down")
                ):
                    signals.append(
                        ChanSignal(
                            kind="s3",
                            label="三卖",
                            date=strokes[pull].end.date,
                            price=strokes[pull].end.price,
                            bar_index=strokes[pull].end.bar_index,
                            confirmed_date=strokes[pull].confirmed_date,
                            level=params.level,
                            detail={
                                "rule": "下破中枢 ZD 后反抽不回中枢",
                                "pivot_zg": round(p.zg, 6),
                                "pivot_zd": round(p.zd, 6),
                                "break_stroke_index": i,
                            },
                        )
                    )
                break

    # ── 结构一买 / 结构一卖 (4C: czsc cxt_first_buy_V221126) ───────────
    # 不看中枢: 最近 21/19/.../5 笔构成下行 (上行) 结构且末笔力度衰减。
    # 与上面的趋势一买并列输出, 两套口径互不替代。
    for i in range(n):
        cur = strokes[i]
        for size in _FIRST_POINT_WINDOWS:
            if i - size + 1 < 0:
                continue
            window = strokes[i - size + 1 : i + 1]
            if not _check_first_point(window, cur.direction):
                continue
            is_buy = cur.direction == "down"
            signals.append(
                ChanSignal(
                    kind="b1s" if is_buy else "s1s",
                    label="结构一买" if is_buy else "结构一卖",
                    date=cur.end.date,
                    price=cur.end.price,
                    bar_index=cur.end.bar_index,
                    confirmed_date=cur.confirmed_date,
                    level=params.level,
                    detail={
                        "rule": "czsc 结构一买: 下行结构末笔力度衰减"
                        if is_buy
                        else "czsc 结构一卖: 上行结构末笔力度衰减",
                        "window_strokes": size,
                    },
                )
            )
            break

    # 去重: 同一位置同一类型只保留一条 (不同中枢可能指向同一次突破/回抽)
    unique: dict[tuple[str, str], ChanSignal] = {}
    for sig in signals:
        unique.setdefault((sig.kind, sig.date), sig)
    signals = sorted(unique.values(), key=lambda s: (s.bar_index, s.kind))
    return signals


# ================================================================
# 主入口
# ================================================================


def _attach_volumes(
    strokes: list[Stroke],
    segments: list[Segment],
    merged: Sequence[MergedBar],
    dates: Sequence[str],
    volumes: Sequence[float],
) -> tuple[list[Stroke], list[Segment]]:
    """给笔 / 线段填成交量 (背驰的成交量力度口径)。

    笔: czsc ``get_power_volume`` = 笔合并 K 去掉首尾 (两端分型的外侧 K) 后的成交量,
    即起点分型中间合并 K 至终点分型中间合并 K 覆盖的原始 K 线成交量之和。
    段: 段起点至段终点 (极值点) 的原始 K 线成交量之和。
    """
    prefix = [0.0]
    for v in volumes:
        prefix.append(prefix[-1] + float(v or 0.0))

    def _sum(lo: int, hi: int) -> float:
        return prefix[hi + 1] - prefix[lo] if hi >= lo else 0.0

    strokes = [
        replace(
            st,
            volume=_sum(
                merged[st.start.merged_index].start_index,
                merged[st.end.merged_index].end_index,
            ),
        )
        for st in strokes
    ]
    date_index = {d: i for i, d in enumerate(dates)}
    segments = [
        replace(
            seg,
            volume=_sum(
                strokes[seg.start_index].start.bar_index,
                date_index.get(seg.end_date, strokes[seg.start_index].start.bar_index),
            ),
        )
        for seg in segments
    ]
    return strokes, segments


def analyze(
    bars: pl.DataFrame,
    params: ChanParams | None = None,
    factors: pl.DataFrame | None = None,
    symbol: str = "",
) -> ChanStructure:
    """对一组原始价 K 线做完整缠论结构分析。

    ``bars`` 必须含 ``date`` / ``high`` / ``low`` / ``close`` 四列, 其中价格
    视为**原始价**; 若同时含 ``raw_high`` / ``raw_low`` / ``raw_close``, 以其为准。
    ``factors`` 为复权因子表 (含 ``trade_date`` / ``ex_factor``, 单标的)。
    """
    params = params or ChanParams()
    params.validate()

    if bars is None or bars.is_empty():
        raise ValueError("bars 为空")
    if len(bars) < 3:
        raise ValueError("K 线不足 3 根, 无法构成一个分型")

    series = apply_price_mode(bars, factors, params)
    merged = merge_klines(series.high, series.low, series.dates)
    fractals = find_fractals(merged, params)
    strokes = build_strokes(fractals, params)
    segments = build_segments(strokes, params)
    has_volume = "volume" in bars.columns
    if has_volume:
        strokes, segments = _attach_volumes(
            strokes, segments, merged, series.dates, bars["volume"].fill_null(0).to_list()
        )
    pivots = build_pivots(strokes, params)
    trend = classify_trend(pivots, strokes)
    divergences = detect_bi_divergence(strokes, params)
    divergences = classify_divergence_scope(strokes, pivots, divergences)
    signals = build_signals(strokes, pivots, segments, divergences, params)

    # 段级别: 中枢 + 背驰 (与笔级并行, 不参与笔级买卖点判定)
    segment_pivots = build_segment_pivots(segments, params)
    segment_divergences = detect_segment_divergence(segments, params)
    segment_divergences = classify_segment_divergence_scope(
        segments, segment_pivots, segment_divergences
    )

    status = "ok"
    warnings: list[str] = []
    if len(strokes) == 0:
        status = "insufficient_data"
        warnings.append("未构成任何笔")
    if series.adj_factor_missing:
        warnings.append("adj_factor_missing: 无复权因子数据, 已退化为原始价")
    if not has_volume:
        warnings.append("volume_missing: 无成交量数据, 背驰只按价差与长度判定")

    return ChanStructure(
        symbol=symbol,
        price_mode=params.price_mode,
        adj_anchor_date=series.adj_anchor_date,
        level=params.level,
        dates=list(series.dates),
        closes=list(series.close),
        raw_closes=list(series.raw_close),
        highs=list(series.high),
        lows=list(series.low),
        fractals=list(fractals),
        strokes=list(strokes),
        segments=list(segments),
        pivots=list(pivots),
        signals=list(signals),
        trend=trend,
        status=status,
        adj_factor_missing=series.adj_factor_missing,
        warnings=warnings,
        divergences=list(divergences),
        segment_pivots=list(segment_pivots),
        segment_divergences=list(segment_divergences),
    )


def analyze_sublevel(
    daily_bars: pl.DataFrame,
    sub_bars: pl.DataFrame,
    params: ChanParams | None = None,
    factors: pl.DataFrame | None = None,
    symbol: str = "",
) -> ChanStructure:
    """日线内的次级别递归分析 (§4.8 预留)。

    M0/M1 **不实现**, 按 fail-closed 原则直接报错, 不返回空结构冒充成功。
    """
    raise NotImplementedError(
        "次级别递归分析尚未实现 (M2 之后); 目标级别: "
        f"{getattr(sub_level_of((params or ChanParams()).level), 'value', None)}"
    )


__all__ = [
    "AdjustedSeries",
    "ChanLevel",
    "ChanParams",
    "ChanSignal",
    "ChanStructure",
    "Divergence",
    "Fractal",
    "MergedBar",
    "Pivot",
    "Segment",
    "SegmentDivergence",
    "Stroke",
    "analyze",
    "analyze_sublevel",
    "apply_price_mode",
    "build_pivots",
    "build_segment_pivots",
    "build_segments",
    "build_signals",
    "build_strokes",
    "classify_divergence_scope",
    "classify_segment_divergence_scope",
    "classify_trend",
    "detect_bi_divergence",
    "detect_segment_divergence",
    "find_fractals",
    "merge_klines",
    "sub_level_of",
]
