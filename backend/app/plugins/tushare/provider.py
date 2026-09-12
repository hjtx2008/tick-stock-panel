"""Tushare Pro 内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily       A 股日K, 未复权原始价(项目契约: 复权一律由 pipeline 用本地因子计算)
  - adj_factor  A 股除权因子, 由官方累积因子序列还原为单事件 pre/post 比值
  - financial   财务: metrics(fina_indicator 批量) / income / balance_sheet / cash_flow
                (三者逐只) / shares(daily_basic 按日批量)
未声明 minute / realtime → provider_has_dataset 为 False, 自动回退现有源。

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - daily.vol     成交量, 单位 **手**   → 与项目日K契约一致, **不转换**
  - daily.amount  成交额, 单位 **千元** → 此处 ** 乘 1000** 转元 (项目契约 amount 为元)
  - adj_factor    官方给的是**每个交易日一个值的累积因子**(非单事件比值),
                  本层还原为项目契约要求的单事件 pre/post 比值, 见 _to_event_ratios。

取数模式 (官方建议: 循环日期而非循环 ts_code):
  - 标的数 >= _DATE_MODE_MIN_SYMBOLS: 按交易日逐日取全市场(单次约 5400 行, 一天一次请求),
    全市场一年历史 ≈ 243 次请求; 逐标的模式则需 5000+ 次。
  - 标的数较少: 逐标的取区间(分页兜底), 避免为几只股票拉回全市场再丢弃。
  - 交易日历(trade_cal)不可用时自动回退逐标的模式。
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.tushare import client as tushare_client
from app.plugins.tushare.client import TushareClient, TushareError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余 provider_has_dataset 返回 False → 回退现有数据源
_DATASETS = ("daily", "adj_factor", "financial")

API_KEY_ENV = "TUSHARE_API_KEY"
SECRETS_FIELD = "tushare_api_key"  # UI 配置的 Token 存 secrets.json, 优先级高于 .env

_DATE_MODE_MIN_SYMBOLS = 100   # 标的数达到此值才走"按交易日取全市场"模式
_SYMBOL_BATCH = 30             # 逐标的模式: 每批标的产出一次 chunk(与 on_chunk_done 进度对齐)
_ADJ_BASELINE_BACKDAYS = 30    # 除权因子需窗口前一个交易日作基准, 向前多取的自然日(覆盖长假)
_ADJ_EPS = 1e-6                # 因子未变化(非除权日)的判定阈值

# ---- 财务 ----
_FINANCIAL_HISTORY_PERIODS = 8        # 与 fuyao 插件一致: 首装全量历史取最近 8 期
_FIN_LATEST_WINDOW_DAYS = 460         # latest_only: 按 ann_date 回看 ~15 个月(必含最新年报+季报)
_FIN_HISTORY_WINDOW_DAYS = 900        # 全量历史: 回看 ~30 个月 → 覆盖 8 期季报
_FIN_SYMBOL_INTERVAL_S = 0.08         # 逐只请求节流(实测 30 连发无报错, 留余量)
_FIN_SHARES_MONTHS = 24               # shares 历史: 取最近 N 个月, 每月最后交易日
_FIN_FAIL_ABORT_RATIO = 0.5           # 失败标的占比超过此值 → 抛错中止(避免半截数据落库)

# 项目 canonical 表名 → Tushare 接口名
_STATEMENT_APIS = {
    "income": "income",
    "balance_sheet": "balancesheet",
    "cash_flow": "cashflow",
}

# Tushare 原始字段 → 项目 canonical 列 (对齐 fuyao 插件的 canonical 命名)
_INCOME_FIELD_MAP = {
    "revenue": "revenue",
    "oper_cost": "operating_cost",
    "sell_exp": "selling_expense",
    "admin_exp": "admin_expense",
    "rd_exp": "rd_expense",
    "fin_exp": "financial_expense",
    "operate_profit": "operating_profit",
    "total_profit": "total_profit",
    "income_tax": "income_tax",
    "n_income": "net_income",                    # 净利润(含少数股东损益)
    "n_income_attr_p": "net_income_attributable",  # 归属母公司净利润
    "basic_eps": "basic_eps",
    "total_revenue": "total_revenue",            # 营业总收入(canonical 外扩展列)
}
_BALANCE_FIELD_MAP = {
    "total_assets": "total_assets",
    "total_cur_assets": "total_current_assets",
    "total_nca": "total_non_current_assets",
    "money_cap": "cash_and_equivalents",
    "accounts_receiv": "accounts_receivable",
    "total_liab": "total_liabilities",
    # 含少数股东权益 = 股东权益合计(实测 total_assets - total_liab 恰等于此列)
    "total_hldr_eqy_inc_min_int": "total_equity",
    "total_hldr_eqy_exc_min_int": "total_equity_attributable",
}
_CASHFLOW_FIELD_MAP = {
    "n_cashflow_act": "net_operating_cash_flow",
    "n_cashflow_inv_act": "net_investing_cash_flow",
    "n_cash_flows_fnc_act": "net_financing_cash_flow",
    "c_pay_acq_const_fiolta": "capex",
    "n_incr_cash_cash_equ": "net_cash_change",
}
_METRICS_FIELD_MAP = {
    "roe": "roe",
    "roa": "roa",
    "grossprofit_margin": "gross_margin",
    "netprofit_margin": "net_margin",
    "debt_to_assets": "debt_to_asset_ratio",
    "or_yoy": "revenue_yoy",
    "netprofit_yoy": "net_income_yoy",
    "eps": "eps",
    "bps": "bps",
}


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: Token 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    # 状态行会拼在「未配置」标签之后, 文案不再重复"未配置"字样
    return False, f"缺少 Token(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Token 实探一次轻量接口(先探后存, 对齐 /tickflow-key 语义)。不落盘。"""
    client = None
    try:
        client = tushare_client.TushareClient(api_key=api_key, timeout=10.0, interval=0.0)
        rows = client.probe()
    except TushareError as e:
        return False, f"Token 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
    if not rows:
        return False, "Token 可用但接口无返回(请确认积分是否覆盖 stock_basic)"
    return True, "ok"


