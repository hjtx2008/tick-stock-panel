"""ext_capital_flow 内置预设与 flatten 契约 (看板「全市场资金流向」面板数据源)。

锁住:
  - preset id/label/mode/字段名, 看板前端按此 id 直接调 api.extDataRows
  - flatten 兼容上游 [industry / flow_amount / direction / date] 形态, 方向大小写不敏感,
    date 缺省时留空字符串供前端按 timeseries 自行取最新日

回归: 看板 MarketCapitalFlowPanel 直接走 api.extDataRows('ext_capital_flow', ...) →
服务端按本 preset id 寻表 → 字段对不上会出现 "key not found" 类静默错误。
"""
from __future__ import annotations

from app.services import ext_presets


def test_capital_flow_preset_is_registered():
    """_presets() 必须包含 ext_capital_flow, 否则看板调 extDataRows 会拿到 404。"""
    ids = [p.id for p in ext_presets._presets()]
    assert "ext_capital_flow" in ids, f"ext_capital_flow 未注册; 当前 presets={ids}"


def test_capital_flow_preset_fields_contract():
    """字段必须含 industry / flow_amount / direction / date, 看板组件按这四个字段名读取。"""
    preset = ext_presets.get_preset("ext_capital_flow")
    assert preset is not None
    field_names = {f.name for f in preset.fields}
    assert {"date", "industry", "flow_amount", "direction"}.issubset(field_names), (
        f"字段缺失; 实际={field_names}"
    )


def test_capital_flow_preset_disabled_by_default():
    """出厂禁用 (避免启动即网络拉取, 与 ext_gn_ths / ext_hy_ths 一致 #199)。"""
    preset = ext_presets.get_preset("ext_capital_flow")
    assert preset is not None
    assert preset.pull.enabled is False, "新建的内置 preset 不应在出厂时启用拉取"


def test_flatten_capital_flow_rows_basic():
    """flatten: 上游数组 → 本地 schema, 方向小写归一。"""
    raw = [
        {"industry": "半导体", "flow_amount": 12.5, "direction": "in", "date": "2026-09-09"},
        {"industry": "房地产", "flow_amount": -8.3, "direction": "out", "date": "2026-09-09"},
        {"industry": "新股", "flow_amount": 5.2, "direction": "new", "date": "2026-09-09"},
    ]
    out = ext_presets._flatten_capital_flow_rows(raw)
    assert len(out) == 3
    assert out[0] == {"date": "2026-09-09", "industry": "半导体", "flow_amount": 12.5, "direction": "in"}
    assert out[1]["direction"] == "out"
    assert out[2]["direction"] == "new"


def test_flatten_capital_flow_rows_handles_missing_and_dirty():
    """flatten 兼容字段缺省/类型脏数据, 不抛异常。"""
    raw = [
        {"industry": "", "flow_amount": 1.0},                          # 空 industry 跳过
        {"industry": "医药", "flow_amount": "abc", "direction": "IN"},  # 字符串金额 → 0.0
        {"industry": "金融", "flow_amount": None, "direction": "Out"},  # None 金额 → 0.0
        {"name": "钢铁", "flow_amount": 3.0, "direction": "new"},        # name 兜底为 industry
    ]
    out = ext_presets._flatten_capital_flow_rows(raw)
    # 第 1 行被空 industry 过滤, 共 3 行
    assert len(out) == 3
    assert all(isinstance(r["flow_amount"], float) for r in out)
    assert {r["direction"] for r in out} == {"in", "out", "new"}  # 全部归一为小写


def test_apply_preset_flatten_routes_ext_capital_flow():
    """ext_pull._apply_preset_flatten 必须把 ext_capital_flow 派发到资金流向 flatten。

    之前 switch 只支持 ext_gn_ths / ext_hy_ths, 定时拉取链路会原样写入脏行,
    字段名不匹配 (上游返回 industry/flow_amount/direction/date), 看板读取时静默空。
    """
    from app.services.ext_pull import _apply_preset_flatten
    rows = [{"industry": "半导体", "flow_amount": 3.21, "direction": "in", "date": "2026-09-08"}]
    out = _apply_preset_flatten("ext_capital_flow", rows)
    assert out == [{
        "date": "2026-09-08",
        "industry": "半导体",
        "flow_amount": 3.21,
        "direction": "in",
    }]
    # 非预设 id 原样返回
    assert _apply_preset_flatten("custom_xxx", [{"a": 1}]) == [{"a": 1}]


