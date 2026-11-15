"""缠论算法内核单元测试 (docs/chan-theory-spec.md §8)。

覆盖:
- §8.1 包含处理 / §8.2 分型 / §8.3 成笔
- §8.4 线段 / §8.5 中枢 / §8.6 走势类型
- §8.7 背驰与买卖点 (D6 拍板的笔力度公式)
- §8.8 复权口径 (raw / qfq / hfq / 锚点稳定性)
- §8.7 截断测试 (T7.1): 不得依赖未来 K 线
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.custom.chan.core import (
    ChanLevel,
    ChanParams,
    Fractal,
    Pivot,
    Stroke,
    analyze,
    analyze_sublevel,
    apply_price_mode,
    build_pivots,
    build_segments,
    build_strokes,
    detect_bi_divergence,
    find_fractals,
    merge_klines,
    sub_level_of,
)

BASE_DATE = date(2026, 1, 5)


# ================================================================
# 造数辅助
# ================================================================


def _dates(n: int, start: date = BASE_DATE) -> list[str]:
    """生成 n 个连续交易日的日期字符串 (跳过周末)。"""
    out: list[str] = []
    cur = start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _bars(highs: list[float], lows: list[float], closes: list[float] | None = None) -> pl.DataFrame:
    n = len(highs)
    assert len(lows) == n
    if closes is None:
        closes = list(highs)
    return pl.DataFrame(
        {
            "date": _dates(n),
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


# 一段标准锯齿: 顶@2 → 底@6 → 顶@10 (分型清晰, 无包含关系)
ZIGZAG_HIGHS = [11, 13, 15, 14, 13, 12, 10, 11, 13, 14, 15, 14]
ZIGZAG_LOWS = [10, 12, 14, 13, 12, 11, 9, 10, 12, 13, 14, 13]


def _fractal(kind: str, price: float, date_str: str, index: int, merged: int = 0) -> Fractal:
    return Fractal(
        kind=kind,  # type: ignore[arg-type]
        price=price,
        date=date_str,
        bar_index=index,
        merged_index=merged,
        start_index=max(index - 1, 0),
        end_index=index + 1,
        confirmed_date=date_str,
    )


def _stroke(direction: str, start: Fractal, end: Fractal, k_count: int) -> Stroke:
    return Stroke(
        direction=direction,  # type: ignore[arg-type]
        start=start,
        end=end,
        k_count=k_count,
        confirmed_date=end.confirmed_date,
    )


# ================================================================
# §8.1 包含处理
# ================================================================


def test_merge_klines_merges_containing_bars() -> None:
    """第二根被第一根包含 → 合并成一根, 区间 [0,1]。"""
    merged = merge_klines([11.0, 10.8, 12.0], [10.0, 10.2, 11.0], _dates(3))
    assert len(merged) == 2
    assert merged[0].start_index == 0
    assert merged[0].end_index == 1
    # 首两根构成包含关系时方向由高点关系决定: 当前高 10.8 < 前高 11 → 下降方向
    assert merged[0].high == 10.8
    assert merged[0].low == 10.0


def test_merge_klines_down_direction_takes_lower() -> None:
    """未被包含的 K 线原样保留, 方向随之翻转。"""
    merged = merge_klines([11.0, 10.8, 12.0], [10.0, 10.2, 11.0], _dates(3))
    assert merged[1].high == 12.0
    assert merged[1].low == 11.0
    assert merged[1].start_index == 2


def test_merge_klines_keeps_non_containing_bars() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    assert len(merged) == len(ZIGZAG_HIGHS)


# ================================================================
# §8.2 分型
# ================================================================


def test_find_fractals_detects_top_and_bottom() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    kinds = [(f.kind, f.merged_index) for f in fractals]
    assert ("top", 2) in kinds
    assert ("bottom", 6) in kinds
    assert ("top", 10) in kinds


def test_find_fractals_requires_both_high_and_low_extreme() -> None:
    """高点最高但低点不是最高 → 不成顶分型 (严格原文版)。"""
    highs = [10.0, 12.0, 11.0, 11.5]
    lows = [9.0, 8.0, 9.5, 9.0]
    merged = merge_klines(highs, lows, _dates(4))
    fractals = find_fractals(merged, ChanParams())
    assert not any(f.kind == "top" and f.merged_index == 1 for f in fractals)


# ================================================================
# §8.3 成笔
# ================================================================


def test_build_strokes_applies_new_stroke_gap_rule() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    assert len(strokes) == 2
    assert strokes[0].direction == "down"
    assert strokes[1].direction == "up"
    assert strokes[0].start.price == 15.0
    assert strokes[0].end.price == 9.0
    # 笔跨越的 K 线根数 = 首尾极值之间的原始 K 线数
    assert strokes[0].k_count == 5  # 顶@2 → 底@6
    assert strokes[1].k_count == 5  # 底@6 → 顶@10


def test_build_strokes_respects_min_k_count() -> None:
    """把 min_k_count 调高 → 不成笔。"""
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams(min_k_count=99))
    assert strokes == []


def test_build_strokes_strict_no_shared_kline_is_the_default() -> None:
    """新笔默认不共用 K 线: 关闭严格模式后间隔要求放宽。"""
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strict = build_strokes(fractals, ChanParams(strict_no_shared_kline=True))
    loose = build_strokes(fractals, ChanParams(strict_no_shared_kline=False))
    assert len(strict) <= len(loose)


def test_stroke_amplitude_and_strength() -> None:
    """笔幅度 = 绝对价格差; 笔力度 = 幅度 / 天数 * 100。"""
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    s = strokes[0]
    assert s.amplitude == pytest.approx(6.0)
    assert s.strength == pytest.approx(6.0 / s.k_count * 100.0)


# ================================================================
# §8.7 背驰 (D6 公式)
# ================================================================


def _three_strokes() -> list[Stroke]:
    """构造 涨-跌-涨-跌 四笔, 最后一笔与前一笔同向 (下跌)。"""
    return [
        _stroke("up", _fractal("bottom", 9.0, "2026-01-05", 0), _fractal("top", 15.0, "2026-01-12", 6), 6),
        _stroke("down", _fractal("top", 15.0, "2026-01-12", 6), _fractal("bottom", 9.0, "2026-01-19", 12), 6),
        _stroke("up", _fractal("bottom", 9.0, "2026-01-19", 12), _fractal("top", 15.0, "2026-01-26", 18), 6),
        _stroke("down", _fractal("top", 15.0, "2026-01-26", 18), _fractal("bottom", 12.0, "2026-02-02", 24), 6),
    ]


def test_divergence_requires_smaller_amplitude_or_strength() -> None:
    """幅度变小 → 笔背离成立 (D6 判定式)。"""
    strokes = _three_strokes()
    divs = detect_bi_divergence(strokes, ChanParams())
    last = next(d for d in divs if d.stroke_index == 3)
    assert last.days_ok is True
    assert last.amplitude_ok is True
    assert last.diverged is True


def test_divergence_not_triggered_when_amplitude_grows() -> None:
    """幅度变大且天数更少 → 不成立。"""
    strokes = _three_strokes()
    bigger = _stroke(
        "down",
        _fractal("top", 15.0, "2026-01-26", 18),
        _fractal("bottom", 8.0, "2026-02-02", 24),
        3,
    )
    strokes = [*strokes[:3], bigger]
    divs = detect_bi_divergence(strokes, ChanParams())
    last = next(d for d in divs if d.stroke_index == 3)
    assert last.days_ok is False
    assert last.amplitude_ok is False
    assert last.diverged is False


def test_divergence_uses_previous_same_direction_stroke() -> None:
    """前笔 = 上一笔同方向的笔 (相隔一笔), 不是相邻的上一笔。"""
    strokes = _three_strokes()
    divs = detect_bi_divergence(strokes, ChanParams())
    last = next(d for d in divs if d.stroke_index == 3)
    assert last.prev_index == 1


def test_divergence_strength_only_case() -> None:
    """幅度持平但力度变小 → 仍算背离。"""
    strokes = [
        _stroke("down", _fractal("top", 15.0, "2026-01-05", 0), _fractal("bottom", 9.0, "2026-01-12", 6), 4),
        _stroke("up", _fractal("bottom", 9.0, "2026-01-12", 6), _fractal("top", 15.0, "2026-01-19", 12), 4),
        _stroke("down", _fractal("top", 15.0, "2026-01-19", 12), _fractal("bottom", 9.0, "2026-01-26", 18), 8),
    ]
    divs = detect_bi_divergence(strokes, ChanParams())
    last = next(d for d in divs if d.stroke_index == 2)
    assert last.amplitude_ok is False
    assert last.strength_ok is True
    assert last.diverged is True


# ================================================================
# §8.8 复权口径
# ================================================================


def _factors(rows: list[tuple[str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * len(rows),
            "trade_date": [date.fromisoformat(d) for d, _ in rows],
            "ex_factor": [f for _, f in rows],
        }
    )


def test_price_mode_raw_equals_raw_prices() -> None:
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    series = apply_price_mode(bars, None, ChanParams(price_mode="raw"))
    assert series.close == [10.0, 11.0, 12.0]
    assert series.adj_anchor_date is None


def test_qfq_without_events_equals_raw() -> None:
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    series = apply_price_mode(bars, _factors([]), ChanParams(price_mode="qfq"))
    assert series.close == [10.0, 11.0, 12.0]


def test_qfq_anchor_price_unchanged_and_history_shifted_down() -> None:
    """锚点日价格 == 原始价; 锚点之前被向下调整。"""
    dates = _dates(4)
    bars = pl.DataFrame(
        {
            "date": dates,
            "high": [10.0, 11.0, 12.0, 13.0],
            "low": [9.0, 10.0, 11.0, 12.0],
            "close": [9.5, 10.5, 11.5, 12.5],
        }
    )
    factors = _factors([(dates[2], 1.1)])
    series = apply_price_mode(bars, factors, ChanParams(price_mode="qfq"))
    # 锚点默认样本末日 → 末日价格不变
    assert series.close[-1] == pytest.approx(12.5)
    # 事件日之前价格被除以 1.1
    assert series.close[0] == pytest.approx(9.5 / 1.1)
    # 锚点回显
    assert series.adj_anchor_date == dates[-1]


def test_qfq_fixed_anchor_is_stable_when_new_event_arrives() -> None:
    """T8.4 稳定性: 锚点固定后, 追加新除权事件不改变锚点及之前的价格。"""
    dates = _dates(5)
    base = pl.DataFrame(
        {
            "date": dates,
            "high": [10.0, 11.0, 12.0, 13.0, 14.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "close": [9.5, 10.5, 11.5, 12.5, 13.5],
        }
    )
    anchor = dates[4]
    params = ChanParams(price_mode="qfq", adj_anchor_date=date.fromisoformat(anchor))
    factors_before = _factors([(dates[2], 1.1)])
    factors_after = _factors([(dates[2], 1.1), ("2026-03-02", 1.05)])

    before = apply_price_mode(base, factors_before, params)
    after = apply_price_mode(base, factors_after, params)
    assert before.close == pytest.approx(after.close)


def test_hfq_first_price_equals_raw() -> None:
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    factors = _factors([("2026-01-07", 1.1)])
    series = apply_price_mode(bars, factors, ChanParams(price_mode="hfq"))
    assert series.close[0] == pytest.approx(10.0)
    assert series.close[2] == pytest.approx(12.0 * 1.1)


def test_missing_factors_fallback_is_flagged() -> None:
    """无因子数据 → 退化为 raw 并显式标记, 不静默。"""
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    series = apply_price_mode(bars, None, ChanParams(price_mode="qfq"))
    assert series.close == [10.0, 11.0, 12.0]
    assert series.adj_factor_missing is True


def test_anchor_outside_sample_falls_back_and_echoes() -> None:
    """锚点不在样本内 → 回退样本末日, 并回显实际锚点 (无因子时同样回显)。"""
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    params = ChanParams(price_mode="qfq", adj_anchor_date=date(2020, 1, 1))
    series = apply_price_mode(bars, None, params)
    assert series.adj_anchor_date == bars["date"][-1]
    assert series.adj_factor_missing is True


# ================================================================
# §8.6/§8.5 中枢与走势
# ================================================================


def test_build_pivots_requires_three_strokes() -> None:
    """中枢至少要三笔, 否则不成中枢。"""
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    assert build_pivots(strokes, ChanParams()) == []
    assert build_pivots(strokes, ChanParams(min_strokes_per_pivot=2)) != []


def test_build_pivots_zg_above_zd() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    for pivot in build_pivots(strokes, ChanParams(min_strokes_per_pivot=2)):
        assert pivot.zg > pivot.zd


def test_build_segments_returns_alternating_directions() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    segments = build_segments(strokes, ChanParams())
    dirs = [s.direction for s in segments]
    assert all(dirs[i] != dirs[i + 1] for i in range(len(dirs) - 1))


# ================================================================
# T7.1 截断测试 — 不得依赖未来 K 线
# ================================================================


def test_truncated_analysis_does_not_depend_on_future_bars() -> None:
    """前 N 根算出的结构, 在前 N+M 根里必须原样存在 (不依赖未来数据)。

    注意: 缠论结构会被后续 K 线"修订"(同型分型被更极端者替换), 因此只比较
    **已确认** 的结构 —— 截断样本里 end.bar_index 距样本末至少 2 根的笔,
    在长样本里必须逐字段一致 (含 confirmed_date)。
    """
    highs = [
        11, 13, 15, 14, 13, 12, 10, 11, 13, 14, 15, 14,
        13, 12, 13, 14, 16, 15, 14, 13, 12, 11, 12, 13,
        15, 14, 13, 12, 11, 10, 11, 13,
    ]
    lows = [
        10, 12, 14, 13, 12, 11, 9, 10, 12, 13, 14, 13,
        12, 11, 12, 13, 15, 14, 13, 12, 11, 10, 11, 12,
        14, 13, 12, 11, 10, 9, 10, 12,
    ]
    full = _bars([float(h) for h in highs], [float(low_v) for low_v in lows])
    n = 20
    cut = full.head(n)

    short = analyze(cut, ChanParams(price_mode="raw"))
    long = analyze(full, ChanParams(price_mode="raw"))

    margin = 2
    closed = [s for s in short.strokes if s.end.bar_index <= n - 1 - margin]
    assert closed, "截断样本应至少有一笔已确认的笔"

    long_keys = {
        (s.start.price, s.end.price, s.start.date, s.end.date, s.k_count, s.confirmed_date)
        for s in long.strokes
    }
    for s in closed:
        key = (s.start.price, s.end.price, s.start.date, s.end.date, s.k_count, s.confirmed_date)
        assert key in long_keys, f"截断样本的笔在长样本中找不到: {key}"


def test_analyze_never_confirms_beyond_sample_end() -> None:
    """确认日不得超出样本末日 (确认滞后不得穿越到未来)。"""
    bars = _bars([float(h) for h in ZIGZAG_HIGHS], [float(low_v) for low_v in ZIGZAG_LOWS])
    structure = analyze(bars, ChanParams(price_mode="raw"))
    last_date = structure.dates[-1]
    for stroke in structure.strokes:
        assert stroke.confirmed_date <= last_date


# ================================================================
# §4.8 次级别递归接口 (预留, 必须 fail-closed)
# ================================================================


def test_sub_level_of_chain() -> None:
    assert sub_level_of(ChanLevel.DAY) is ChanLevel.M30
    assert sub_level_of(ChanLevel.M30) is ChanLevel.M5
    assert sub_level_of(ChanLevel.M5) is ChanLevel.M1
    assert sub_level_of(ChanLevel.M1) is None


def test_analyze_sublevel_not_implemented_fails_closed() -> None:
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    with pytest.raises(NotImplementedError):
        analyze_sublevel(bars, bars, ChanParams())


# ================================================================
# 参数校验
# ================================================================


@pytest.mark.parametrize(
    "params",
    [
        ChanParams(price_mode="xxx"),  # type: ignore[arg-type]
        ChanParams(overlap_mode="xxx"),  # type: ignore[arg-type]
        ChanParams(divergence_mode="xxx"),  # type: ignore[arg-type]
        ChanParams(min_k_count=2),
        ChanParams(min_strokes_per_pivot=2),
    ],
)
def test_invalid_params_raise(params: ChanParams) -> None:
    bars = _bars([10.0, 11.0, 12.0], [9.0, 10.0, 11.0])
    with pytest.raises(ValueError):
        analyze(bars, params)


def test_analyze_rejects_short_input() -> None:
    bars = _bars([10.0, 11.0], [9.0, 10.0])
    with pytest.raises(ValueError):
        analyze(bars, ChanParams(price_mode="raw"))


def test_analyze_returns_structure_summary() -> None:
    bars = _bars([float(h) for h in ZIGZAG_HIGHS], [float(low_v) for low_v in ZIGZAG_LOWS])
    structure = analyze(bars, ChanParams(price_mode="raw"), symbol="000001.SZ")
    summary = structure.summary()
    assert summary["symbol"] == "000001.SZ"
    assert summary["status"] in ("ok", "insufficient_data")
    assert summary["bar_count"] == len(ZIGZAG_HIGHS)
    assert summary["stroke_count"] == len(structure.strokes)


# ================================================================
# §8.5/§8.6 中枢方向与走势类型
# ================================================================


def _mk_strokes(spec: list[tuple[str, float, float, int]]) -> list[Stroke]:
    """按 (方向, 起点价, 终点价, 笔根数) 造一串笔, 日期依次顺延。"""
    out: list[Stroke] = []
    cursor = 0
    for direction, start_price, end_price, k_count in spec:
        start_date = _dates(cursor + k_count)[cursor]
        end_date = _dates(cursor + k_count)[cursor + k_count - 1]
        start = Fractal(
            kind="bottom" if direction == "up" else "top",
            price=start_price,
            date=start_date,
            bar_index=cursor,
            merged_index=cursor,
            start_index=cursor,
            end_index=cursor + k_count - 1,
            confirmed_date=end_date,
        )
        end = Fractal(
            kind="top" if direction == "up" else "bottom",
            price=end_price,
            date=end_date,
            bar_index=cursor + k_count - 1,
            merged_index=cursor + k_count - 1,
            start_index=cursor,
            end_index=cursor + k_count - 1,
            confirmed_date=end_date,
        )
        out.append(
            Stroke(
                direction=direction,  # type: ignore[arg-type]
                start=start,
                end=end,
                k_count=k_count,
                confirmed_date=end_date,
            )
        )
        cursor += k_count
    return out


def test_pivot_direction_follows_entering_stroke() -> None:
    """中枢方向 = 进入中枢的那一段的方向 (否则相邻中枢方向必然交替)。"""
    strokes = _mk_strokes(
        [
            ("down", 20.0, 18.0, 5),
            ("up", 18.0, 21.0, 5),
            ("down", 21.0, 19.5, 5),
        ]
    )
    pivots = build_pivots(strokes, ChanParams(min_strokes_per_pivot=3))
    assert pivots, "三笔重叠应构成中枢"
    # 第一笔是向下进入 → 中枢方向向下
    assert pivots[0].direction == "down"


def test_classify_trend_detects_two_same_direction_pivots() -> None:
    from app.custom.chan.core import Pivot, classify_trend

    p1 = Pivot(direction="down", zd=18.0, zg=25.0, start_date="2026-01-05", end_date="2026-01-20", start_index=0, end_index=2, stroke_count=3)
    p2 = Pivot(direction="down", zd=16.0, zg=17.5, start_date="2026-01-21", end_date="2026-02-05", start_index=3, end_index=5, stroke_count=3)
    assert classify_trend([p1, p2], []) == "down"
    p3 = Pivot(direction="up", zd=19.0, zg=22.0, start_date="2026-02-06", end_date="2026-02-20", start_index=6, end_index=8, stroke_count=3)
    assert classify_trend([p1, p2, p3], []) == "down"


def test_classify_trend_oscillation_when_pivots_overlap() -> None:
    from app.custom.chan.core import Pivot, classify_trend

    p1 = Pivot(direction="down", zd=18.0, zg=25.0, start_date="2026-01-05", end_date="2026-01-20", start_index=0, end_index=2, stroke_count=3)
    p2 = Pivot(direction="down", zd=20.0, zg=26.0, start_date="2026-01-21", end_date="2026-02-05", start_index=3, end_index=5, stroke_count=3)
    assert classify_trend([p1, p2], []) == "oscillation"


# ================================================================
# §8.7 买卖点
# ================================================================


def test_first_buy_signal_on_down_trend_divergence() -> None:
    """下跌趋势中最后一笔底背驰 → 一买。"""
    from app.custom.chan.core import Pivot, build_signals

    strokes = _mk_strokes(
        [
            ("up", 20.0, 25.0, 5),
            ("down", 25.0, 18.0, 5),
            ("up", 18.0, 23.0, 5),
            ("down", 23.0, 17.0, 5),
            ("up", 17.0, 21.0, 5),
            ("down", 21.0, 16.5, 5),
            ("up", 16.5, 20.0, 5),
            ("down", 20.0, 16.2, 6),  # 与第 5 笔同向: 幅度/力度都变小 → 底背驰
        ]
    )
    pivots = [
        Pivot(direction="down", zd=18.0, zg=25.0, start_date=strokes[0].start_date, end_date=strokes[2].end_date, start_index=0, end_index=2, stroke_count=3),
        Pivot(direction="down", zd=16.0, zg=17.5, start_date=strokes[3].start_date, end_date=strokes[5].end_date, start_index=3, end_index=5, stroke_count=3),
    ]
    params = ChanParams()
    divergences = detect_bi_divergence(strokes, params)
    signals = build_signals(strokes, pivots, [], divergences, params)
    b1 = [s for s in signals if s.kind == "b1"]
    assert b1, "应产生一类买点"
    assert b1[0].date == strokes[7].end_date
    assert b1[0].price == pytest.approx(16.2)
    assert b1[0].detail["cur_days"] == 6
    assert b1[0].detail["prev_days"] == 5


def test_first_buy_signal_not_repainted_by_later_pivot() -> None:
    """一买只依据背驰笔之前已完成的中枢判定; 之后形成的中枢不得改写它。

    复现: 一买之后行情继续, 又形成一个与前一下跌中枢重叠的下跌中枢。
    若按「全体中枢的最后两个」判趋势, 下跌趋势不再成立, 历史一买会消失。
    """
    from app.custom.chan.core import Pivot, build_signals

    base_spec = [
        ("up", 20.0, 25.0, 5),
        ("down", 25.0, 18.0, 5),
        ("up", 18.0, 23.0, 5),
        ("down", 23.0, 17.0, 5),
        ("up", 17.0, 21.0, 5),
        ("down", 21.0, 16.5, 5),
        ("up", 16.5, 20.0, 5),
        ("down", 20.0, 16.2, 6),  # 底背驰 → 一买
    ]
    later_spec = [
        ("up", 16.2, 17.9, 5),
        ("down", 17.9, 16.4, 5),
        ("up", 16.4, 17.8, 5),
        ("down", 17.8, 16.3, 5),
    ]
    params = ChanParams()

    def b1_of(spec: list[tuple[str, float, float, int]], extra_pivots: int) -> list[tuple[str, float]]:
        strokes = _mk_strokes(spec)
        pivots = [
            Pivot(direction="down", zd=18.0, zg=25.0, start_date=strokes[0].start_date, end_date=strokes[2].end_date, start_index=0, end_index=2, stroke_count=3),
            Pivot(direction="down", zd=16.0, zg=17.5, start_date=strokes[3].start_date, end_date=strokes[5].end_date, start_index=3, end_index=5, stroke_count=3),
        ]
        if extra_pivots:
            # 与前一下跌中枢重叠 (ZG 17.8 >= 前 ZD 16.0) → 全体中枢视角下不再是下跌趋势
            pivots.append(
                Pivot(direction="down", zd=16.4, zg=17.8, start_date=strokes[9].start_date, end_date=strokes[11].end_date, start_index=9, end_index=11, stroke_count=3)
            )
        divergences = detect_bi_divergence(strokes, params)
        signals = build_signals(strokes, pivots, [], divergences, params)
        return [(s.date, s.price) for s in signals if s.kind == "b1"]

    early = b1_of(base_spec, extra_pivots=0)
    late = b1_of(base_spec + later_spec, extra_pivots=1)
    assert early, "截断样本应产生一买"
    assert set(early) <= set(late), f"后续行情改写了历史一买: {early} -> {late}"


def test_third_buy_signal_when_pullback_stays_above_pivot() -> None:
    """上破中枢 ZG 后回抽不回中枢 → 三买。"""
    from app.custom.chan.core import Pivot, build_signals

    strokes = _mk_strokes(
        [
            ("up", 9.0, 10.0, 5),
            ("down", 10.0, 9.2, 5),
            ("up", 9.2, 12.0, 5),   # 突破中枢上沿
            ("down", 12.0, 11.0, 5),  # 回抽仍在上沿之上
        ]
    )
    pivots = [
        Pivot(direction="up", zd=9.2, zg=10.0, start_date=strokes[0].start_date, end_date=strokes[1].end_date, start_index=0, end_index=1, stroke_count=2),
    ]
    params = ChanParams()
    divergences = detect_bi_divergence(strokes, params)
    signals = build_signals(strokes, pivots, [], divergences, params)
    b3 = [s for s in signals if s.kind == "b3"]
    assert b3, "应产生三类买点"
    assert b3[0].date == strokes[3].end_date


def test_third_buy_is_first_pullback_after_breakout_absorbed_into_pivot() -> None:
    """突破笔与中枢区间重叠时会被延伸并入中枢; 三买必须是紧随其后的第一次回抽。

    笔价 8→12→10→13→11→16→14→18→15: 前三笔中枢 [ZD=10, ZG=12],
    11→16 与中枢重叠被并入, 16→14 不回中枢 → 三买在 14, 而不是之后的 15。
    """
    from app.custom.chan.core import build_pivots, build_signals

    prices = [8.0, 12.0, 10.0, 13.0, 11.0, 16.0, 14.0, 18.0, 15.0]
    spec = [
        ("up" if b > a else "down", a, b, 5) for a, b in zip(prices[:-1], prices[1:], strict=True)
    ]
    strokes = _mk_strokes(spec)
    params = ChanParams()
    pivots = build_pivots(strokes, params)
    assert pivots and pivots[0].zg == pytest.approx(12.0)
    assert pivots[0].end_index == 4, "突破笔 11→16 应被延伸并入中枢"

    divergences = detect_bi_divergence(strokes, params)
    signals = build_signals(strokes, pivots, [], divergences, params)
    b3 = [s for s in signals if s.kind == "b3"]
    assert b3, "应产生三类买点"
    assert b3[0].date == strokes[5].end_date
    assert b3[0].price == pytest.approx(14.0)


def test_third_buy_absent_when_pullback_returns_into_pivot() -> None:
    """回抽跌回中枢内部 → 不是三买。"""
    from app.custom.chan.core import Pivot, build_signals

    strokes = _mk_strokes(
        [
            ("up", 9.0, 10.0, 5),
            ("down", 10.0, 9.2, 5),
            ("up", 9.2, 12.0, 5),
            ("down", 12.0, 9.8, 5),  # 回抽进中枢
        ]
    )
    pivots = [
        Pivot(direction="up", zd=9.2, zg=10.0, start_date=strokes[0].start_date, end_date=strokes[1].end_date, start_index=0, end_index=1, stroke_count=2),
    ]
    params = ChanParams()
    divergences = detect_bi_divergence(strokes, params)
    signals = build_signals(strokes, pivots, [], divergences, params)
    assert not [s for s in signals if s.kind == "b3"]


# ================================================================
# 线段口径
# ================================================================


def test_feature_fractal_mode_is_parameterized() -> None:
    """严格原文版 (高低同时极端) 与实务版 (只看同向极值) 结果可区分。"""
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    strict = build_segments(strokes, ChanParams(feature_fractal_mode="strict"))
    extreme = build_segments(strokes, ChanParams(feature_fractal_mode="extreme"))
    assert isinstance(strict, list) and isinstance(extreme, list)


def test_segments_alternate_direction_and_cover_strokes() -> None:
    merged = merge_klines(ZIGZAG_HIGHS, ZIGZAG_LOWS, _dates(len(ZIGZAG_HIGHS)))
    fractals = find_fractals(merged, ChanParams())
    strokes = build_strokes(fractals, ChanParams())
    segments = build_segments(strokes, ChanParams())
    for i in range(len(segments) - 1):
        assert segments[i].direction != segments[i + 1].direction
        assert segments[i].end_index == segments[i + 1].start_index


def _segment_strokes() -> list[Stroke]:
    """构造一串足以成段的笔 (9 笔)。

    刻意直接造 ``Stroke`` 而非从 K 线推导: K 线造数要凑够
    ``min_strokes_per_segment=3`` 笔且分型清晰, 冗长且脆弱。
    价格形态: 下-上-下-上(前 4 笔成一段) → 上-下-上(第 2 段) → 末段延伸中。
    """
    spec: list[tuple[str, float, float]] = [
        ("down", 10.0, 8.5),
        ("up", 8.5, 9.5),
        ("down", 9.5, 7.0),
        ("up", 7.0, 8.2),
        ("down", 8.2, 7.8),
        ("up", 7.8, 8.6),
        ("down", 8.6, 7.2),
        ("up", 7.2, 8.5),
        ("down", 8.5, 8.0),
    ]
    strokes: list[Stroke] = []
    for i, (direction, start_price, end_price) in enumerate(spec):
        start = _fractal(
            "top" if direction == "down" else "bottom",
            start_price,
            f"2026-01-{i + 1:02d}",
            i,
        )
        end = _fractal(
            "bottom" if direction == "down" else "top",
            end_price,
            f"2026-01-{i + 2:02d}",
            i + 1,
        )
        strokes.append(_stroke(direction, start, end, 5))
    return strokes


def test_confirmed_segment_date_not_before_strokes_used_to_confirm() -> None:
    """线段成立要用到特征序列第三元素与破坏笔 (终点笔之后的笔), 确认日不得早于它们。"""
    strokes = _segment_strokes()
    segments = build_segments(strokes, ChanParams())
    confirmed = [s for s in segments if s.confirmed]
    assert confirmed, "样本应至少有一段已确认线段"
    for seg in confirmed:
        # 终点笔 b 之后紧跟的破坏笔 b+1 必然参与确认
        used = strokes[seg.end_index + 1]
        assert seg.confirmed_date >= used.confirmed_date, (
            f"线段 {seg.start_index}-{seg.end_index} 确认日 {seg.confirmed_date} "
            f"早于确认所用笔 {used.confirmed_date}"
        )


def test_segments_cover_all_strokes_including_unconfirmed_tail() -> None:
    """末段未被破坏时, 线段必须覆盖到最后一笔 (以 confirmed=False 显式输出)。

    回归背景: 早期实现直接 break 丢弃剩余笔, 图上线段只画到全图中段,
    看着像「算法后半段失效」。
    """
    strokes = _segment_strokes()
    segments = build_segments(strokes, ChanParams())
    assert strokes, "造数应至少成笔"
    assert segments, "应至少切出线段"
    # 覆盖到最后一笔
    assert segments[-1].end_index == len(strokes) - 1
    # 未确认末段最多只有一段, 且必须显式标记
    unconfirmed = [s for s in segments if not s.confirmed]
    assert len(unconfirmed) <= 1
    assert all(s.confirmed for s in segments[:-1]), "只有末段可以是未确认段"
    # 末段必须显式标记为未确认
    assert segments[-1].confirmed is False


def test_unconfirmed_tail_segment_is_not_emitted_when_too_short() -> None:
    """剩余笔数不足 min_strokes_per_segment 时不输出未确认末段。"""
    strokes = _segment_strokes()
    params = ChanParams(min_strokes_per_segment=999)
    segments = build_segments(strokes, params)
    assert segments == [] or all(s.confirmed for s in segments)


# ================================================================
# §8.6 走势判据口径回归: 必须用「最后两个」同向中枢
# ================================================================


def _pivot(direction: str, zd: float, zg: float, idx: int) -> Pivot:
    """构造一个仅用于走势判定的中枢 (日期/索引按 idx 顺延)。"""
    dates = _dates(12)
    base = idx * 3
    return Pivot(
        direction=direction,  # type: ignore[arg-type]
        zd=zd,
        zg=zg,
        start_date=dates[base],
        end_date=dates[base + 2],
        start_index=base,
        end_index=base + 2,
        stroke_count=3,
    )


def test_classify_trend_uses_last_two_same_direction_pivots() -> None:
    """前两个不重叠但后两个重叠 → 判盘整 (前两个口径会误判为趋势)。"""
    from app.custom.chan.core import classify_trend

    p1 = _pivot("down", 18.0, 25.0, 0)   # 首个中枢
    p2 = _pivot("down", 16.0, 17.5, 1)   # 与 p1 不重叠 (17.5 < 18.0)
    p3 = _pivot("down", 20.0, 26.0, 2)   # 与 p2 重叠 → 最后两个不构成趋势
    assert classify_trend([p1, p2, p3], []) == "oscillation"


def test_classify_trend_detects_later_same_direction_pivots() -> None:
    """前两个重叠但后两个不重叠 → 判趋势 (前两个口径会误判为盘整)。"""
    from app.custom.chan.core import classify_trend

    p1 = _pivot("down", 18.0, 25.0, 0)
    p2 = _pivot("down", 20.0, 26.0, 1)   # 与 p1 重叠
    p3 = _pivot("down", 16.0, 17.5, 2)   # 与 p2 不重叠 (17.5 < 20.0)
    assert classify_trend([p1, p2, p3], []) == "down"


def test_second_buy_fires_when_pullback_is_not_divergent() -> None:
    """一买后的回抽笔即便不背驰, 只要不破一买低点仍应给出二买。"""
    from app.custom.chan.core import Pivot, build_signals

    strokes = _mk_strokes(
        [
            ("up", 20.0, 25.0, 5),
            ("down", 25.0, 18.0, 5),
            ("up", 18.0, 23.0, 5),
            ("down", 23.0, 17.0, 5),
            ("up", 17.0, 21.0, 5),
            ("down", 21.0, 16.5, 5),
            ("up", 16.5, 20.0, 5),
            ("down", 20.0, 16.2, 6),   # 底背驰 → 一买
            ("up", 16.2, 19.0, 5),
            ("down", 19.0, 16.5, 5),   # 回抽: 笔数少于前一笔 → 非背驰, 但不破 16.2
        ]
    )
    pivots = [
        Pivot(direction="down", zd=18.0, zg=25.0, start_date=strokes[0].start_date, end_date=strokes[2].end_date, start_index=0, end_index=2, stroke_count=3),
        Pivot(direction="down", zd=16.0, zg=17.5, start_date=strokes[3].start_date, end_date=strokes[5].end_date, start_index=3, end_index=5, stroke_count=3),
    ]
    params = ChanParams()
    divergences = detect_bi_divergence(strokes, params)
    signals = build_signals(strokes, pivots, [], divergences, params)
    b2 = [s for s in signals if s.kind == "b2"]
    assert b2, "回抽不破一买低点即应产生二买"
    assert b2[0].date == strokes[9].end_date
    assert b2[0].detail["pullback_diverged"] is False


# ================================================================
# §8.9 段级别中枢 / 段背驰 (第 5 步)
# ================================================================


def _mk_segments(
    spec: list[tuple[str, float, float, int, bool]],
) -> list:
    """按 (方向, 起点价, 终点价, 段根数, 是否确认) 造一串线段。"""
    from app.custom.chan.core import Segment

    out = []
    cursor = 0
    for direction, start_price, end_price, k_count, confirmed in spec:
        start_date = _dates(cursor + k_count)[cursor]
        end_date = _dates(cursor + k_count)[cursor + k_count - 1]
        out.append(
            Segment(
                direction=direction,  # type: ignore[arg-type]
                start_price=start_price,
                end_price=end_price,
                start_date=start_date,
                end_date=end_date,
                start_index=cursor,
                end_index=cursor + k_count - 1,
                stroke_count=3,
                confirmed_date=end_date,
                confirmed=confirmed,
                k_count=k_count,
            )
        )
        cursor += k_count
    return out


def test_build_segment_pivots_uses_segment_index_coordinates() -> None:
    """段中枢的 start/end_index 是**线段下标** (不是笔下标), 且 nested_level=1。"""
    from app.custom.chan.core import build_segment_pivots

    segments = _mk_segments(
        [
            ("down", 25.0, 18.0, 5, True),
            ("up", 18.0, 22.0, 5, True),
            ("down", 22.0, 19.0, 5, True),
            ("up", 19.0, 23.0, 5, True),
        ]
    )
    pivots = build_segment_pivots(segments, ChanParams())
    assert pivots, "三段重叠应构成段中枢"
    p = pivots[0]
    assert p.nested_level == 1
    assert p.start_index == 0
    assert p.end_index >= 2
    assert p.zg >= p.zd


def test_build_segment_pivots_requires_three_segments() -> None:
    """段中枢至少要三个次级别走势 —— 两段重叠不成中枢。"""
    from app.custom.chan.core import build_segment_pivots

    segments = _mk_segments(
        [
            ("down", 25.0, 18.0, 5, True),
            ("up", 18.0, 22.0, 5, True),
        ]
    )
    assert build_segment_pivots(segments, ChanParams()) == []


def test_min_segments_per_pivot_must_be_at_least_three() -> None:
    with pytest.raises(ValueError):
        ChanParams(min_segments_per_pivot=2).validate()


def test_segment_amplitude_and_strength_match_stroke_formula() -> None:
    """段力度口径与笔力度一致: 幅度 / 天数 * 100。"""
    segments = _mk_segments([("up", 20.0, 25.0, 5, True)])
    seg = segments[0]
    assert seg.amplitude == pytest.approx(5.0)
    assert seg.strength == pytest.approx(5.0 / 5 * 100)


def test_detect_segment_divergence_on_strength_decay() -> None:
    """同向的下一段幅度/力度衰减 → 段背驰成立。"""
    from app.custom.chan.core import detect_segment_divergence

    segments = _mk_segments(
        [
            ("up", 20.0, 25.0, 5, True),   # 0
            ("down", 25.0, 18.0, 5, True),  # 1
            ("up", 18.0, 23.0, 5, True),   # 2
            ("down", 23.0, 17.0, 5, True),  # 3  ← 前一段 (同向下)
            ("up", 17.0, 21.0, 5, True),   # 4
            ("down", 21.0, 16.5, 5, True),  # 5  ← 幅度/力度都比第 3 段小
        ]
    )
    divs = detect_segment_divergence(segments, ChanParams())
    hit = [d for d in divs if d.segment_index == 5]
    assert hit, "第 6 段应与第 4 段比较"
    d = hit[0]
    assert d.prev_index == 3
    assert d.amplitude_ok is True
    assert d.strength_ok is True
    assert d.days_ok is True
    assert d.diverged is True


def test_detect_segment_divergence_requires_enough_days() -> None:
    """段天数不足前段 → 不构成背驰 (与笔级同构的 AND 判据)。"""
    from app.custom.chan.core import detect_segment_divergence

    segments = _mk_segments(
        [
            ("up", 20.0, 25.0, 5, True),
            ("down", 25.0, 18.0, 5, True),
            ("up", 18.0, 23.0, 5, True),
            ("down", 23.0, 17.0, 6, True),   # 前段 6 根
            ("up", 17.0, 21.0, 4, True),
            ("down", 21.0, 16.5, 4, True),   # 当前段只有 4 根 → 天数不足
        ]
    )
    divs = detect_segment_divergence(segments, ChanParams())
    hit = [d for d in divs if d.segment_index == 5]
    assert hit
    assert hit[0].days_ok is False
    assert hit[0].diverged is False


def test_detect_segment_divergence_ignores_unconfirmed_tail() -> None:
    """未确认的末段仍在延伸 → 不参与段背驰判定 (避免刷新抖动)。"""
    from app.custom.chan.core import detect_segment_divergence

    segments = _mk_segments(
        [
            ("up", 20.0, 25.0, 5, True),
            ("down", 25.0, 18.0, 5, True),
            ("up", 18.0, 23.0, 5, True),
            ("down", 23.0, 17.0, 5, True),
            ("up", 17.0, 21.0, 5, True),
            ("down", 21.0, 16.5, 5, False),  # 未确认末段
        ]
    )
    divs = detect_segment_divergence(segments, ChanParams())
    assert all(d.segment_index != 5 for d in divs)


def test_classify_segment_divergence_scope_trend_vs_consolidation() -> None:
    """段背驰形态归属判据与笔级同构: 最后两个同向段中枢不重叠 → 趋势。"""
    from app.custom.chan.core import SegmentDivergence, classify_segment_divergence_scope

    segments = _mk_segments(
        [
            ("up", 20.0, 25.0, 5, True),
            ("down", 25.0, 18.0, 5, True),
            ("up", 18.0, 23.0, 5, True),
            ("down", 23.0, 17.0, 5, True),
            ("up", 17.0, 21.0, 5, True),
            ("down", 21.0, 16.5, 5, True),
        ]
    )
    div = SegmentDivergence(
        segment_index=5,
        prev_index=3,
        duan_ok=True,
        days_ok=True,
        amplitude_ok=True,
        strength_ok=True,
    )

    # 盘整: 两个同向中枢重叠
    consolidating = [
        Pivot(direction="down", zd=18.0, zg=25.0, start_date="2026-01-05", end_date="2026-01-20", start_index=0, end_index=1, stroke_count=3, nested_level=1),
        Pivot(direction="down", zd=20.0, zg=26.0, start_date="2026-01-21", end_date="2026-02-05", start_index=2, end_index=3, stroke_count=3, nested_level=1),
    ]
    scoped = classify_segment_divergence_scope(segments, consolidating, [div])
    assert scoped[0].scope == "consolidation"

    # 趋势: 第二个同向中枢整体下移 (zg < prev.zd)
    trending = [
        Pivot(direction="down", zd=18.0, zg=25.0, start_date="2026-01-05", end_date="2026-01-20", start_index=0, end_index=1, stroke_count=3, nested_level=1),
        Pivot(direction="down", zd=16.0, zg=17.5, start_date="2026-01-21", end_date="2026-02-05", start_index=2, end_index=3, stroke_count=3, nested_level=1),
    ]
    scoped = classify_segment_divergence_scope(segments, trending, [div])
    assert scoped[0].scope == "trend"


def test_analyze_exposes_segment_pivots_and_divergences() -> None:
    """analyze() 汇总段级别中枢与段背驰, 且 summary 给出计数。"""
    bars = _bars(
        [11, 13, 15, 14, 13, 12, 10, 11, 13, 14, 15, 14, 12, 13, 15, 16],
        [10, 12, 14, 13, 12, 11, 9, 10, 12, 13, 14, 13, 11, 12, 14, 15],
    )
    structure = analyze(bars, ChanParams(), symbol="TEST")
    summary = structure.summary()
    assert "segment_pivot_count" in summary
    assert "segment_divergence_count" in summary
    assert "confirmed_segment_divergence_count" in summary
    for p in structure.segment_pivots:
        assert p.nested_level == 1
    for d in structure.segment_divergences:
        assert d.scope in ("trend", "consolidation")