@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "tushare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    """Tushare 大量字段可能为 None/空串, 统一转 float; 不可转返回 None(不启发式补值)。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_event_ratios(df: pl.DataFrame) -> pl.DataFrame:
    """官方累积因子序列 → 项目契约的单事件 pre/post 比值。

    推导 (与基准无关, 前复权/后复权口径都成立):
      复权价 = close(t) * f(t) 在除权日必须连续(无跳空), 即
        close(d_prev) * f(d_prev) = close(d) * f(d)
      除权日真实涨跌幅为 0 时 close(d) = 除权参考价 R, 故
        f(d) / f(d_prev) = close(d_prev) / R = pre/post 比值
    与 fuyao 插件的 p/ref、项目 pipeline 的 cum_prod 口径一致。

    df: symbol / trade_date / adj_factor(已按 symbol,trade_date 排序去重)
    返回: symbol / trade_date / ex_factor, 仅保留因子真正发生变化的除权日。
    """
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("adj_factor").shift(1).over("symbol").alias("_prev_factor"))
        .with_columns((pl.col("adj_factor") / pl.col("_prev_factor")).alias("ex_factor"))
        .filter(
            pl.col("_prev_factor").is_not_null()
            & (pl.col("_prev_factor") != 0)
            & ((pl.col("ex_factor") - 1.0).abs() > _ADJ_EPS)
        )
        .select("symbol", "trade_date", "ex_factor")
    )


def _iso_date(value) -> str | None:
    """Tushare YYYYMMDD → ISO 'YYYY-MM-DD'; 不可解析返回 None(不伪造日期)。"""
    d = tushare_client._parse_date(value)
    return d.isoformat() if d is not None else None