def test_flatten_capital_flow_rows_em_format():
    """东方财富板块接口 (f14/f62/f3) -> 本地 schema:
    f62 元 / 1e8 转亿, direction 由 f62 符号推导, day 注入 date 字段。
    """
    from datetime import date
    raw = [
        {"f14": "通信设备", "f62": 5101434624.0, "f3": 0.35},
        {"f14": "元件",     "f62": 4962682368.0, "f3": 2.14},
        {"f14": "通信",     "f62": -123456789.0, "f3": -0.03},
        {"f14": "钢铁",     "f62": 0,           "f3": 0.0},
    ]
    out = ext_presets._flatten_capital_flow_rows(raw, day=date(2026, 9, 9))
    assert len(out) == 4
    assert out[0]["industry"] == "通信设备"
    assert out[0]["flow_amount"] == 51.0143  # 5101434624 / 1e8 = 51.01434624, round 4 位
    assert out[0]["direction"] == "in"
    assert out[0]["date"] == "2026-09-09"
    assert out[2]["direction"] == "out"
    assert out[2]["flow_amount"] == -1.2346  # -123456789 / 1e8 ≈ -1.23, round 4 位
    assert out[3]["direction"] == "new"  # f62 == 0


def test_flatten_capital_flow_rows_em_no_day_keeps_empty_date():
    """day 未传入时, EM 形态行的 date 字段留空字符串 (由调用方按 timeseries 自行取最新日)。"""
    raw = [{"f14": "通信设备", "f62": 1e8, "f3": 0.5}]
    out = ext_presets._flatten_capital_flow_rows(raw)
    assert out[0]["date"] == ""
    assert out[0]["flow_amount"] == 1.0
    assert out[0]["direction"] == "in"


def test_symbol_check_skipped_when_no_mapped_declared():
    """资金流向类 (按板块聚合) symbol_map/code_map 都为空, 跳过 symbol/code 校验。
    避免 ext_capital_flow 等「按非标的维度聚合」数据源被误拒。
    """
    from app.services.ext_pull import _assert_symbol_present_if_needed
    from app.services.ext_data import ExtConfig, ExtField, PullConfig

    cfg = ExtConfig(
        id="ext_capital_flow", label="全市场板块资金流向", mode="timeseries",
        fields=[ExtField("industry", "string", "板块")],
        symbol_map={}, code_map={},
        pull=PullConfig(url=""),
    )
    rows = [{"industry": "通信设备", "flow_amount": 10.0}]
    _assert_symbol_present_if_needed(rows, cfg)  # 不抛


def test_symbol_check_raises_when_mapped_declared_but_col_missing():
    """ext_hy_ths / ext_gn_ths 旧契约: 声明 mapped 但数据缺 col 时仍报错提醒。"""
    import pytest
    from app.services.ext_pull import _assert_symbol_present_if_needed
    from app.services.ext_data import ExtConfig, ExtField, PullConfig

    cfg = ExtConfig(
        id="ext_hy_ths", label="扩展行业", mode="snapshot",
        fields=[ExtField("name", "string", "名称")],
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={},
        pull=PullConfig(url=""),
    )
    rows = [{"name": "万科A"}]
    with pytest.raises(ValueError, match="缺少 symbol/code"):
        _assert_symbol_present_if_needed(rows, cfg)


