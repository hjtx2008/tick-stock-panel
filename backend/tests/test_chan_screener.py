"""缠论选股器单元测试 (第 5 步)。

核心保证: 选股器给出的结构结论必须与详情页 ``/api/chan/structure``
(默认前复权口径) **逐项一致** —— 否则「选出来的票」和「点进去看到的图」会打架。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.custom.chan.core import ChanLevel, ChanParams, analyze, apply_price_mode
from app.custom.chan.screener import (
    ChanScreenConditions,
    ChanScreenQuery,
    build_factor_cum,
    build_qfq_frame,
    match_conditions,
    scan_frame,
)


def _dates(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out: list[date] = []
    cur = start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _zigzag(n: int = 120, base: float = 10.0, step: float = 0.4) -> tuple[list[float], list[float]]:
    """交替涨跌的锯齿行情, 便于构成笔与中枢。"""
    highs: list[float] = []
    lows: list[float] = []
    price = base
    for i in range(n):
        price = max(4.0, price + (step if (i // 5) % 2 == 0 else -step))
        highs.append(price + 0.5)
        lows.append(price - 0.5)
    return highs, lows


def _market(symbols: list[str], n: int = 120) -> pl.DataFrame:
    frames = []
    for i, sym in enumerate(symbols):
        highs, lows = _zigzag(n, base=10.0 + i)
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [sym] * n,
                    "date": _dates(n),
                    "raw_high": highs,
                    "raw_low": lows,
                    "raw_close": highs,
                }
            )
        )
    return pl.concat(frames).sort(["symbol", "date"])


def _factors(symbol: str, event_date: date, ex_factor: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol],
            "trade_date": [event_date],
            "ex_factor": [ex_factor],
        }
    )


def test_qfq_frame_matches_core_apply_price_mode() -> None:
    """全市场预计算的 qfq 价必须与 core.apply_price_mode 逐根一致。"""
    bars = _market(["000001.SZ"])
    factors = _factors("000001.SZ", date(2026, 3, 2), 0.9)

    qfq = build_qfq_frame(bars, build_factor_cum(factors))
    core = apply_price_mode(
        bars.select(["date", "raw_high", "raw_low", "raw_close"]),
        factors,
        ChanParams(level=ChanLevel.DAY, price_mode="qfq"),
    )

    assert qfq["close"].to_list() == pytest.approx(core.close)
    assert qfq["high"].to_list() == pytest.approx(core.high)
    assert qfq["low"].to_list() == pytest.approx(core.low)


def test_qfq_frame_without_factors_falls_back_to_raw() -> None:
    bars = _market(["000001.SZ"])
    qfq = build_qfq_frame(bars, build_factor_cum(pl.DataFrame()))
    assert qfq["close"].to_list() == pytest.approx(bars["raw_close"].to_list())


def test_scan_frame_matches_per_symbol_analyze() -> None:
    """选股器的结构摘要必须与逐标的 analyze() 一致。"""
    symbols = ["000001.SZ", "600000.SH"]
    bars = _market(symbols)
    factors = pl.concat(
        [
            _factors("000001.SZ", date(2026, 3, 2), 0.9),
            _factors("600000.SH", date(2026, 3, 10), 1.1),
        ]
    )

    result = scan_frame(
        bars, factors, ChanScreenQuery(days=120, symbols=None)
    )
    assert result["scanned"] == len(symbols)

    by_symbol = {m["symbol"]: m for m in result["matches"]}
    for sym in symbols:
        sub = bars.filter(pl.col("symbol") == sym)
        sub_factors = factors.filter(pl.col("symbol") == sym)
        if sub_factors.is_empty():
            sub_factors = None
        structure = analyze(
            sub.select(["date", "raw_high", "raw_low", "raw_close"]),
            ChanParams(level=ChanLevel.DAY, price_mode="qfq"),
            factors=sub_factors,
            symbol=sym,
        )
        got = by_symbol[sym]
        assert got["trend"] == structure.trend
        assert [s["kind"] for s in got["signals"]] == [s.kind for s in structure.signals]
        assert [s["date"] for s in got["signals"]] == [s.date for s in structure.signals]
        assert [s["price"] for s in got["signals"]] == pytest.approx(
            [s.price for s in structure.signals]
        )
        assert got["stroke_count"] == len(structure.strokes)
        assert got["segment_pivot_count"] == len(structure.segment_pivots)
        assert len(got["divergences"]) == len(structure.divergences)
        assert len(got["segment_divergences"]) == len(structure.segment_divergences)


def test_scan_frame_filters_by_symbol_list() -> None:
    bars = _market(["000001.SZ", "600000.SH", "000002.SZ"])
    result = scan_frame(
        bars,
        pl.DataFrame(),
        ChanScreenQuery(days=120, symbols=("000001.SZ", "000002.SZ")),
    )
    assert result["scanned"] == 2
    assert {m["symbol"] for m in result["matches"]} <= {"000001.SZ", "000002.SZ"}


def test_condition_signal_kinds_filters_matches() -> None:
    bars = _market(["000001.SZ"])
    result = scan_frame(bars, pl.DataFrame(), ChanScreenQuery(days=120))
    all_matches = result["matches"]
    assert all_matches, "样本应至少产生一个标的"

    hit = scan_frame(
        bars,
        pl.DataFrame(),
        ChanScreenQuery(days=120, conditions=ChanScreenConditions(signal_kinds=("b1",))),
    )
    for m in hit["matches"]:
        assert any(s["kind"] == "b1" for s in m["signals"])

    miss = scan_frame(
        bars,
        pl.DataFrame(),
        ChanScreenQuery(days=120, conditions=ChanScreenConditions(signal_kinds=("s3",))),
    )
    for m in miss["matches"]:
        assert any(s["kind"] == "s3" for s in m["signals"])


def test_match_conditions_rules() -> None:
    summary = {
        "symbol": "000001.SZ",
        "trend": "down",
        "bar_count": 100,
        "stroke_count": 8,
        "segment_count": 2,
        "pivot_count": 1,
        "segment_pivot_count": 1,
        "signals": [{"kind": "b1", "label": "一买", "date": "2026-03-02", "price": 9.0, "bar_index": 90}],
        "divergences": [
            {"date": "2026-03-02", "scope": "trend", "diverged": True, "direction": "down"}
        ],
        "segment_divergences": [
            {"date": "2026-03-02", "scope": "trend", "diverged": True, "direction": "down"}
        ],
    }
    assert match_conditions(summary, ChanScreenConditions())
    assert match_conditions(summary, ChanScreenConditions(trend=("down",)))
    assert not match_conditions(summary, ChanScreenConditions(trend=("up",)))
    assert match_conditions(summary, ChanScreenConditions(signal_kinds=("b1",)))
    assert not match_conditions(summary, ChanScreenConditions(signal_kinds=("b3",)))
    assert match_conditions(summary, ChanScreenConditions(require_divergence=True))
    assert match_conditions(summary, ChanScreenConditions(require_segment_divergence=True))
    assert match_conditions(summary, ChanScreenConditions(require_segment_pivot=True))
    empty_pivot = {**summary, "segment_pivot_count": 0}
    assert not match_conditions(empty_pivot, ChanScreenConditions(require_segment_pivot=True))
    # 信号距样本末日 100-1-90 = 9 根
    assert match_conditions(summary, ChanScreenConditions(max_signal_age=9))
    assert not match_conditions(summary, ChanScreenConditions(max_signal_age=8))
    # 新鲜度针对被命中的那类信号: b1 很旧 + 无关的信号很新 → 不算命中
    stale = {
        **summary,
        "signals": [
            {"kind": "b1", "label": "一买", "date": "2026-01-02", "price": 9.0, "bar_index": 10},
            {"kind": "s3", "label": "三卖", "date": "2026-03-02", "price": 9.5, "bar_index": 99},
        ],
    }
    assert not match_conditions(
        stale, ChanScreenConditions(signal_kinds=("b1",), max_signal_age=5)
    )
    assert match_conditions(stale, ChanScreenConditions(max_signal_age=5))
    # 盘整背驰口径
    assert not match_conditions(summary, ChanScreenConditions(divergence_scope=("consolidation",)))
    assert match_conditions(summary, ChanScreenConditions(divergence_scope=("trend",)))


def test_invalid_conditions_raise() -> None:
    with pytest.raises(ValueError):
        ChanScreenConditions(signal_kinds=("b9",)).validate()
    with pytest.raises(ValueError):
        ChanScreenConditions(trend=("sideways",)).validate()
    with pytest.raises(ValueError):
        ChanScreenConditions(divergence_scope=("x",)).validate()
    with pytest.raises(ValueError):
        ChanScreenConditions(max_signal_age=-1).validate()


def test_scan_frame_rejects_short_window() -> None:
    bars = _market(["000001.SZ"])
    with pytest.raises(ValueError):
        scan_frame(bars, pl.DataFrame(), ChanScreenQuery(days=10))


def test_scan_frame_limit_caps_rows() -> None:
    bars = _market(["000001.SZ", "600000.SH", "000002.SZ"])
    result = scan_frame(bars, pl.DataFrame(), ChanScreenQuery(days=120, limit=1))
    assert len(result["matches"]) <= 1
    assert result["total_matched"] >= len(result["matches"])