def _month_end_days(days: list[date], months: int) -> list[date]:
    """从升序交易日列表中取最近 months 个"每月最后交易日"。

    shares 表存的是股本时间序列, 逐日取 24 个月 = 480 次请求不划算,
    按月抽样(24 次)即可支撑"历史流通股本"的插值使用。
    """
    if not days:
        return []
    buckets: dict[tuple[int, int], date] = {}
    for d in days:
        key = (d.year, d.month)
        buckets[key] = d  # days 升序 → 同月最后一个覆盖为最晚交易日
    picked = sorted(buckets.values())
    return picked[-months:] if months > 0 else picked


def _fin_frame(rows: list[dict], field_map: dict[str, str], periods: int) -> pl.DataFrame:
    """财务原始行 → canonical 帧。

    - 同一报告期(end_date)官方可能返回多行(合并/单季/调整), 只保留
      「report_type=1 优先, 其次 ann_date 最新」的一行, 避免同期重复落库。
    - 按 (symbol, period_end) 去重后每只保留最近 periods 期。
    - 字段缺失一律 None, 不做任何启发式补值。
    """
    out: list[dict] = []
    for r in rows:
        code = r.get("ts_code")
        period = _iso_date(r.get("end_date"))
        if not code or not period:
            continue
        row: dict = {
            "symbol": code,
            "period_end": period,
            "announce_date": _iso_date(r.get("ann_date")),
            "_rt": 0 if str(r.get("report_type") or "") == "1" else 1,
        }
        for src, dst in field_map.items():
            row[dst] = _to_float(r.get(src))
        out.append(row)
    if not out:
        return pl.DataFrame()

    df = pl.DataFrame(out)
    # _rt: report_type=1(合并报表)优先; 同期内再取 ann_date 最新
    df = (
        df.sort(["symbol", "period_end", "_rt", "announce_date"],
                descending=[False, False, False, True])
        .unique(subset=["symbol", "period_end"], keep="first")
        .drop("_rt")
    )
    if periods > 0:
        df = (
            df.sort(["symbol", "period_end"], descending=[False, True])
            .group_by("symbol", maintain_order=True)
            .head(periods)
            .sort(["symbol", "period_end"])
        )
    return df


