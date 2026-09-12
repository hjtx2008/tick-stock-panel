"""TushareProvider 契约与单位标准化测试。

不依赖真实网络与 Token: 用假 TushareClient 返回样例响应, 验证
  - 单位口径 (CONTRIBUTING §3.1): amount 千元→元 乘 1000、vol 手不转换
  - 字段映射与日期归一 (YYYYMMDD / int / YYYY-MM-DD 三种形态)
  - 取数模式分流 (全市场走交易日历、少量标的走逐标的、日历失败自动回退)
  - 除权因子: 官方累积因子序列 → 单事件 pre/post 比值 (含基准日/窗口过滤)
  - 能力声明 (未声明数据集回退现有源) 与设置页试拉
  - 失败处理: 少量失败告警继续, 过半失败抛错避免半截数据落库
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.plugins.tushare import client as tc
from app.plugins.tushare import provider as tp
from app.plugins.tushare.provider import TushareProvider


class _FakeClient:
    """记录调用的假客户端。构造参数与真实 TushareClient 对齐 (api_key 必填)。"""

    def __init__(
        self,
        *,
        trade_days: list[date] | None = None,
        daily_by_date: dict[str, list[dict]] | None = None,
        daily_by_symbol: dict[str, list[dict]] | None = None,
        adj_by_date: dict[str, list[dict]] | None = None,
        adj_by_symbol: dict[str, list[dict]] | None = None,
        fail_on: set[str] | None = None,
        probe_rows: list[dict] | None = None,
    ):
        self.trade_days_result = trade_days
        self.daily_by_date_map = daily_by_date or {}
        self.daily_by_symbol_map = daily_by_symbol or {}
        self.adj_by_date_map = adj_by_date or {}
        self.adj_by_symbol_map = adj_by_symbol or {}
        self.fail_on = fail_on or set()
        self.probe_rows = probe_rows if probe_rows is not None else [{"ts_code": "000001.SZ"}]
        self.calls: list[tuple] = []
        # ---- 财务 ----
        self.fina_indicator_rows: list[dict] = []
        self.statement_rows: dict[str, list[dict]] = {}      # api_name -> rows
        self.daily_basic_by_date_map: dict[str, list[dict]] = {}
        self.fina_batch_sizes: list[int] = []                # 记录每批请求标的数

    def fina_indicator_batch(self, ts_codes, start, end):
        self.calls.append(("fina_indicator_batch", tuple(ts_codes), start, end))
        self.fina_batch_sizes.append(len(ts_codes))
        self._maybe_fail("fina_indicator_batch")
        wanted = set(ts_codes)
        return [r for r in self.fina_indicator_rows if r.get("ts_code") in wanted]

    def statement_by_symbol(self, api_name, ts_code, start, end):
        self.calls.append(("statement_by_symbol", api_name, ts_code, start, end))
        self._maybe_fail(f"statement_by_symbol:{ts_code}")
        return [
            r for r in self.statement_rows.get(api_name, [])
            if r.get("ts_code") == ts_code
        ]

    def daily_basic_by_date(self, day):
        self.calls.append(("daily_basic_by_date", day))
        self._maybe_fail(f"daily_basic_by_date:{day}")
        return list(self.daily_basic_by_date_map.get(day.strftime("%Y%m%d"), []))

    # ---- 记录 + 可选失败注入 ----
    def _maybe_fail(self, key: str):
        if key in self.fail_on:
            raise tc.TushareError(f"注入失败: {key}")

    def close(self):
        self.calls.append(("close",))

    def trade_days(self, start, end):
        self.calls.append(("trade_days", start, end))
        self._maybe_fail("trade_days")
        out = self.trade_days_result
        if out is None:
            return []
        return [d for d in out if start <= d <= end]

    def daily_by_date(self, day):
        self.calls.append(("daily_by_date", day))
        self._maybe_fail(f"daily_by_date:{day}")
        return list(self.daily_by_date_map.get(day.strftime("%Y%m%d"), []))

    def daily_by_symbol(self, ts_code, start, end):
        self.calls.append(("daily_by_symbol", ts_code, start, end))
        self._maybe_fail(f"daily_by_symbol:{ts_code}")
        return list(self.daily_by_symbol_map.get(ts_code, []))

    def adj_factor_by_date(self, day):
        self.calls.append(("adj_factor_by_date", day))
        self._maybe_fail(f"adj_factor_by_date:{day}")
        return list(self.adj_by_date_map.get(day.strftime("%Y%m%d"), []))

    def adj_factor_by_symbol(self, ts_code, start, end):
        self.calls.append(("adj_factor_by_symbol", ts_code, start, end))
        self._maybe_fail(f"adj_factor_by_symbol:{ts_code}")
        return list(self.adj_by_symbol_map.get(ts_code, []))

    def probe(self):
        self.calls.append(("probe",))
        self._maybe_fail("probe")
        return list(self.probe_rows)


def _provider(monkeypatch, client: _FakeClient) -> TushareProvider:
    """返回注入了假 client 的 provider (同时屏蔽真实 API Key 读取)。"""
    monkeypatch.setattr(tp, "get_api_key", lambda: "test-token")
    p = TushareProvider()
    p._client = client
    return p


def _daily_row(code="000001.SZ", day="20240102", **over):
    """Tushare daily 实测形态: 字段名 vol(手) / amount(千元), 日期 YYYYMMDD。"""
    row = {
        "ts_code": code,
        "trade_date": day,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "vol": 123456.0,      # 手 — 与项目契约一致, 不转换
        "amount": 125000.5,   # 千元 — provider 层 乘 1000 转元
    }
    row.update(over)
    return row


def _factor_row(code, day, factor):
    return {"ts_code": code, "trade_date": day, "adj_factor": factor}


# ---- 单位口径 ----


def test_daily_amount_thousand_yuan_converted_to_yuan(monkeypatch):
    """核心口径: Tushare amount 单位千元 → 项目契约元 (乘 1000)。"""
    client = _FakeClient(
        daily_by_symbol={"000001.SZ": [_daily_row(amount=125000.5)]},
    )
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 1
    assert df["amount"][0] == pytest.approx(125000500.0)  # 125000.5 千元 = 1.250005 亿元


def test_daily_volume_hand_not_converted(monkeypatch):
    """vol 单位已是手, 与项目契约一致, 不做任何换算。"""
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row(vol=123456.0)]})
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df["volume"][0] == pytest.approx(123456.0)


def test_daily_ohlc_passthrough_and_schema(monkeypatch):
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row()]})
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    row = df.to_dicts()[0]
    assert row["symbol"] == "000001.SZ"
    assert row["date"] == date(2024, 1, 2)
    assert (row["open"], row["high"], row["low"], row["close"]) == (10.0, 10.5, 9.8, 10.2)
    # 只保留项目日K契约列, 不夹带 upstream 原始字段
    assert set(df.columns) <= {"symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"}


@pytest.mark.parametrize(
    "raw",
    ["20240102", 20240102, "2024-01-02"],
    ids=["str_yyyymmdd", "int_yyyymmdd", "iso"],
)
def test_trade_date_parsing_variants(monkeypatch, raw):
    """Tushare 日期可能以字符串/整数/ISO 形态返回, 三种都要正确归一。"""
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row(day=raw)]})
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df["date"][0] == date(2024, 1, 2)


@pytest.mark.parametrize("missing", ["vol", "amount"], ids=["vol_null", "amount_null"])
def test_null_field_stays_null_not_fabricated(monkeypatch, missing):
    """缺失字段必须置 None, 禁止启发式补值 (CONTRIBUTING §3.1)。

    注意: 另一字段必须有值 — filter_halt_days 会把 OHL 全 null / 量额全 null
    的行当停牌剔除(项目既有行为), 那样就测不到"字段值"了。
    """
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row(**{missing: None})]})
    df = _provider(monkeypatch, client).get_daily(
        ["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 1
    row = df.to_dicts()[0]
    field = "volume" if missing == "vol" else "amount"
    assert row[field] is None
    # 另一字段保持正常换算结果, 证明没有"整体置空"或"补 0"
    other = "amount" if missing == "vol" else "volume"
    assert row[other] == pytest.approx(125000500.0 if other == "amount" else 123456.0)


# ---- 取数模式分流 ----


def test_full_market_uses_trade_calendar_mode(monkeypatch):
    """标的数 >= 阈值: 走交易日历 + 逐日全市场取数(一天一次请求)。"""
    days = [date(2024, 1, 2), date(2024, 1, 3)]
    client = _FakeClient(
        trade_days=days,
        daily_by_date={
            "20240102": [_daily_row(code="000001.SZ", day="20240102")],
            "20240103": [_daily_row(code="000002.SZ", day="20240103", close=11.0)],
        },
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 151)]  # 150 只 → 触发按日模式
    df = _provider(monkeypatch, client).get_daily(
        symbols, datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert ("trade_days", date(2024, 1, 1), date(2024, 1, 31)) in client.calls
    assert ("daily_by_date", date(2024, 1, 2)) in client.calls
    # 逐标的接口完全不应被调用
    assert not any(c[0] == "daily_by_symbol" for c in client.calls)
    assert df.height == 2


def test_few_symbols_use_per_symbol_mode(monkeypatch):
    """标的数少: 逐标的取区间, 避免为几只股票拉回全市场。"""
    client = _FakeClient(
        trade_days=[date(2024, 1, 2)],
        daily_by_symbol={"000001.SZ": [_daily_row()]},
    )
    _provider(monkeypatch, client).get_daily(
        ["000001.SZ", "000002.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert any(c[0] == "daily_by_symbol" for c in client.calls)
    assert not any(c[0] == "daily_by_date" for c in client.calls)


def test_calendar_failure_falls_back_to_per_symbol(monkeypatch):
    """交易日历不可用(如积分不足)时自动回退逐标的模式, 而非整体失败。"""
    client = _FakeClient(
        fail_on={"trade_days"},
        daily_by_symbol={"000001.SZ": [_daily_row()]},
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]
    df = _provider(monkeypatch, client).get_daily(
        symbols, datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert any(c[0] == "daily_by_symbol" for c in client.calls)
    assert df.height == 1


def test_date_mode_filters_to_requested_symbols(monkeypatch):
    """按日模式拉回全市场, 必须过滤成请求标的, 避免串数据。"""
    client = _FakeClient(
        trade_days=[date(2024, 1, 2)],
        daily_by_date={
            "20240102": [
                _daily_row(code="000001.SZ"),
                _daily_row(code="000002.SZ"),
                _daily_row(code="600519.SH"),
            ]
        },
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]  # 含 000001/000002, 不含 600519
    df = _provider(monkeypatch, client).get_daily(
        symbols, datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "000002.SZ"]


def test_non_stock_asset_type_yields_nothing(monkeypatch):
    """Tushare daily 仅覆盖股票; ETF/指数不产出 → 上层回退现有数据源。"""
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row()]})
    df = _provider(monkeypatch, client).get_daily(
        ["510300.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31), asset_type="etf"
    )
    assert df.is_empty()
    assert client.calls == []


def test_iter_daily_reports_progress(monkeypatch):
    client = _FakeClient(
        trade_days=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        daily_by_date={d: [_daily_row(day=d)] for d in ("20240102", "20240103", "20240104")},
    )
    seen: list[tuple[int, int]] = []
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]
    chunks = list(
        _provider(monkeypatch, client).iter_daily(
            symbols,
            start_time=datetime(2024, 1, 1),
            end_time=datetime(2024, 1, 31),
            on_chunk_done=lambda d, t: seen.append((d, t)),
        )
    )
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert len(chunks) == 3


# ---- 失败处理 ----


def test_major_failure_raises_to_avoid_half_data(monkeypatch):
    """过半请求失败必须抛错: 半截数据落库的代价远高于同步失败。"""
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    client = _FakeClient(
        trade_days=days,
        daily_by_date={"20240104": [_daily_row(day="20240104")]},
        fail_on={"daily_by_date:2024-01-02", "daily_by_date:2024-01-03"},
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]
    with pytest.raises(tc.TushareError, match="不完整"):
        _provider(monkeypatch, client).get_daily(
            symbols, datetime(2024, 1, 1), datetime(2024, 1, 31)
        )


def test_minor_failure_continues_with_warning(monkeypatch):
    """个别日期抖动只告警继续, 已取到的数据本身正确。"""
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    client = _FakeClient(
        trade_days=days,
        daily_by_date={d: [_daily_row(day=d)] for d in ("20240102", "20240103", "20240105")},
        fail_on={"daily_by_date:2024-01-04"},
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]
    df = _provider(monkeypatch, client).get_daily(
        symbols, datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 3


# ---- 除权因子 ----


def test_adj_factor_cumulative_to_event_ratio(monkeypatch):
    """官方累积因子序列 → 单事件 pre/post 比值: ratio = f(d) / f(d_prev)。"""
    # 10 送 10: 除权日因子从 1.0 跳到 2.0 (或后复权口径 2.0 → 4.0, 比值等价)
    client = _FakeClient(
        adj_by_symbol={
            "000001.SZ": [
                _factor_row("000001.SZ", "20240301", 2.0),
                _factor_row("000001.SZ", "20240304", 4.0),  # 除权日
                _factor_row("000001.SZ", "20240305", 4.0),
            ]
        }
    )
    df = _provider(monkeypatch, client).get_adj_factors(
        ["000001.SZ"], datetime(2024, 3, 1), datetime(2024, 3, 31)
    )
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["symbol"] == "000001.SZ"
    assert row["trade_date"] == date(2024, 3, 4)
    assert row["ex_factor"] == pytest.approx(2.0)


def test_adj_factor_ignores_unchanged_days(monkeypatch):
    """因子未变化的交易日不是除权事件, 不应产出记录。"""
    client = _FakeClient(
        adj_by_symbol={
            "000001.SZ": [
                _factor_row("000001.SZ", "20240301", 1.0),
                _factor_row("000001.SZ", "20240304", 1.0),
                _factor_row("000001.SZ", "20240305", 1.0),
            ]
        }
    )
    df = _provider(monkeypatch, client).get_adj_factors(
        ["000001.SZ"], datetime(2024, 3, 1), datetime(2024, 3, 31)
    )
    assert df.is_empty()


def test_adj_factor_needs_baseline_before_window(monkeypatch):
    """窗口首日若为除权日, 必须靠"窗口前一个交易日"的因子作基准 → 取数窗口要向前多取。"""
    client = _FakeClient(
        adj_by_symbol={
            "000001.SZ": [
                _factor_row("000001.SZ", "20240531", 1.0),  # 基准日(在请求窗口之前)
                _factor_row("000001.SZ", "20240603", 1.5),  # 除权日(窗口首日)
            ]
        }
    )
    df = _provider(monkeypatch, client).get_adj_factors(
        ["000001.SZ"], datetime(2024, 6, 1), datetime(2024, 6, 30)
    )
    assert df.height == 1
    assert df["ex_factor"][0] == pytest.approx(1.5)
    # 取数起点必须比请求起点早, 否则拿不到基准
    call = next(c for c in client.calls if c[0] == "adj_factor_by_symbol")
    assert call[2] < date(2024, 6, 1)


def test_adj_factor_filters_events_outside_window(monkeypatch):
    """基准日自身的因子变化不算请求窗口内的事件。"""
    client = _FakeClient(
        adj_by_symbol={
            "000001.SZ": [
                _factor_row("000001.SZ", "20240530", 1.0),
                _factor_row("000001.SZ", "20240531", 1.2),  # 除权, 但在窗口之前
                _factor_row("000001.SZ", "20240603", 1.2),
            ]
        }
    )
    df = _provider(monkeypatch, client).get_adj_factors(
        ["000001.SZ"], datetime(2024, 6, 1), datetime(2024, 6, 30)
    )
    assert df.is_empty()


def test_adj_factor_multi_symbol_grouped(monkeypatch):
    """多标的必须分组计算, 不能跨标的串用前值。"""
    client = _FakeClient(
        trade_days=[date(2024, 3, 1), date(2024, 3, 4)],
        adj_by_date={
            "20240301": [
                _factor_row("000001.SZ", "20240301", 1.0),
                _factor_row("000002.SZ", "20240301", 1.0),
            ],
            "20240304": [
                _factor_row("000001.SZ", "20240304", 2.0),
                _factor_row("000002.SZ", "20240304", 1.1),
            ],
        }
    )
    symbols = [f"{i:06d}.SZ" for i in range(1, 120)]
    df = _provider(monkeypatch, client).get_adj_factors(
        symbols, datetime(2024, 3, 1), datetime(2024, 3, 31)
    )
    got = {r["symbol"]: r["ex_factor"] for r in df.to_dicts()}
    assert got["000001.SZ"] == pytest.approx(2.0)
    assert got["000002.SZ"] == pytest.approx(1.1)


def test_adj_factor_schema_and_sorting(monkeypatch):
    client = _FakeClient(
        adj_by_symbol={
            "000002.SZ": [
                _factor_row("000002.SZ", "20240301", 1.0),
                _factor_row("000002.SZ", "20240305", 1.3),
            ],
            "000001.SZ": [
                _factor_row("000001.SZ", "20240301", 1.0),
                _factor_row("000001.SZ", "20240306", 1.1),
            ],
        }
    )
    df = _provider(monkeypatch, client).get_adj_factors(
        ["000001.SZ", "000002.SZ"], datetime(2024, 3, 1), datetime(2024, 3, 31)
    )
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df["symbol"].to_list() == ["000001.SZ", "000002.SZ"]
    assert df.schema["ex_factor"] == pl.Float64


def test_adj_factor_non_stock_returns_empty(monkeypatch):
    client = _FakeClient(adj_by_symbol={"510300.SH": [_factor_row("510300.SH", "20240301", 1.0)]})
    df = _provider(monkeypatch, client).get_adj_factors(
        ["510300.SH"], datetime(2024, 3, 1), datetime(2024, 3, 31), asset_type="etf"
    )
    assert df.is_empty()
    assert df.columns == ["symbol", "trade_date", "ex_factor"]


# ---- 能力声明 / 设置页试拉 ----


def test_only_declared_datasets_available(monkeypatch):
    """未声明的数据集 provider_has_dataset 必须为 False → 自动回退现有源。

    financial 已在 P1 接入 (见 tests/test_tushare_financial.py), 此处一并断言存在。
    """
    p = _provider(monkeypatch, _FakeClient())
    for ds in ("daily", "adj_factor", "financial"):
        assert ds in p.config.datasets
    for ds in ("minute", "realtime", "full_minute"):
        assert ds not in p.config.datasets


def test_test_dataset_daily_preview_is_json_safe(monkeypatch):
    client = _FakeClient(daily_by_symbol={"000001.SZ": [_daily_row()]})
    result = _provider(monkeypatch, client).test_dataset("daily", ["000001.SZ"])
    assert result["provider"] == "tushare"
    assert result["rows"] == 1
    assert "symbol" in result["columns"]
    # date 必须已转 ISO 字符串, 否则设置页 JSON 序列化会炸
    assert result["preview"][0]["date"] == "2024-01-02"


def test_test_dataset_unsupported_returns_error(monkeypatch):
    result = _provider(monkeypatch, _FakeClient()).test_dataset("minute")
    assert result["rows"] == 0
    assert "minute" in result["error"]


def test_test_dataset_error_is_captured(monkeypatch):
    client = _FakeClient(fail_on={"daily_by_symbol:000001.SZ"})
    result = _provider(monkeypatch, client).test_dataset("daily", ["000001.SZ"])
    assert result["rows"] == 0
    # 失败被汇总为"避免写入不完整数据"的显式错误,而非静默返回 0 行
    assert "不完整" in result["error"]


# ---- 可用性 / Token 探测 ----


def test_availability_requires_key(monkeypatch):
    monkeypatch.setattr(tp, "get_api_key", lambda: "")
    ok, reason = tp.availability()
    assert ok is False
    assert "TUSHARE_API_KEY" in reason

    monkeypatch.setattr(tp, "get_api_key", lambda: "a" * 64)
    assert tp.availability() == (True, "ok")


def test_probe_api_key_reports_invalid(monkeypatch):
    """探测失败(无效 Token)必须返回 False 且不落盘。"""
    import app.plugins.tushare.provider as provider_mod

    class _Boom:
        def __init__(self, **kw):
            raise tc.TushareError("Token 无效")

        def close(self):
            pass

    monkeypatch.setattr(provider_mod.tushare_client, "TushareClient", _Boom)
    ok, reason = tp.probe_api_key("bad-token")
    assert ok is False
    assert "无效" in reason


def test_probe_api_key_reports_empty_response(monkeypatch):
    """Token 通过但接口无返回(积分不足)也要提示, 不能误判为成功。"""
    import app.plugins.tushare.provider as provider_mod

    class _Empty:
        def __init__(self, **kw):
            pass

        def probe(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(provider_mod.tushare_client, "TushareClient", _Empty)
    ok, reason = tp.probe_api_key("valid-but-no-data")
    assert ok is False
    assert "积分" in reason


# ---- client 层 ----


def test_client_rejects_missing_key():
    with pytest.raises(tc.TushareError):
        tc.TushareClient(api_key="")


def test_client_envelope_error_code(monkeypatch):
    """信封 code != 0 必须抛错, 不能把错误响应当空数据处理。"""
    import httpx

    from app.plugins.tushare.client import TushareClient

    def _fake_post(self, *a, **kw):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"code": 40203, "msg": "抱歉, 您没有访问该接口的权限"}

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = TushareClient(api_key="k", interval=0.0)
    with pytest.raises(tc.TushareError, match="40203"):
        client.query("daily", {"trade_date": "20240102"})
    client.close()


def test_client_assembles_fields_and_items(monkeypatch):
    """响应为 列名 + 二维行分离的结构, 必须正确组装成 dict 列表。"""
    import httpx

    from app.plugins.tushare.client import TushareClient

    def _fake_post(self, *a, **kw):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "code": 0,
                    "msg": None,
                    "data": {
                        "fields": ["ts_code", "trade_date", "close"],
                        "items": [["000001.SZ", "20240102", 10.2]],
                    },
                }

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = TushareClient(api_key="k", interval=0.0)
    rows = client.query("daily", {"trade_date": "20240102"})
    assert rows == [{"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.2}]
    client.close()


def test_client_pagination_stops_on_short_page(monkeypatch):
    """末页不足 _PAGE_SIZE 即停, 不空转。"""
    import httpx

    from app.plugins.tushare.client import TushareClient

    calls = []

    def _fake_post(self, *a, **kw):
        calls.append(kw["json"]["params"])
        n = 6000 if len(calls) == 1 else 3

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code"],
                        "items": [["000001.SZ"]] * n,
                    },
                }

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = TushareClient(api_key="k", interval=0.0)
    pages = list(client.query_paged("daily", {}))
    assert len(pages) == 2
    assert [p["offset"] for p in calls] == [0, 6000]
    client.close()
