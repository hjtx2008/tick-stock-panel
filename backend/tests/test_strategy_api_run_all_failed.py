"""/api/strategies/run-all: 单策略失败必须带出 error, 不能被读成「0 命中」。

engine.run_all 把失败策略隔离成 total=0 的占位; 端点若只输出 total,
调用方无法区分「策略跑挂了」和「今天确实没有命中」。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import strategy as strategy_api
from app.services import screener as screener_service
from app.strategy.engine import StrategyDataContext, StrategyEngine

_OK = '''
import polars as pl
META = {"id": "ok_one", "name": "ok_one", "timeframes": ["1d"], "asset_types": ["stock"]}
def filter(df, params):
    return pl.col("close") > 10
'''
_BOOM = '''
META = {"id": "boom_one", "name": "boom_one", "timeframes": ["1d"], "asset_types": ["stock"]}
def filter(df, params):
    raise ValueError("injected failure")
'''


def _context() -> StrategyDataContext:
    n = 5
    return StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 9, 7),
        current=pl.DataFrame({
            "symbol": [f"{i:06d}.SZ" for i in range(n)],
            "name": [f"股票{i}" for i in range(n)],
            "open": [9.0 + i for i in range(n)],
            "high": [9.5 + i for i in range(n)],
            "low": [8.5 + i for i in range(n)],
            "close": [9.0 + i for i in range(n)],
            "volume": [1000.0] * n,
            "amount": [9000.0] * n,
            "turnover_rate": [1.0] * n,
            "total_shares": [1e8] * n,
            "float_shares": [5e7] * n,
        }),
    )


class _FakeService:
    def __init__(self, repo, asset_type="stock"):
        pass

    def build_strategy_context(self, *args, **kwargs):
        return _context()


def test_run_all_reports_failed_strategy(monkeypatch, tmp_path):
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "ok_one.py").write_text(_OK, encoding="utf-8")
    (d / "boom_one.py").write_text(_BOOM, encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[d])
    monkeypatch.setattr(screener_service, "ScreenerService", _FakeService)
    monkeypatch.setattr(
        strategy_api.strategy_config,
        "list_overrides",
        lambda _dir: {
            "ok_one": {"basic_filter": {"enabled": False}},
            "boom_one": {"basic_filter": {"enabled": False}},
        },
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))

    resp = strategy_api.run_all(strategy_api.RunAllRequest(as_of="2026-09-07"), request)

    assert resp["results"]["ok_one"]["total"] == 3  # close 9..13 中 > 10 的 3 只
    assert "error" not in resp["results"]["ok_one"]
    assert resp["results"]["boom_one"]["total"] == 0
    assert "injected failure" in resp["results"]["boom_one"]["error"]
    assert set(resp["failed"]) == {"boom_one"}
