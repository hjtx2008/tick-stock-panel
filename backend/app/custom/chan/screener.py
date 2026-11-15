"""缠论选股器 (第 5 步)。

在全市场 (或指定股票池) 上跑缠论结构分析, 按条件筛出标的。

口径与详情页一致
----------------
详情页 ``/api/chan/structure`` 默认前复权 (qfq), 锚点 = 样本末日::

    qfq(D) = raw(D) * cum(D) / cum(样本末日)

选股器必须给出**同样的**结构结论, 否则「选股器选出来的票」和「点进去看到的图」
会打架。但逐标的调用 ``apply_price_mode`` 是全市场扫描的头号耗时 (每标的一次
polars join_asof + collect)。选股器改成**全市场一次性**算复权:

1. 除权事件按 symbol 做 ``cum_prod`` → ``cum(D)`` (全历史口径, 与逐标的一致);
2. 一次 ``join_asof`` 把 ``cum(D)`` 铺到全市场每个 (symbol, date);
3. 逐标的除以自己的锚点因子 ``cum(该标的样本末日)`` → qfq。

第 3 步是逐标的的**常数**缩放 —— 缠论内核只依赖同标的内的相对关系 (笔/线段/
中枢/背驰全部是同标的比较), 所以常数缩放不改变任何结构结论, 却让
「锚点 = 样本末日」的口径得以精确复现。

并行
----
缠论内核是纯 Python (受 GIL 限制), 单线程全市场 5584 只 / 250 根约 37s。
选股器按标的分块写成临时 parquet, 用 ``ProcessPoolExecutor`` 并行计算, 实测约 5s;
进程池不可用时自动退回单进程 (结论不变, 只是慢)。
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from app.custom.chan.core import ChanLevel, ChanParams, analyze

logger = logging.getLogger(__name__)

VALID_SIGNAL_KINDS = ("b1", "b2", "b3", "s1", "s2", "s3")
VALID_TRENDS = ("up", "down", "oscillation")
VALID_SCOPES = ("trend", "consolidation")

_MAX_WORKERS = 8
_MAX_CHUNK_ROWS = 400_000


# ================================================================
# 查询模型
# ================================================================


@dataclass(frozen=True)
class ChanScreenConditions:
    """筛选条件 (全部为「与」关系; 留空 = 该维度不限)。"""

    signal_kinds: tuple[str, ...] = ()
    """命中的买卖点类型 (b1/b2/b3/s1/s2/s3)。空 = 不限。"""

    trend: tuple[str, ...] = ()
    """走势方向 (up/down/oscillation)。空 = 不限。"""

    divergence_scope: tuple[str, ...] = ()
    """笔背驰形态 (trend=趋势背驰 / consolidation=盘整背驰)。空 = 不限。"""

    require_divergence: bool = False
    """必须存在**确认**的笔背驰。"""

    require_segment_divergence: bool = False
    """必须存在**确认**的段背驰。"""

    require_segment_pivot: bool = False
    """必须存在段级中枢。"""

    max_signal_age: int = 0
    """买卖点距样本末日不超过 N 根 K 线; 0 = 不限。"""

    def validate(self) -> None:
        for kind in self.signal_kinds:
            if kind not in VALID_SIGNAL_KINDS:
                raise ValueError(f"未知买卖点类型: {kind}")
        for trend in self.trend:
            if trend not in VALID_TRENDS:
                raise ValueError(f"未知走势方向: {trend}")
        for scope in self.divergence_scope:
            if scope not in VALID_SCOPES:
                raise ValueError(f"未知背驰形态: {scope}")
        if self.max_signal_age < 0:
            raise ValueError("max_signal_age 不能为负")


@dataclass(frozen=True)
class ChanScreenQuery:
    """一次选股请求。"""

    days: int = 250
    """样本长度 (交易日)。"""

    symbols: tuple[str, ...] | None = None
    """股票池; None = 全市场 (股票)。"""

    conditions: ChanScreenConditions = field(default_factory=ChanScreenConditions)
    limit: int = 200
    """返回条数上限 (排序后取前 N)。"""


# ================================================================
# 复权预计算 (全市场一次性)
# ================================================================


def build_factor_cum(factors: pl.DataFrame) -> pl.DataFrame:
    """由除权事件表算每标的的累积因子 ``cum(D) = Π ex_factor(事件日 <= D)``。

    ``factors`` 需含 ``symbol`` / ``trade_date`` / ``ex_factor`` 三列。
    累积在**全量事件表**上计算, 与样本区间无关 —— 与逐标的
    ``core._factor_cum_series`` 口径完全一致。
    """
    if factors is None or factors.is_empty():
        return pl.DataFrame(
            schema={"symbol": pl.Utf8, "trade_date": pl.Date, "cum": pl.Float64}
        )
    f = factors.select(["symbol", "trade_date", "ex_factor"]).with_columns(
        pl.col("ex_factor").cast(pl.Float64, strict=False)
    ).drop_nulls()
    if f.is_empty():
        return pl.DataFrame(
            schema={"symbol": pl.Utf8, "trade_date": pl.Date, "cum": pl.Float64}
        )
    return (
        f.sort(["symbol", "trade_date"])
        .with_columns(pl.col("ex_factor").cum_prod().over("symbol").alias("cum"))
        .select(["symbol", "trade_date", "cum"])
    )


def build_qfq_frame(
    bars: pl.DataFrame,
    factor_cum: pl.DataFrame,
) -> pl.DataFrame:
    """把全市场原始价换算成前复权价 (锚点 = 各标的样本末日)。

    ``bars`` 需含 ``symbol`` / ``date`` / ``raw_high`` / ``raw_low`` / ``raw_close``。
    返回列: ``symbol`` / ``date`` / ``high`` / ``low`` / ``close`` (前复权) /
    ``close_raw`` (原始收盘价, 供选股列表显示现价)。
    """
    if bars is None or bars.is_empty():
        raise ValueError("bars 为空")

    out = bars.select(
        [
            "symbol",
            "date",
            pl.col("raw_high").alias("high"),
            pl.col("raw_low").alias("low"),
            pl.col("raw_close").alias("close_raw"),
        ]
    ).sort(["symbol", "date"])

    if factor_cum is None or factor_cum.is_empty():
        # 无因子 → 退化为原始价 (与 core 的 adj_factor_missing 分支一致)
        out = out.with_columns(
            [
                pl.col("high").alias("high_adj"),
                pl.col("low").alias("low_adj"),
                pl.col("close_raw").alias("close_adj"),
            ]
        )
    else:
        out = out.join_asof(
            factor_cum,
            left_on="date",
            right_on="trade_date",
            by="symbol",
            strategy="backward",
        ).with_columns(pl.col("cum").fill_null(1.0).alias("cum"))
        # 锚点 = 该标的样本末日的累积因子
        out = out.with_columns(
            pl.col("cum").last().over("symbol").alias("cum_anchor")
        ).with_columns(
            pl.when(pl.col("cum_anchor") <= 0)
            .then(1.0)
            .otherwise(pl.col("cum") / pl.col("cum_anchor"))
            .alias("ratio")
        )
        out = out.with_columns(
            [
                (pl.col("high") * pl.col("ratio")).alias("high_adj"),
                (pl.col("low") * pl.col("ratio")).alias("low_adj"),
                (pl.col("close_raw") * pl.col("ratio")).alias("close_adj"),
            ]
        )

    return out.select(
        [
            "symbol",
            "date",
            pl.col("high_adj").alias("high"),
            pl.col("low_adj").alias("low"),
            pl.col("close_adj").alias("close"),
            "close_raw",
        ]
    ).sort(["symbol", "date"])


# ================================================================
# 单标的结构摘要
# ================================================================


def summarize_structure(symbol: str, structure, close_raw: float) -> dict:
    """把一个标的的结构压成紧凑摘要 (供选股列表 / 条件判定使用)。"""
    return {
        "symbol": symbol,
        "trend": structure.trend,
        "status": structure.status,
        "last_date": structure.dates[-1] if structure.dates else None,
        "last_close": round(float(close_raw), 4),
        "bar_count": len(structure.dates),
        "stroke_count": len(structure.strokes),
        "segment_count": len(structure.segments),
        "pivot_count": len(structure.pivots),
        "segment_pivot_count": len(structure.segment_pivots),
        "signals": [
            {
                "kind": s.kind,
                "label": s.label,
                "date": s.date,
                "price": round(s.price, 4),
                "bar_index": s.bar_index,
            }
            for s in structure.signals
        ],
        "divergences": [
            {
                "date": structure.strokes[d.stroke_index].end.date,
                "scope": d.scope,
                "diverged": d.diverged,
                "direction": structure.strokes[d.stroke_index].direction,
            }
            for d in structure.divergences
        ],
        "segment_divergences": [
            {
                "date": structure.segments[d.segment_index].end_date,
                "scope": d.scope,
                "diverged": d.diverged,
                "direction": structure.segments[d.segment_index].direction,
            }
            for d in structure.segment_divergences
        ],
    }


def _scan_symbols(bars: pl.DataFrame) -> list[dict]:
    """对一批标的算结构摘要 (bars 已是前复权价, 列: symbol/date/high/low/close/close_raw)。"""
    params = ChanParams(level=ChanLevel.DAY, price_mode="raw")
    out: list[dict] = []
    for key, g in bars.partition_by("symbol", as_dict=True, maintain_order=True).items():
        symbol = key[0] if isinstance(key, tuple) else key
        g = g.sort("date")
        close_raw = float(g["close_raw"][-1])
        try:
            structure = analyze(
                g.select(["date", "high", "low", "close"]), params, symbol=symbol
            )
        except ValueError:
            # 数据不足等 → 跳过该标的 (不返回空结构冒充成功)
            continue
        out.append(summarize_structure(symbol, structure, close_raw))
    return out


def _scan_chunk(chunk_path: str) -> list[dict]:
    """子进程入口: 读一个分块文件, 逐标的算结构摘要。

    必须是模块级函数 (可 pickle), 且不能依赖仓库全局状态。
    """
    df = pl.read_parquet(chunk_path)
    return _scan_symbols(df)


# ================================================================
# 条件判定
# ================================================================


def match_conditions(summary: dict, conditions: ChanScreenConditions) -> bool:
    """判断一个标的摘要是否满足全部条件。"""
    if conditions.trend and summary["trend"] not in conditions.trend:
        return False

    signals = summary["signals"]
    if conditions.signal_kinds and not any(
        s["kind"] in conditions.signal_kinds for s in signals
    ):
        return False

    if conditions.max_signal_age > 0:
        bar_count = summary["bar_count"]
        # 新鲜度针对**被条件命中的那类信号** —— 指定了 signal_kinds 时,
        # 不能拿一个"无关但很新"的信号来顶替 (否则 b1 + 10根 会被 s3 顶掉)。
        candidates = [
            s
            for s in signals
            if not conditions.signal_kinds or s["kind"] in conditions.signal_kinds
        ]
        recent = [
            s for s in candidates if bar_count - 1 - s["bar_index"] <= conditions.max_signal_age
        ]
        if not recent:
            return False

    divergences = summary["divergences"]
    if conditions.require_divergence and not any(d["diverged"] for d in divergences):
        return False
    if conditions.divergence_scope and not any(
        d["diverged"] and d["scope"] in conditions.divergence_scope for d in divergences
    ):
        return False

    segment_divergences = summary["segment_divergences"]
    if conditions.require_segment_divergence and not any(
        d["diverged"] for d in segment_divergences
    ):
        return False

    return not (conditions.require_segment_pivot and summary["segment_pivot_count"] <= 0)


def _sort_key(summary: dict) -> tuple:
    """排序: 最近出现的买卖点优先, 其次信号多者优先, 最后按代码。"""
    signals = summary["signals"]
    latest = max((s["bar_index"] for s in signals), default=-1)
    return (-latest, -len(signals), summary["symbol"])


# ================================================================
# 全市场扫描
# ================================================================


def scan_frame(
    bars: pl.DataFrame,
    factors: pl.DataFrame,
    query: ChanScreenQuery,
    max_workers: int | None = None,
) -> dict:
    """在一批标的的原始日 K 上跑选股。

    ``bars`` 需含 ``symbol`` / ``date`` / ``raw_high`` / ``raw_low`` / ``raw_close``;
    ``factors`` 需含 ``symbol`` / ``trade_date`` / ``ex_factor``。
    """
    started = time.perf_counter()
    query.conditions.validate()
    if query.days < 30:
        raise ValueError("days 至少为 30")

    frame = bars
    if query.symbols:
        frame = frame.filter(pl.col("symbol").is_in(list(query.symbols)))
    if frame.is_empty():
        return {
            "matches": [],
            "scanned": 0,
            "total_matched": 0,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    factor_cum = build_factor_cum(factors)
    qfq = build_qfq_frame(frame, factor_cum)

    summaries = _run_parallel(qfq, max_workers=max_workers)

    matched = [s for s in summaries if match_conditions(s, query.conditions)]
    matched.sort(key=_sort_key)
    return {
        "matches": matched[: query.limit],
        "scanned": len(summaries),
        "total_matched": len(matched),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def _run_parallel(qfq: pl.DataFrame, max_workers: int | None = None) -> list[dict]:
    """按标的分块并行计算结构摘要; 进程池不可用时退回单进程。"""
    symbols = qfq["symbol"].unique().sort().to_list()
    if not symbols:
        return []

    workers = max_workers or min(_MAX_WORKERS, max(1, (os.cpu_count() or 2) - 1), len(symbols))
    if workers <= 1 or len(symbols) < 32:
        return _scan_symbols(qfq)

    tmpdir = Path(tempfile.mkdtemp(prefix="chan_scan_"))
    try:
        chunks = _write_chunks(qfq, symbols, workers, tmpdir)
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_scan_chunk, [str(p) for p in chunks]))
        except Exception as exc:  # 进程池不可用时退回单进程 (结论不变, 只是慢)
            logger.warning("缠论选股并行失败, 退回单进程: %s", exc)
            return _scan_symbols(qfq)
        out: list[dict] = []
        for part in results:
            out.extend(part)
        return out
    finally:
        for f in tmpdir.glob("*.parquet"):
            with contextlib.suppress(OSError):
                f.unlink()
        with contextlib.suppress(OSError):
            tmpdir.rmdir()


def _write_chunks(
    qfq: pl.DataFrame,
    symbols: list[str],
    workers: int,
    tmpdir: Path,
) -> list[Path]:
    """把前复权价按标的分块写成临时 parquet (子进程各自读自己那块)。"""
    paths: list[Path] = []
    groups = qfq.partition_by("symbol", as_dict=True, maintain_order=True)
    buckets: list[list] = [[] for _ in range(workers)]
    for i, (_key, g) in enumerate(groups.items()):
        buckets[i % workers].append(g)
    for i, frames in enumerate(buckets):
        if not frames:
            continue
        path = tmpdir / f"chunk_{i}.parquet"
        pl.concat(frames).write_parquet(path)
        paths.append(path)
    return paths


def market_window_dates(enriched_dir: Path, days: int) -> list[str]:
    """全市场最近 ``days`` 个交易日 (升序, ``YYYY-MM-DD``), 选股器与详情页共用的样本窗口。

    以股票 enriched 的 ``date=`` 分区为交易日历, 只计含 parquet 文件的分区。
    详情页若按单标的自己的最近 N 根取样, 停牌股的起点会更早, 分型/笔路径随之改变,
    选出来的票与点进去看到的结构不一致。目录不存在时返回空列表。
    """
    if days <= 0 or not enriched_dir.is_dir():
        return []
    partitions = sorted(
        (
            entry
            for entry in os.scandir(enriched_dir)
            if entry.name.startswith("date=") and entry.is_dir()
        ),
        key=lambda e: e.name,
        reverse=True,
    )
    picked: list[str] = []
    for entry in partitions:
        if any(f.name.endswith(".parquet") for f in os.scandir(entry.path)):
            picked.append(entry.name.split("=", 1)[1])
            if len(picked) >= days:
                break
    picked.reverse()
    return picked


def scan_market(repo, query: ChanScreenQuery) -> dict:
    """从仓库数据源做全市场 (或指定股票池) 选股。

    日 K 走 enriched parquet (与仓库其它服务同一份数据), 复权因子走 DuckDB
    视图 ``adj_factor`` —— 不另建数据通道。
    """
    from app.parquet import scan_enriched_parquet

    started = time.perf_counter()
    data_dir = repo.store.data_dir
    enriched_dir = data_dir / "kline_daily_enriched"

    # enriched parquet 按 date=YYYY-MM-DD 分区。
    # 两种实测 (8763 个分区 / 250 个交易日 / 5584 只):
    #   - 全表 distinct 取交易日: 12.4s
    #   - Path.glob 列出全部分区: 3.5s
    #   - os.scandir 列分区 + 显式文件清单: 0.2s, collect 0.6s
    # 所以这里用 os.scandir + 显式文件清单, 不走全表扫。
    use_dates = market_window_dates(enriched_dir, query.days)
    if not use_dates:
        raise ValueError("日线数据为空")

    files: list[str] = []
    for day in use_dates:
        day_dir = enriched_dir / f"date={day}"
        for entry in os.scandir(day_dir):
            if entry.name.endswith(".parquet"):
                files.append(entry.path)
    if not files:
        raise ValueError("日线数据为空")

    lf = scan_enriched_parquet(files)
    bars = (
        lf.select(["symbol", "date", "raw_high", "raw_low", "raw_close"])
        .collect()
        .sort(["symbol", "date"])
    )
    if bars.is_empty():
        raise ValueError("日线数据为空")

    # 分区名只是"有哪些天", 实际有数据的交易日以帧内为准 → 再裁剪到最近 days 天
    actual_dates = bars["date"].unique().sort().to_list()
    if len(actual_dates) > query.days:
        bars = bars.filter(pl.col("date") >= actual_dates[-query.days])
        use_dates = actual_dates[-query.days :]

    rows = repo.execute_all("SELECT symbol, trade_date, ex_factor FROM adj_factor")
    factors = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trade_date": [r[1] for r in rows],
            "ex_factor": [float(r[2]) for r in rows],
        },
        schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64},
    )

    result = scan_frame(bars, factors, query)
    result["as_of"] = use_dates[-1] if isinstance(use_dates[-1], str) else use_dates[-1].isoformat()
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


__all__ = [
    "ChanScreenConditions",
    "ChanScreenQuery",
    "build_factor_cum",
    "build_qfq_frame",
    "match_conditions",
    "scan_frame",
    "scan_market",
    "summarize_structure",
]
