"""缠论 HTTP 接口契约测试 (docs/chan-theory-spec.md §5.2)。

用假的仓库句柄喂数据, 不依赖本机 data/ 目录 —— 保证 CI 可复现。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.extensions.contracts import ExtensionContext
from app.extensions.loader import configure_backend_extensions, start_backend_extensions


def _bars(n: int = 120) -> pl.DataFrame:
    """一段锯齿行情: 交替上涨/下跌, 便于构成笔与中枢。"""
    highs: list[float] = []
    lows: list[float] = []
    base = 10.0
    for i in range(n):
        step = 1.0 if (i // 5) % 2 == 0 else -1.0
        base = max(4.0, base + step * 0.4)
        highs.append(base + 0.5)
        lows.append(base - 0.5)
    dates = [
        (date(2026, 1, 5) + timedelta(days=i)).isoformat() for i in range(n)
    ]
    return pl.DataFrame(
        {
            "date": dates,
            "raw_high": highs,
            "raw_low": lows,
            "raw_close": highs,
        }
    )


class _FakeRepo:
    """最小仓库句柄: 只实现缠论接口用到的四个方法。"""

    def __init__(
        self,
        bars: pl.DataFrame,
        factors: pl.DataFrame | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._bars = bars
        self._factors = factors
        # 无股票 enriched 分区目录 → 详情页退回按标的自身最近 N 根取样
        self.store = SimpleNamespace(data_dir=data_dir or Path("__no_such_data_dir__"))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def enriched_latest_date(self) -> date:
        return date.fromisoformat(self._bars["date"][-1])

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        bars = self._bars.filter(
            (pl.col("date") >= start.isoformat()) & (pl.col("date") <= end.isoformat())
        )
        if columns:
            keep = [c for c in columns if c in bars.columns]
            return bars.select(keep)
        return bars

    def execute_all(self, sql: str, params: list | None = None) -> list[tuple]:
        if self._factors is None:
            return []
        return [
            (row["trade_date"], row["ex_factor"])
            for row in self._factors.iter_rows(named=True)
        ]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert not errors, errors
    context = ExtensionContext(
        api_version=1,
        data_dir=__import__("pathlib").Path("."),
        repository=_FakeRepo(_bars()),
    )
    start_backend_extensions(context, registry)
    return TestClient(app)


def test_structure_returns_core_contract(client: TestClient) -> None:
    resp = client.get("/api/chan/structure", params={"symbol": "000001.SZ", "days": 120})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "000001.SZ"
    assert body["price_mode"] == "qfq"
    assert body["level"] == "1d"
    assert body["adj_anchor_date"] == body["dates"][-1]  # 默认锚点 = 样本末日, 必须回显
    assert len(body["dates"]) == len(body["closes"]) == len(body["raw_closes"])
    assert body["summary"]["status"] == "ok"
    assert body["summary"]["bar_count"] == len(body["dates"])
    for key in ("fractals", "strokes", "segments", "pivots", "signals"):
        assert key in body


def test_structure_stroke_payload_has_amplitude_and_strength(client: TestClient) -> None:
    body = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 120}
    ).json()
    for stroke in body["strokes"]:
        assert stroke["amplitude"] >= 0
        assert stroke["strength"] >= 0
        if stroke["k_count"] > 0:
            expected = stroke["amplitude"] / stroke["k_count"] * 100
            assert stroke["strength"] == pytest.approx(expected, rel=1e-3)


def test_unimplemented_level_returns_501(client: TestClient) -> None:
    resp = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "level": "30m"}
    )
    assert resp.status_code == 501
    assert "30m" in resp.json()["detail"]


def test_invalid_price_mode_returns_400(client: TestClient) -> None:
    resp = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "price_mode": "xxx"}
    )
    assert resp.status_code == 400


def test_invalid_anchor_date_returns_400(client: TestClient) -> None:
    resp = client.get(
        "/api/chan/structure",
        params={"symbol": "000001.SZ", "adj_anchor_date": "not-a-date"},
    )
    assert resp.status_code == 400


def test_include_can_trim_payload(client: TestClient) -> None:
    body = client.get(
        "/api/chan/structure",
        params={"symbol": "000001.SZ", "days": 120, "include": "signals"},
    ).json()
    assert "signals" in body
    assert "strokes" not in body


def test_insufficient_data_is_flagged_not_silently_empty() -> None:
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert not errors, errors
    context = ExtensionContext(
        api_version=1,
        data_dir=__import__("pathlib").Path("."),
        repository=_FakeRepo(_bars(5)),
    )
    start_backend_extensions(context, registry)
    body = TestClient(app).get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 30}
    ).json()
    assert body["summary"]["status"] == "insufficient_data"


def test_missing_symbol_data_returns_404() -> None:
    class _EmptyRepo(_FakeRepo):
        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            return pl.DataFrame()

    app = FastAPI()
    registry, _ = configure_backend_extensions(app)
    context = ExtensionContext(
        api_version=1,
        data_dir=__import__("pathlib").Path("."),
        repository=_EmptyRepo(_bars()),
    )
    start_backend_extensions(context, registry)
    resp = TestClient(app).get(
        "/api/chan/structure", params={"symbol": "000001.SZ"}
    )
    assert resp.status_code == 404


def test_segment_level_payload_contract(client: TestClient) -> None:
    """段级中枢 / 段背驰 的序列化契约 (第 5 步)。"""
    body = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 120}
    ).json()

    assert "segment_pivots" in body
    assert "segment_divergences" in body
    for pivot in body["segment_pivots"]:
        assert pivot["nested_level"] == 1
        assert pivot["zg"] >= pivot["zd"]
        assert pivot["start_date"] <= pivot["end_date"]

    for div in body["segment_divergences"]:
        assert div["scope"] in ("trend", "consolidation")
        assert "segment_index" in div
        assert "detail" in div
        detail = div["detail"]
        assert detail["cur_days"] >= 0
        assert detail["cur_strength"] >= 0


def test_segment_payload_has_k_count_amplitude_strength(client: TestClient) -> None:
    body = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 120}
    ).json()
    for seg in body["segments"]:
        assert seg["amplitude"] >= 0
        if seg["k_count"] > 0:
            expected = seg["amplitude"] / seg["k_count"] * 100
            assert seg["strength"] == pytest.approx(expected, rel=1e-3)
        assert "confirmed" in seg


def test_summary_exposes_segment_counts(client: TestClient) -> None:
    body = client.get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 120}
    ).json()
    summary = body["summary"]
    assert summary["segment_pivot_count"] == len(body["segment_pivots"])
    assert summary["segment_divergence_count"] == len(body["segment_divergences"])
    assert summary["confirmed_segment_divergence_count"] == sum(
        1 for d in body["segment_divergences"] if d["diverged"]
    )


def test_screen_rejects_unknown_signal_kind(client: TestClient) -> None:
    """非法条件在扫描前就被拒 (400), 不会跑起全市场扫描。"""
    resp = client.post(
        "/api/chan/screen",
        json={"days": 120, "signal_kinds": ["b9"]},
    )
    assert resp.status_code == 400
    assert "b9" in resp.json()["detail"]


def test_screen_rejects_out_of_range_days(client: TestClient) -> None:
    resp = client.post("/api/chan/screen", json={"days": 5})
    assert resp.status_code == 422


def _client_for(repo) -> TestClient:
    app = FastAPI()
    registry, _ = configure_backend_extensions(app)
    context = ExtensionContext(api_version=1, data_dir=Path("."), repository=repo)
    start_backend_extensions(context, registry)
    return TestClient(app)


def _market_partitions(root: Path, dates: list[str], empty: set[str] = frozenset()) -> Path:
    """按 enriched 布局造 date= 分区; empty 中的日期只建目录不放 parquet。"""
    enriched = root / "kline_daily_enriched"
    for d in dates:
        part = enriched / f"date={d}"
        part.mkdir(parents=True)
        if d not in empty:
            (part / "part-0.parquet").write_bytes(b"")
    return root


def test_structure_window_matches_market_window_for_suspended_stock(tmp_path) -> None:
    """详情页与选股器同用「全市场最近 N 个交易日」: 停牌股起点不得因停牌往前漂。"""
    from app.custom.chan.screener import market_window_dates

    full = _bars(140)
    market_dates = full["date"].to_list()
    suspended = set(market_dates[100:110])  # 窗口内停牌 10 天
    stock_bars = full.filter(~pl.col("date").is_in(list(suspended)))
    data_dir = _market_partitions(tmp_path, market_dates)

    window = market_window_dates(data_dir / "kline_daily_enriched", 100)
    assert window == market_dates[-100:]

    body = _client_for(_FakeRepo(stock_bars, data_dir=data_dir)).get(
        "/api/chan/structure", params={"symbol": "000001.SZ", "days": 100}
    ).json()
    assert body["dates"][0] == window[0]
    assert body["dates"][-1] == window[-1]
    assert len(body["dates"]) == 90  # 100 个交易日 - 10 天停牌


def test_market_window_skips_partitions_without_parquet(tmp_path) -> None:
    from app.custom.chan.screener import market_window_dates

    dates = [f"2026-03-{d:02d}" for d in range(1, 11)]
    data_dir = _market_partitions(tmp_path, dates, empty={"2026-03-10", "2026-03-05"})
    assert market_window_dates(data_dir / "kline_daily_enriched", 3) == [
        "2026-03-07", "2026-03-08", "2026-03-09",
    ]
    assert market_window_dates(tmp_path / "missing", 3) == []