class TushareProvider:
    """Tushare Pro 数据源。daily / adj_factor 两个数据集。"""

    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self._client: TushareClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TushareClient:
        if self._client is None:
            self._client = tushare_client.TushareClient(api_key=get_api_key())
        return self._client

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股日K → 内部契约。未复权原始价、vol 手不转换、amount 千元乘 1000 转元。"""
        chunks = [
            df
            for df in self.iter_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """分批产出日K, 供历史同步逐批落盘, 避免全市场结果累积在内存。

        Tushare daily 仅覆盖股票(ETF/指数另有 fund_daily/index_daily), 故
        asset_type != "stock" 时直接不产出 → 上层回退现有数据源。
        """
        if not symbols or asset_type != "stock":
            return
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        symset = set(symbols)
        client = self._get_client()

        days: list[date] = []
        if len(symset) >= _DATE_MODE_MIN_SYMBOLS:
            try:
                days = client.trade_days(start_d, end_d)
            except TushareError as e:
                logger.warning("Tushare 交易日历不可用, 回退逐标的模式: %s", e)
                days = []
        if days:
            yield from self._iter_daily_by_date(client, days, symset, on_chunk_done)
        else:
            yield from self._iter_daily_by_symbol(
                client, sorted(symset), start_d, end_d, on_chunk_done
            )

    def _iter_daily_by_date(
        self,
        client: TushareClient,
        days: list[date],
        symset: set[str],
        on_chunk_done: Callable[[int, int], None] | None,
    ) -> Iterator[pl.DataFrame]:
        """逐交易日取全市场日K, 按请求标的过滤后逐日产出。"""
        total = len(days)
        failed = 0
        for done, day in enumerate(days, start=1):
            try:
                rows = client.daily_by_date(day)
            except TushareError as e:
                failed += 1
                logger.warning("Tushare 日K拉取失败 %s: %s", day, e)
            else:
                df = _daily_frame(rows, symset)
                if not df.is_empty():
                    yield df
            if on_chunk_done:
                on_chunk_done(done, total)
        _check_failure("日K", failed, total, unit="个交易日")

    def _iter_daily_by_symbol(
        self,
        client: TushareClient,
        symbols: list[str],
        start_d: date,
        end_d: date,
        on_chunk_done: Callable[[int, int], None] | None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的取区间日K, 每 _SYMBOL_BATCH 只产出一次。"""
        batches = [
            symbols[i : i + _SYMBOL_BATCH] for i in range(0, len(symbols), _SYMBOL_BATCH)
        ]
        total = len(batches)
        failed = 0
        for done, batch in enumerate(batches, start=1):
            rows: list[dict] = []
            for sym in batch:
                try:
                    rows.extend(client.daily_by_symbol(sym, start_d, end_d))
                except TushareError as e:
                    failed += 1
                    logger.warning("Tushare 日K拉取失败 %s: %s", sym, e)
            df = _daily_frame(rows, None)
            if not df.is_empty():
                yield df
            if on_chunk_done:
                on_chunk_done(done, total)
        _check_failure("日K", failed, max(1, len(symbols)), unit="只标的")

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股除权因子 → 内部契约(symbol/trade_date/ex_factor, 单事件比值非累积)。

        ex_factor 需要"窗口前一个交易日"的因子作基准, 故实际取数窗口向前多
        _ADJ_BASELINE_BACKDAYS 个自然日; 还原后的事件再按请求窗口过滤。
        """
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=schema)
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        symset = set(symbols)
        client = self._get_client()
        # 基准窗口: 向前多取一段自然日, 保证首个除权事件能拿到 f(d_prev)
        raw_start = start_d - timedelta(days=_ADJ_BASELINE_BACKDAYS)

        rows: list[dict] = []
        try:
            days = (
                client.trade_days(raw_start, end_d)
                if len(symset) >= _DATE_MODE_MIN_SYMBOLS
                else []
            )
        except TushareError as e:
            logger.warning("Tushare 交易日历不可用, 回退逐标的模式: %s", e)
            days = []

        if days:
            total = len(days)
            failed = 0
            for done, day in enumerate(days, start=1):
                try:
                    rows.extend(client.adj_factor_by_date(day))
                except TushareError as e:
                    failed += 1
                    logger.warning("Tushare 除权因子拉取失败 %s: %s", day, e)
                if on_chunk_done:
                    on_chunk_done(done, total)
            _check_failure("除权因子", failed, total, unit="个交易日")
        else:
            syms = sorted(symset)
            total = len(syms)
            failed = 0
            for done, sym in enumerate(syms, start=1):
                try:
                    rows.extend(client.adj_factor_by_symbol(sym, raw_start, end_d))
                except TushareError as e:
                    failed += 1
                    logger.warning("Tushare 除权因子拉取失败 %s: %s", sym, e)
                if on_chunk_done:
                    on_chunk_done(done, total)
            _check_failure("除权因子", failed, max(1, total), unit="只标的")

        factors = _factor_frame(rows, symset)
        if factors.is_empty():
            return pl.DataFrame(schema=schema)
        events = _to_event_ratios(factors).filter(
            (pl.col("trade_date") >= start_d) & (pl.col("trade_date") <= end_d)
        )
        if events.is_empty():
            return pl.DataFrame(schema=schema)
        return events.sort(["symbol", "trade_date"]).with_columns(
            pl.col("ex_factor").cast(pl.Float64)
        )

    # ---- 测试(设置页试拉) ----
    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """财务数据 → canonical 列(symbol/period_end/announce_date/指标)。

        - metrics: fina_indicator **支持批量**(单次封顶 100 行, 客户端折半兜底)
          → 全市场 ≈ 111 次请求, 秒级。这是 Tushare 相对逐只源的最大优势。
        - income / balance_sheet / cash_flow: ts_code **硬必填且不支持批量**
          (实测多 code 返回 0 行且不报错) → 逐只, 全市场 3 表 ≈ 16650 次请求。
        - shares: daily_basic 按交易日取全市场(单次约 5550 行, 1 请求/天),
          total_share/float_share 单位 **万股 → 乘 10000 转股**。
        """
        if not symbols:
            return pl.DataFrame()
        if table == "metrics":
            return self._financial_metrics(symbols, latest_only)
        if table in _STATEMENT_APIS:
            field_map = {
                "income": _INCOME_FIELD_MAP,
                "balance_sheet": _BALANCE_FIELD_MAP,
                "cash_flow": _CASHFLOW_FIELD_MAP,
            }[table]
            return self._financial_statements(table, field_map, symbols, latest_only)
        if table == "shares":
            return self._financial_shares(symbols, latest_only)
        return pl.DataFrame()

    def _financial_metrics(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        client = self._get_client()
        window = _FIN_LATEST_WINDOW_DAYS if latest_only else _FIN_HISTORY_WINDOW_DAYS
        end = date.today()
        start = end - timedelta(days=window)
        rows = client.fina_indicator_batch(list(symbols), start, end)
        if not rows:
            return pl.DataFrame()
        periods = 1 if latest_only else _FINANCIAL_HISTORY_PERIODS
        return _fin_frame(rows, _METRICS_FIELD_MAP, periods)

    def _financial_statements(
        self,
        table: str,
        field_map: dict[str, str],
        symbols: list[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        api_name = _STATEMENT_APIS[table]
        client = self._get_client()
        window = _FIN_LATEST_WINDOW_DAYS if latest_only else _FIN_HISTORY_WINDOW_DAYS
        end = date.today()
        start = end - timedelta(days=window)
        periods = 1 if latest_only else _FINANCIAL_HISTORY_PERIODS

        all_rows: list[dict] = []
        failed = 0
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_FIN_SYMBOL_INTERVAL_S)
            try:
                all_rows.extend(client.statement_by_symbol(api_name, sym, start, end))
            except TushareError as e:
                failed += 1
                logger.warning("Tushare 财务 %s %s 失败: %s", table, sym, e)
        if symbols and failed / len(symbols) > _FIN_FAIL_ABORT_RATIO:
            raise TushareError(
                f"Tushare 财务 {table} 失败率过高: {failed}/{len(symbols)}"
            )
        if not all_rows:
            return pl.DataFrame()
        return _fin_frame(all_rows, field_map, periods)

    def _financial_shares(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        """总股本/流通股本: daily_basic 按交易日批量取, 单位万股 → 股。"""
        client = self._get_client()
        end = date.today()
        days: list[date] = []
        try:
            days = client.trade_days(end - timedelta(days=_FIN_SHARES_MONTHS * 32), end)
        except TushareError as e:
            logger.warning("Tushare 交易日历不可用, shares 回退最近交易日: %s", e)
        days = _month_end_days(days, _FIN_SHARES_MONTHS) if not latest_only else (days[-1:] if days else [])

        out: list[dict] = []
        for day in days:
            try:
                rows = client.daily_basic_by_date(day)
            except TushareError as e:
                logger.warning("Tushare daily_basic %s 失败: %s", day, e)
                continue
            wanted = set(symbols)
            for r in rows:
                code = r.get("ts_code")
                if code not in wanted:
                    continue
                total_wan = _to_float(r.get("total_share"))
                float_wan = _to_float(r.get("float_share"))
                out.append(
                    {
                        "symbol": code,
                        # 股本以"该交易日可用"落库: period_end 记交易日, 与
                        # share_capital.apply_historical_float_shares 的口径一致
                        "period_end": day.isoformat(),
                        "announce_date": day.isoformat(),
                        "total_shares": None if total_wan is None else total_wan * 10000,
                        "float_shares": None if float_wan is None else float_wan * 10000,
                    }
                )
        if not out:
            return pl.DataFrame()
        return (
            pl.DataFrame(out)
            .sort(["symbol", "period_end"])
            .unique(subset=["symbol", "period_end"], keep="last")
        )

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉测试」。返回 rows / columns / preview, 失败返回 error。"""
        if dataset == "daily":
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
            except TushareError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            return _preview(self.name, dataset, df)

        if dataset == "adj_factor":
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                df = self.get_adj_factors(
                    syms, datetime.now() - timedelta(days=365), datetime.now()
                )
            except TushareError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            return _preview(self.name, dataset, df)

        if dataset == "financial":
            # 试拉只验证连通性与字段, 固定走 metrics(批量接口, 秒级返回);
            # 三表逐只模式耗时与标的数成正比, 不适合放在"试拉"里。
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ", "600519.SH"]
            try:
                df = self.get_financials("metrics", syms, latest_only=True)
            except TushareError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            return _preview(self.name, dataset, df)

        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": 0,
            "error": f"Tushare 插件未接入 {dataset} 数据集(自动回退现有数据源)",
        }


