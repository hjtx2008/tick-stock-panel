"""Tushare Pro 内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily       A 股日K, 未复权原始价(项目契约: 复权一律由 pipeline 用本地因子计算)
  - adj_factor  A 股除权因子, 由官方累积因子序列还原为单事件 pre/post 比值
未声明 minute / realtime / financial → provider_has_dataset 为 False, 自动回退现有源。

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.tushare import client as tushare_client
from app.plugins.tushare.client import TushareClient, TushareError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余 provider_has_dataset 返回 False → 回退现有数据源
_DATASETS = ("daily", "adj_factor")

API_KEY_ENV = "TUSHARE_API_KEY"
SECRETS_FIELD = "tushare_api_key"  # UI 配置的 Token 存 secrets.json, 优先级高于 .env

_DATE_MODE_MIN_SYMBOLS = 100   # 标的数达到此值才走"按交易日取全市场"模式
_SYMBOL_BATCH = 30             # 逐标的模式: 每批标的产出一次 chunk(与 on_chunk_done 进度对齐)
_ADJ_BASELINE_BACKDAYS = 30    # 除权因子需窗口前一个交易日作基准, 向前多取的自然日(覆盖长假)
_ADJ_EPS = 1e-6                # 因子未变化(非除权日)的判定阈值


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