def test_timeseries_no_dedup_avoids_single_row_bug():
    """regression: ext_capital_flow 写入 parquet 后只剩 1 行 (issue: 去重 key 用 date 列)

    之前 write_ext_parquet 对无 symbol 列的 timeseries 模式用 df.columns[0] (=date) 作
    unique subset, 全部 496 行同日 → 只剩 1 行。
    修复后: timeseries 模式跳过合并去重, 当日数据全量覆盖。
    """
    import os
    import tempfile
    import polars as pl
    from datetime import date
    from app.services.ext_data import ExtConfig, ExtField, PullConfig, write_ext_parquet, ExtConfigStore

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        tmp = Path(tmp)
        cfg = ExtConfig(
            id="ext_capital_flow", label="t", mode="timeseries",
            fields=[
                ExtField("date", "string", "d"),
                ExtField("industry", "string", "i"),
                ExtField("flow_amount", "float", "a"),
                ExtField("direction", "string", "dir"),
            ],
            symbol_map={}, code_map={},
            pull=PullConfig(url=""),
        )
        # 第一次写入: 5 行, 同日, 不同 industry
        rows1 = [
            {"date": "2026-09-10", "industry": "A", "flow_amount": 1.0, "direction": "in"},
            {"date": "2026-09-10", "industry": "B", "flow_amount": 2.0, "direction": "in"},
            {"date": "2026-09-10", "industry": "C", "flow_amount": 3.0, "direction": "out"},
            {"date": "2026-09-10", "industry": "D", "flow_amount": -4.0, "direction": "out"},
            {"date": "2026-09-10", "industry": "E", "flow_amount": 5.0, "direction": "in"},
        ]
        n1 = write_ext_parquet(pl.DataFrame(rows1), cfg, tmp, snapshot_date=date(2026, 9, 10))
        assert n1 == 5

        # 第二次写入: 同日, 5 行 (模拟 refetch, 内容相同)
        n2 = write_ext_parquet(pl.DataFrame(rows1), cfg, tmp, snapshot_date=date(2026, 9, 10))
        assert n2 == 5, "timeseries refetch 应全量覆盖, 不应剩 1 行"

        # 第三次: 写入不同 industry 集合 (3 行), 仍然覆盖当日
        rows3 = [
            {"date": "2026-09-10", "industry": "X", "flow_amount": 1.0, "direction": "in"},
            {"date": "2026-09-10", "industry": "Y", "flow_amount": 2.0, "direction": "out"},
            {"date": "2026-09-10", "industry": "Z", "flow_amount": 3.0, "direction": "in"},
        ]
        n3 = write_ext_parquet(pl.DataFrame(rows3), cfg, tmp, snapshot_date=date(2026, 9, 10))
        assert n3 == 3

        # 读取最终 parquet 验证
        path = os.path.join(tmp, "ext_data", "ext_capital_flow", "timeseries", "date=2026-09-10", "part.parquet")
        df = pl.read_parquet(path)
        assert df.height == 3
        assert set(df["industry"].to_list()) == {"X", "Y", "Z"}


def test_flip_em_po_toggles_direction():
    """_flip_em_po: po=1 -> po=0, po=0 -> po=1, 无 po -> 不变。"""
    from app.services.ext_pull import _flip_em_po
    base = "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62"
    assert _flip_em_po(base) == base.replace("po=1", "po=0")
    assert _flip_em_po(base.replace("po=1", "po=0")) == base
    # 无 po 参数 -> 不变 (无操作)
    no_po = base.replace("po=1&", "")
    assert _flip_em_po(no_po) == no_po


def test_fetch_and_ingest_merges_reverse_direction_for_capital_flow():
    """regression: ext_capital_flow 拉取后只 100 行同方向, 看板看不到 outflow。

    修复后 fetch_and_ingest 检测单方向结果时自动翻转 po 追加反方向拉取,
    合并写入 (100 in + 100 out = 200 行)。
    """
    import asyncio
    import tempfile
    from pathlib import Path
    from unittest.mock import AsyncMock, patch
    import polars as pl
    from datetime import date
    from app.services.ext_data import ExtConfig, ExtField, PullConfig
    from app.services.ext_pull import fetch_and_ingest

    cfg = ExtConfig(
        id="ext_capital_flow", label="t", mode="timeseries",
        fields=[
            ExtField("date", "string", "d"),
            ExtField("industry", "string", "i"),
            ExtField("flow_amount", "float", "a"),
            ExtField("direction", "string", "dir"),
        ],
        pull=PullConfig(
            url="https://example.test/x?pn=1&pz=100&po=1&np=1&fltt=2&fid=f62",
            response_path="data.diff",
            field_map={},
        ),
    )

    # 第一次 (po=1) 返回 5 行 in, 第二次 (po=0) 返回 5 行 out
    in_rows = [{"f14": "A", "f62": 1e8, "f3": 0.5} for _ in range(5)]
    out_rows = [{"f14": "Z", "f62": -1e8, "f3": -0.5} for _ in range(5)]
    captured_urls = []
    async def fake_request_json(pull, config_id, day=None):
        captured_urls.append(pull.url)
        if "po=1" in pull.url:
            return {"data": {"diff": in_rows}}
        return {"data": {"diff": out_rows}}
    with patch("app.services.ext_pull._request_json", side_effect=fake_request_json):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            n, d = asyncio.run(fetch_and_ingest(cfg, tmp, target_date=date(2026, 9, 10)))
            assert n == 10, f"期望合并后 10 行 (5 in + 5 out), 实际 {n}"
            assert d == "2026-09-10"
            assert len(captured_urls) == 2
            assert "po=1" in captured_urls[0]
            assert "po=0" in captured_urls[1]