def _daily_frame(rows: list[dict], symset: set[str] | None) -> pl.DataFrame:
    """Tushare daily 原始行 → 内部日K契约 (vol 手不转换, amount 千元 乘 1000 转元)。

    symset 为 None 时不做标的过滤(逐标的模式结果本就是请求标的)。
    """
    out: list[dict] = []
    for r in rows:
        code = r.get("ts_code")
        if not code:
            continue
        if symset is not None and code not in symset:
            continue
        d = _parse_trade_date(r.get("trade_date"))
        if d is None:
            continue
        amount = _to_float(r.get("amount"))
        out.append(
            {
                "symbol": str(code),
                "date": d,
                "open": _to_float(r.get("open")),
                "high": _to_float(r.get("high")),
                "low": _to_float(r.get("low")),
                "close": _to_float(r.get("close")),
                "volume": _to_float(r.get("vol")),  # 手, 与项目契约一致
                # Tushare 单位为千元, 项目契约为元 (乘 1000)
                "amount": amount * 1000.0 if amount is not None else None,
            }
        )
    if not out:
        return pl.DataFrame()
    return normalize_daily(out, source="tushare")


def _factor_frame(rows: list[dict], symset: set[str]) -> pl.DataFrame:
    """Tushare adj_factor 原始行 → symbol/trade_date/adj_factor(去重排序)。"""
    out: list[dict] = []
    for r in rows:
        code = r.get("ts_code")
        if not code or code not in symset:
            continue
        d = _parse_trade_date(r.get("trade_date"))
        factor = _to_float(r.get("adj_factor"))
        if d is None or factor is None:
            continue
        out.append({"symbol": str(code), "trade_date": d, "adj_factor": factor})
    if not out:
        return pl.DataFrame()
    return (
        pl.DataFrame(out)
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )


