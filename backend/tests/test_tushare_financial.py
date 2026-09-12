"""Tushare 财务数据集 (P1) 契约测试。

复用 test_tushare_provider 里的 _FakeClient / _provider, 全部为离线契约测试,
不依赖真实网络与 Token。

核心守卫(这些坑静默出错, 数据看着正常但全是错的):
  - fina_indicator 单次返回封顶 100 行且**静默截断** → 批量上限 + 折半重拉
  - income/balancesheet/cashflow 的 ts_code **硬必填且不支持批量** → 只能逐只
  - daily_basic 的 total_share/float_share 单位 **万股** → 乘 10000 转股
  - 同一报告期官方返回多行(合并/单季/调整) → 必须去重, 否则同期重复落库
  - 失败率过半必须抛错, 避免半截数据覆盖掉本地已有全量
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest
from test_tushare_provider import _FakeClient, _provider

from app.plugins.tushare import client as tc
from app.plugins.tushare import provider as tp
from app.plugins.tushare.provider import TushareProvider


def _indicator_row(code="600519.SH", end_date="20260630", **over):
    """fina_indicator 实测形态: ann_date / end_date 均为 YYYYMMDD 字符串。"""
    row = {
        "ts_code": code,
        "ann_date": "20260815",
        "end_date": end_date,
        "eps": 35.57,
        "roe": 17.9543,
        "roa": 19.9688,
        "grossprofit_margin": 89.5552,
        "netprofit_margin": 50.7516,
        "debt_to_assets": 15.1931,
        "or_yoy": 1.4699,
        "netprofit_yoy": -1.9516,
        "bps": 200.9898,
    }
    row.update(over)
    return row


def _income_row(code="600519.SH", end_date="20260630", report_type="1", **over):
    """income 实测形态 (2026H1 贵州茅台, 2026-09 亲测)。"""
    row = {
        "ts_code": code,
        "ann_date": "20260815",
        "end_date": end_date,
        "report_type": report_type,
        "revenue": 90703260964.48,
        "oper_cost": 9473762565.88,
        "sell_exp": 3206308341.53,
        "admin_exp": 3635355263.82,
        "rd_exp": 114926219.6,
        "fin_exp": -243237716.96,
        "operate_profit": 61411291686.27,
        "total_profit": 61438419177.29,
        "income_tax": 15405088610.51,
        "n_income": 46033330566.78,
        "n_income_attr_p": 44516880421.86,
        "basic_eps": 35.57,
    }
    row.update(over)
    return row


# ================================================================
# metrics (fina_indicator, 批量)
# ================================================================

def test_metrics_maps_to_canonical_columns(monkeypatch):
    c = _FakeClient()
    c.fina_indicator_rows = [_indicator_row()]
    p = _provider(monkeypatch, c)
    df = p.get_financials("metrics", ["600519.SH"])
    assert {"symbol", "period_end", "announce_date"} <= set(df.columns)
    r = df.row(0, named=True)
    assert r["symbol"] == "600519.SH"
    assert r["period_end"] == "2026-06-30"       # YYYYMMDD → ISO
    assert r["announce_date"] == "2026-08-15"
    assert r["roe"] == 17.9543
    assert r["roa"] == 19.9688
    assert r["gross_margin"] == 89.5552          # grossprofit_margin
    assert r["net_margin"] == 50.7516            # netprofit_margin
    assert r["debt_to_asset_ratio"] == 15.1931   # debt_to_assets
    assert r["revenue_yoy"] == 1.4699            # or_yoy
    assert r["net_income_yoy"] == -1.9516        # netprofit_yoy


def test_metrics_returns_all_symbols_without_loss(monkeypatch):
    """全量同步不丢标的: 分批逻辑在 client 层(见下方 client 测试), provider 只透传。"""
    c = _FakeClient()
    codes = [f"{600000 + i}.SH" for i in range(120)]
    c.fina_indicator_rows = [_indicator_row(code=s) for s in codes]
    p = _provider(monkeypatch, c)
    df = p.get_financials("metrics", codes)
    assert c.fina_batch_sizes == [120]           # provider 一次性交给 client
    assert sorted(df["symbol"].to_list()) == sorted(codes)


def _patch_post(monkeypatch, rows_per_code: int):
    """伪造 Tushare HTTP 层: 按请求的 ts_code 数量返回 rows_per_code 倍的行。"""
    calls: list[dict] = []

    def _fake_post(self, *a, **kw):
        params = kw["json"]["params"]
        calls.append(dict(params))
        n = len(str(params.get("ts_code", "")).split(",")) * rows_per_code

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "end_date"],
                        "items": [["600000.SH", "20260630"]] * n,
                    },
                }

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    return calls


def test_client_splits_into_batches_of_cap(monkeypatch):
    """client 层按 _FINA_BATCH 分批: 120 只 → 3 次请求, 单批不超过 50。"""
    calls = _patch_post(monkeypatch, rows_per_code=1)
    client = tc.TushareClient(api_key="k", interval=0.0)
    rows = client.fina_indicator_batch([f"{600000 + i}.SH" for i in range(120)],
                                       date(2025, 1, 1), date(2026, 9, 12))
    client.close()
    sizes = [len(str(c["ts_code"]).split(",")) for c in calls]
    assert sizes == [50, 50, 20]
    assert len(rows) == 120


def test_client_halves_when_hitting_row_cap(monkeypatch):
    """返回触顶 100 行 → 折半重拉 (官方是**静默截断**, 不折半就丢数据)。

    每只返回 2 行时, 50 只/批正好 100 行触顶; 折半后每批 25 只 = 50 行, 安全。
    """
    calls = _patch_post(monkeypatch, rows_per_code=2)
    client = tc.TushareClient(api_key="k", interval=0.0)
    rows = client.fina_indicator_batch([f"{600000 + i}.SH" for i in range(60)],
                                       date(2025, 1, 1), date(2026, 9, 12))
    client.close()
    sizes = [len(str(c["ts_code"]).split(",")) for c in calls]
    assert max(sizes) == 50           # 首次按 50 发
    assert 25 in sizes                # 触顶后折半为 25
    assert len(rows) == 120           # 60 只 x 2 行, 一行不丢


def test_metrics_latest_only_keeps_newest_period(monkeypatch):
    c = _FakeClient()
    c.fina_indicator_rows = [
        _indicator_row(end_date="20251231"),
        _indicator_row(end_date="20260331"),
        _indicator_row(end_date="20260630"),
    ]
    p = _provider(monkeypatch, c)
    df = p.get_financials("metrics", ["600519.SH"], latest_only=True)
    assert len(df) == 1
    assert df.row(0, named=True)["period_end"] == "2026-06-30"


def test_metrics_history_caps_at_eight_periods(monkeypatch):
    periods = ("20201231", "20210331", "20210630", "20210930",
               "20211231", "20220331", "20220630", "20220930",
               "20221231", "20230331")
    c = _FakeClient()
    c.fina_indicator_rows = [_indicator_row(end_date=d) for d in periods]
    p = _provider(monkeypatch, c)
    df = p.get_financials("metrics", ["600519.SH"], latest_only=False)
    assert len(df) == tp._FINANCIAL_HISTORY_PERIODS == 8
    # 保留的是最近的 8 期, 最早的 2 期被裁掉
    assert df.row(0, named=True)["period_end"] == "2021-06-30"


def test_metrics_dedupes_same_period_by_announce_date(monkeypatch):
    """同一报告期返回多行(调整/单季) → 只留一行, 取 announce_date 最新。"""
    c = _FakeClient()
    c.fina_indicator_rows = [
        _indicator_row(end_date="20260630", ann_date="20260815", roe=1.0),
        _indicator_row(end_date="20260630", ann_date="20261001", roe=2.0),
    ]
    p = _provider(monkeypatch, c)
    df = p.get_financials("metrics", ["600519.SH"])
    assert len(df) == 1
    assert df.row(0, named=True)["roe"] == 2.0


# ================================================================
# 三大报表 (逐只)
# ================================================================

def test_income_maps_to_canonical_columns(monkeypatch):
    c = _FakeClient()
    c.statement_rows = {"income": [_income_row()]}
    p = _provider(monkeypatch, c)
    r = p.get_financials("income", ["600519.SH"], latest_only=True).row(0, named=True)
    assert r["revenue"] == 90703260964.48
    assert r["operating_cost"] == 9473762565.88            # oper_cost
    assert r["selling_expense"] == 3206308341.53           # sell_exp
    assert r["admin_expense"] == 3635355263.82             # admin_exp
    assert r["rd_expense"] == 114926219.6                  # rd_exp
    assert r["financial_expense"] == -243237716.96         # fin_exp
    assert r["net_income"] == 46033330566.78               # n_income(含少数股东)
    assert r["net_income_attributable"] == 44516880421.86  # n_income_attr_p
    assert r["basic_eps"] == 35.57


def test_balance_sheet_and_cash_flow_route_to_tushare_api_names(monkeypatch):
    """项目表名 ≠ 接口名: balance_sheet → balancesheet。错一个就整表空。"""
    c = _FakeClient()
    c.statement_rows = {
        "balancesheet": [{
            "ts_code": "600519.SH", "ann_date": "20260815", "end_date": "20260630",
            "report_type": "1",
            "total_assets": 309050784569.31,
            "total_cur_assets": 260724668103.4,
            "total_nca": 48326116465.91,
            "money_cap": 53518798979.08,
            "accounts_receiv": 570895.04,
            "total_liab": 46954432394.95,
            "total_hldr_eqy_inc_min_int": 262096352174.36,
        }],
        "cashflow": [{
            "ts_code": "600519.SH", "ann_date": "20260815", "end_date": "20260630",
            "report_type": "1",
            "n_cashflow_act": 70690750119.06,
            "n_cashflow_inv_act": 25640543520.6,
            "n_cash_flows_fnc_act": -37944297802.12,
            "c_pay_acq_const_fiolta": 832142752.28,
            "n_incr_cash_cash_equ": 58385486034.9,
        }],
    }
    p = _provider(monkeypatch, c)

    r = p.get_financials("balance_sheet", ["600519.SH"], latest_only=True).row(0, named=True)
    assert r["total_assets"] == 309050784569.31
    assert r["total_current_assets"] == 260724668103.4
    assert r["total_non_current_assets"] == 48326116465.91
    assert r["cash_and_equivalents"] == 53518798979.08     # money_cap
    assert r["accounts_receivable"] == 570895.04
    assert r["total_liabilities"] == 46954432394.95
    # 权益合计用 inc_min_int(含少数股东): 实测 total_assets - total_liab 恰等于它
    assert r["total_equity"] == 262096352174.36

    r2 = p.get_financials("cash_flow", ["600519.SH"], latest_only=True).row(0, named=True)
    assert r2["net_operating_cash_flow"] == 70690750119.06   # n_cashflow_act
    assert r2["net_investing_cash_flow"] == 25640543520.6
    assert r2["net_financing_cash_flow"] == -37944297802.12
    assert r2["capex"] == 832142752.28
    assert r2["net_cash_change"] == 58385486034.9

    apis = {call[1] for call in c.calls if call[0] == "statement_by_symbol"}
    assert apis == {"balancesheet", "cashflow"}


def test_statements_prefer_report_type_one(monkeypatch):
    """同期多 report_type: report_type=1(合并报表)优先, 而非 announce_date 最新。"""
    c = _FakeClient()
    c.statement_rows = {"income": [
        _income_row(report_type="2", ann_date="20261001", revenue=1.0),
        _income_row(report_type="1", ann_date="20260815", revenue=90703260964.48),
    ]}
    p = _provider(monkeypatch, c)
    df = p.get_financials("income", ["600519.SH"], latest_only=True)
    assert len(df) == 1
    assert df.row(0, named=True)["revenue"] == 90703260964.48


def test_statements_are_per_symbol(monkeypatch):
    """三表不支持批量(实测多 code 返回 0 行) → 每个标的各一次请求。"""
    c = _FakeClient()
    c.statement_rows = {"income": [_income_row(), _income_row(code="600000.SH")]}
    p = _provider(monkeypatch, c)
    p.get_financials("income", ["600519.SH", "600000.SH"], latest_only=True)
    calls = [x for x in c.calls if x[0] == "statement_by_symbol"]
    assert {call[2] for call in calls} == {"600519.SH", "600000.SH"}


def test_statements_abort_when_most_fail(monkeypatch):
    """失败过半 → 抛错中止, 避免半截数据覆盖本地已有全量。"""
    c = _FakeClient(fail_on={"statement_by_symbol:000001.SZ",
                             "statement_by_symbol:000002.SZ"})
    p = _provider(monkeypatch, c)
    with pytest.raises(tc.TushareError):
        p.get_financials("income", ["000001.SZ", "000002.SZ", "600519.SH"])


def test_statements_tolerate_few_failures(monkeypatch):
    """少量失败 → 告警继续, 成功标的照常返回。"""
    c = _FakeClient(fail_on={"statement_by_symbol:000002.SZ"})
    c.statement_rows = {"income": [_income_row(), _income_row(code="600000.SH")]}
    p = _provider(monkeypatch, c)
    df = p.get_financials("income", ["600519.SH", "000002.SZ", "600000.SH"])
    assert sorted(df["symbol"].to_list()) == ["600000.SH", "600519.SH"]


# ================================================================
# shares (daily_basic, 按日批量)
# ================================================================

def test_shares_converts_wan_to_shares(monkeypatch):
    """daily_basic 单位万股 → 乘 10000 转股。

    实测 600519 total_share=125008.1601 万股 = 1.2501e9 股, 与 instruments 一致。
    漏了这一步, 所有市值/换手相关因子会小 1 万倍且不报错。
    """
    c = _FakeClient(trade_days=[date(2026, 9, 10), date(2026, 9, 11)])
    c.daily_basic_by_date_map = {"20260911": [{
        "ts_code": "600519.SH", "trade_date": "20260911",
        "total_share": 125008.1601, "float_share": 125008.1601,
    }]}
    p = _provider(monkeypatch, c)
    r = p.get_financials("shares", ["600519.SH"], latest_only=True).row(0, named=True)
    assert r["period_end"] == "2026-09-11"     # 股本以"该交易日可用"落库
    assert r["total_shares"] == pytest.approx(1_250_081_601.0)
    assert r["float_shares"] == pytest.approx(1_250_081_601.0)


def test_shares_history_samples_month_ends(monkeypatch):
    """历史模式按月抽样: 请求次数 = 月份数, 而非交易日数(480 次太浪费)。"""
    # 用已过去的年份: 未来日期会被 trade_days 的区间过滤掉, 导致月份数对不上
    days = [date(2025, m, 28) for m in range(1, 13)]
    c = _FakeClient(trade_days=days)
    for d in days:
        c.daily_basic_by_date_map[d.strftime("%Y%m%d")] = [{
            "ts_code": "600519.SH", "trade_date": d.strftime("%Y%m%d"),
            "total_share": 125008.1601, "float_share": 125008.1601,
        }]
    p = _provider(monkeypatch, c)
    df = p.get_financials("shares", ["600519.SH"], latest_only=False)
    calls = [x for x in c.calls if x[0] == "daily_basic_by_date"]
    assert len(calls) == 12
    assert df.height == 12


# ================================================================
# 契约与试拉
# ================================================================

def test_unknown_table_returns_empty(monkeypatch):
    """未接入的表 → 空帧, 由 _merge_report_history 保留既有数据, 不抛异常。"""
    p = _provider(monkeypatch, _FakeClient())
    assert p.get_financials("unknown_table", ["600519.SH"]).is_empty()


def test_financial_declared_in_datasets():
    """声明 financial → provider_has_dataset 为真, 设置页财务源可切到 tushare。"""
    assert "financial" in tp._DATASETS
    assert "financial" in TushareProvider().config.datasets


def test_test_dataset_financial_uses_metrics(monkeypatch):
    """试拉 financial 走 metrics(批量秒级), 不触发三表逐只(5550x3 次请求)。"""
    c = _FakeClient()
    c.fina_indicator_rows = [_indicator_row()]
    p = _provider(monkeypatch, c)
    res = p.test_dataset("financial", ["600519.SH"])
    assert res["rows"] == 1
    assert "roe" in res["columns"]
    assert not [x for x in c.calls if x[0] == "statement_by_symbol"]