def test_fetch_and_ingest_skips_reverse_when_already_bidirectional():
    """如果一次拉取已经包含 in+out 双方向 (极端情况), 不重复翻转拉取。"""
    import asyncio
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from datetime import date
    from app.services.ext_data import ExtConfig, ExtField, PullConfig
    from app.services.ext_pull import fetch_and_ingest

    cfg = ExtConfig(
        id="ext_capital_flow", label="t", mode="timeseries",
        fields=[ExtField("d","string"),ExtField("i","string"),ExtField("a","float"),ExtField("dir","string")],
        pull=PullConfig(url="https://example.test/x?pn=1&po=1", response_path="data.diff", field_map={}),
    )
    # 混合方向
    rows = [
        {"f14": "A", "f62": 1e8, "f3": 0.5},
        {"f14": "B", "f62": -1e8, "f3": -0.5},
    ]
    captured_urls = []
    async def fake_request_json(pull, config_id, day=None):
        captured_urls.append(pull.url)
        return {"data": {"diff": rows}}
    with patch("app.services.ext_pull._request_json", side_effect=fake_request_json):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            n, _ = asyncio.run(fetch_and_ingest(cfg, tmp, target_date=date(2026, 9, 10)))
            assert n == 2
            assert len(captured_urls) == 1, "双向数据不应触发反向拉取"


def test_symbol_check_passes_when_mapped_col_present():
    """声明 mapped 的 col 实际出现在数据行里, 校验通过。"""
    from app.services.ext_pull import _assert_symbol_present_if_needed
    from app.services.ext_data import ExtConfig, ExtField, PullConfig

    cfg = ExtConfig(
        id="ext_hy_ths", label="扩展行业", mode="snapshot",
        fields=[ExtField("股票代码", "string", "代码")],
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={},
        pull=PullConfig(url=""),
    )
    rows = [{"股票代码": "000002"}]
    _assert_symbol_present_if_needed(rows, cfg)  # 不抛


def test_flatten_capital_flow_rows_em_skips_empty_f14():
    """EM 形态下空 f14 一律跳过, 与自定义形态保持一致。"""
    raw = [
        {"f14": "", "f62": 1e9, "f3": 0.5},
        {"f14": "通信", "f62": None, "f3": 0.5},  # None 金额 -> 0 -> new
    ]
    out = ext_presets._flatten_capital_flow_rows(raw)
    assert len(out) == 1
    assert out[0]["industry"] == "通信"
    assert out[0]["flow_amount"] == 0.0
    assert out[0]["direction"] == "new"


def test_apply_preset_flatten_passes_day_to_capital_flow():
    """ext_pull._apply_preset_flatten 必须把 day 透传给 _flatten_capital_flow_rows。

    否则 EM 形态行的 date 字段为空, timeseries 看板读取最新日会拿到 None。
    """
    from datetime import date
    from app.services.ext_pull import _apply_preset_flatten
    rows = [{"f14": "通信设备", "f62": 1e9, "f3": 0.5}]
    out = _apply_preset_flatten("ext_capital_flow", rows, day=date(2026, 9, 9))
    assert out[0]["date"] == "2026-09-09"
    assert out[0]["flow_amount"] == 10.0
    assert out[0]["direction"] == "in"