def _parse_trade_date(value) -> date | None:
    """YYYYMMDD(字符串或数字) → date。Tushare 也可能返回 YYYY-MM-DD, 两种都兼容。"""
    if value is None:
        return None
    text = str(value).strip()
    if "-" in text:
        text = text.replace("-", "")
    return tushare_client._parse_date(text)


def _check_failure(what: str, failed: int, total: int, unit: str) -> None:
    """失败比例过半时抛错, 避免"半截数据"被静默落库(资金流向面板的教训)。

    少量失败(如个别日期接口抖动)只告警并继续, 已产出的数据本身是正确的。
    """
    if total <= 0 or failed <= 0:
        return
    if failed * 2 >= total:
        raise TushareError(f"Tushare {what}拉取失败 {failed}/{total} {unit}, 已中止以避免写入不完整数据")
    logger.warning("Tushare %s拉取部分失败: %d/%d %s", what, failed, total, unit)


def _preview(provider: str, dataset: str, df: pl.DataFrame) -> dict:
    """DataFrame → 设置页试拉结果 (date/datetime 转 ISO 保证 JSON 可序列化)。"""
    head = df.head(5).to_dicts()
    for row in head:
        for k, v in list(row.items()):
            if isinstance(v, (date, datetime)):
                row[k] = v.isoformat()
    return {
        "provider": provider,
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": head,
    }
