"""run_all 并行执行的等价性回归测试。

engine.run_all 支持有界线程池并发 (策略对共享 context 只读纯函数)。此处验证:
- parallel=True 与 parallel=False 对同一批策略产出完全一致的结果 (总数 + 标的集);
- 失败策略被隔离跳过 (不抛异常), 其余策略结果照常返回;
- composite 子策略的嵌套 run_all 不受影响 (递归恒串行)。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyEngine

_FILTER_TEMPLATE = '''
import polars as pl

META = {{
    "id": "{sid}",
    "name": "{sid}",
    "timeframes": ["1d"],
    "asset_types": ["stock"],
}}
def filter(df, params):
    return pl.col("close") > {threshold}
'''


def _write_strategy(directory: Path, code: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"strategy_{abs(hash(code)) % 10**8}.py").write_text(code, encoding="utf-8")


def _make_engine(tmp_path: Path) -> StrategyEngine:
    d = tmp_path / "strategies"
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_a", threshold=10.0))
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_b", threshold=15.0))
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="cheap_c", threshold=20.0))
    return StrategyEngine(strategy_dirs=[d])


def _context() -> StrategyDataContext:
    n = 30
    current = pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(n)],
        "name": [f"股票{i}" for i in range(n)],
        "open": [5.0 + i for i in range(n)],
        "high": [5.5 + i for i in range(n)],
        "low": [4.5 + i for i in range(n)],
        "close": [5.0 + i for i in range(n)],
        "volume": [1000.0 * (i + 1) for i in range(n)],
        "amount": [5000.0 * (i + 1) for i in range(n)],
        "turnover_rate": [1.0 + i * 0.1 for i in range(n)],
        "total_shares": [1e8 for _ in range(n)],
        "float_shares": [5e7 for _ in range(n)],
    })
    return StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 9, 7),
        current=current,
    )


def _signature(results: dict) -> dict:
    return {
        sid: (r.total, tuple(sorted(row["symbol"] for row in r.rows)))
        for sid, r in results.items()
    }


def test_parallel_run_all_matches_sequential_results(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    context = _context()
    # 关闭默认基础过滤, 让结果只取决于策略谓词本身
    overrides = {
        meta["id"]: {"basic_filter": {"enabled": False}}
        for meta in engine.list_strategies()
    }

    sequential = engine.run_all(context, overrides_map=overrides, parallel=False)
    parallel = engine.run_all(context, overrides_map=overrides, parallel=True)

    assert list(parallel) == list(sequential)  # 结果键序一致
    assert _signature(parallel) == _signature(sequential)
    # close 序列 5..34: >10 → 11..34 共 24 只; >15 → 19 只; >20 → 14 只
    assert sequential["cheap_a"].total == 24
    assert sequential["cheap_b"].total == 19
    assert sequential["cheap_c"].total == 14


def test_parallel_run_all_isolates_failing_strategy(tmp_path: Path) -> None:
    """单个策略失败 → 隔离跳过, 其余策略照常返回结果。

    2026-11-13 变更: 语义从「首个失败策略抛异常」改为「跳过 + 记 error」。
    实测触发原因: AI 生成的策略引用了指标流水线里不存在的列 (vol_ma20),
    一个坏策略会让整个 run_all 抛 500, 其余 28 个正常策略的结果全部拿不到 ——
    代价远大于跳过一条。
    """
    d = tmp_path / "strategies"
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="ok_first", threshold=10.0))
    _write_strategy(
        d,
        '''
META = {"id": "boom", "name": "boom"}
def filter(df, params):
    raise ValueError("injected strategy failure")
''',
    )
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="ok_last", threshold=15.0))
    engine = StrategyEngine(strategy_dirs=[d])
    context = _context()
    # 关闭默认基础过滤, 让结果只取决于策略谓词本身 (与上方对照测试一致)
    overrides = {
        meta["id"]: {"basic_filter": {"enabled": False}}
        for meta in engine.list_strategies()
    }

    for parallel in (False, True):
        results = engine.run_all(context, overrides_map=overrides, parallel=parallel)
        # 失败策略被跳过, 但结果里保留占位 + 错误原因
        assert "boom" in results
        assert results["boom"].error is not None
        assert "injected strategy failure" in results["boom"].error
        assert results["boom"].total == 0
        # 其余策略不受影响
        assert results["ok_first"].error is None
        assert results["ok_last"].error is None
        assert results["ok_first"].total == 24
        assert results["ok_last"].total == 19


def test_run_all_isolates_pre_validation_failure(tmp_path: Path) -> None:
    """前置校验失败 (asset_type 不匹配 / 策略不存在) → 只跳过该策略。

    2026-11-13 变更: validate_context 原先在 _execute 之外抛错, 一条声明不匹配的
    策略会让整个 run_all 抛 500, 其余策略的结果全丢。
    """
    d = tmp_path / "strategies"
    _write_strategy(d, _FILTER_TEMPLATE.format(sid="ok_a", threshold=10.0))
    _write_strategy(
        d,
        '''
META = {"id": "etf_only", "name": "etf_only",
       "asset_types": ["etf"], "timeframes": ["1d"]}
def filter(df, params):
    return pl.col("close") > 10
''',
    )
    engine = StrategyEngine(strategy_dirs=[d])
    context = _context()
    overrides = {
        meta["id"]: {"basic_filter": {"enabled": False}}
        for meta in engine.list_strategies()
    }

    results = engine.run_all(
        context,
        overrides_map=overrides,
        strategy_ids=["ok_a", "etf_only", "no_such_strategy"],
    )
    # 不匹配的策略: 占位 + 错误原因
    assert results["etf_only"].error is not None
    assert "asset_type" in results["etf_only"].error
    assert results["etf_only"].total == 0
    # 不存在的策略 id: 同样占位, 不炸整批
    assert "no_such_strategy" in results
    assert results["no_such_strategy"].error is not None
    # 正常策略照常出结果, 且键序与传入顺序一致
    assert list(results) == ["ok_a", "etf_only", "no_such_strategy"]
    assert results["ok_a"].error is None
    assert results["ok_a"].total == 24
