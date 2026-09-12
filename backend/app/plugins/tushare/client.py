"""Tushare Pro (https://tushare.pro) HTTP 客户端。

职责: 认证(token 置于请求体)、统一信封解包、分页、节流。不感知 provider / services 层。

协议要点 (与项目声明式 YAML 源结构性不兼容, 故必须走插件):
  - 所有接口共用一个端点, 单次 POST, 请求体为
    {"api_name": ..., "token": ..., "params": {...}, "fields": ...};
    token 在 body 内, 参数嵌套在 params 里。
  - 响应: {"code": 0, "msg": null, "data": {"fields": [...], "items": [[...]]}}
    列名与二维行分离 — 由本客户端组装成 dict 列表供上层消费。

单位口径 (官方文档确认, CONTRIBUTING §3.1 要求不凭字段名推断):
  - daily.vol     成交量, 单位 **手**   → 与项目日K契约一致, 不转换
  - daily.amount  成交额, 单位 **千元** → provider 层 乘 1000 转元
  - adj_factor    复权因子, **每个交易日一个值的累积序列**(非单事件比值)
                  → provider 层按 f(d)/f(d-1) 还原为单事件 pre/post 比值

取数模式: 官方明确建议"循环日期提取全市场, 不要循环 ts_code 拉历史" —
  单次请求上限 6000 行, 而全市场约 5400 只, 故按交易日取数一天一次请求即可拉完。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import date

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tushare.pro"

_PAGE_SIZE = 6000        # 单次返回上限(官方文档: 每次 6000 条)
_MAX_PAGES = 500         # 分页保护: 单接口最多翻 500 页(=300 万行), 防御死循环
_DEFAULT_INTERVAL_S = 0.15  # 请求节流: 约 400 次/分, 留出限频余量

_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
_ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
_TRADE_CAL_FIELDS = "cal_date,is_open"

# ---- 财务字段清单 (2026-09 实测确认存在, 缺失字段会被接口静默忽略) ----
# 实测被拒/不存在的: ebit_yoy, or_last_year (已从清单剔除)
_FINA_INDICATOR_FIELDS = (
    "ts_code,ann_date,end_date,eps,dt_eps,bps,roe,roe_waa,roe_dt,roa,"
    "netprofit_margin,grossprofit_margin,debt_to_assets,netprofit_yoy,or_yoy,ocfps,op_yoy,tr_yoy"
)
_INCOME_FIELDS = (
    "ts_code,ann_date,end_date,report_type,revenue,oper_cost,sell_exp,admin_exp,rd_exp,"
    "fin_exp,operate_profit,total_profit,income_tax,n_income,n_income_attr_p,basic_eps,total_revenue"
)
_BALANCE_FIELDS = (
    "ts_code,ann_date,end_date,report_type,total_assets,total_cur_assets,total_nca,"
    "money_cap,accounts_receiv,total_liab,total_hldr_eqy_inc_min_int,total_hldr_eqy_exc_min_int"
)
_CASHFLOW_FIELDS = (
    "ts_code,ann_date,end_date,report_type,n_cashflow_act,n_cashflow_inv_act,"
    "n_cash_flows_fnc_act,c_pay_acq_const_fiolta,n_incr_cash_cash_equ"
)
_DAILY_BASIC_FIELDS = "ts_code,trade_date,total_share,float_share,total_mv,circ_mv"

# fina_indicator 单次返回封顶 100 行(超出静默截断, 不报错)。
# 单标的在同一区间可能返回多行(多期), 故批量取 50 留一倍余量, 并在触顶时折半重拉。
_FINA_ROW_CAP = 100
_FINA_BATCH = 50
_MAX_HALVE_DEPTH = 3


class TushareError(Exception):
    """Tushare 接口错误(配置缺失 / 网络失败 / 信封 code != 0)。"""


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_date(value) -> date | None:
    """Tushare 日期字段(YYYYMMDD) → date。None/非法返回 None, 不伪造。"""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


class TushareClient:
    """Tushare Pro REST 客户端 (httpx.Client 可并发复用)。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        interval: float = _DEFAULT_INTERVAL_S,
    ) -> None:
        if not api_key:
            raise TushareError("未配置 TUSHARE_API_KEY")
        self._api_key = api_key
        self._interval = max(0.0, interval)
        self._last_call = 0.0
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # ---- 内部 ----
    def _throttle(self) -> None:
        """按 interval 节流。Tushare 按接口限频, 超限会返回错误码而非静默丢数据。"""
        if self._interval <= 0:
            return
        gap = time.monotonic() - self._last_call
        if 0 <= gap < self._interval:
            time.sleep(self._interval - gap)
        self._last_call = time.monotonic()

    def _post(self, body: dict) -> dict:
        self._throttle()
        try:
            resp = self._http.post("/", json=body)
        except httpx.HTTPError as e:
            raise TushareError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise TushareError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise TushareError(f"响应不是 JSON: {e}") from e
        code = payload.get("code")
        if code not in (0, "0", None):
            raise TushareError(f"Tushare 接口错误 code={code}: {payload.get('msg') or ''}")
        return payload

    def query(self, api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
        """单次查询 → dict 列表 (fields + items 组装)。无数据时返回空列表。"""
        body = {
            "api_name": api_name,
            "token": self._api_key,
            "params": params or {},
            "fields": fields or "",
        }
        payload = self._post(body)
        data = payload.get("data") or {}
        names = data.get("fields") or []
        items = data.get("items") or []
        if not names or not items:
            return []
        # strict=False: 个别接口可能出现 fields 与 items 长度不齐, 缺位补 None 而非整批丢弃
        return [dict(zip(names, row, strict=False)) for row in items]

    def query_paged(
        self, api_name: str, params: dict | None = None, fields: str = ""
    ) -> Iterator[list[dict]]:
        """分页查询, 逐页产出; 某页不足 _PAGE_SIZE 即视为末页。"""
        offset = 0
        for _ in range(_MAX_PAGES):
            page_params = dict(params or {})
            page_params["limit"] = _PAGE_SIZE
            page_params["offset"] = offset
            rows = self.query(api_name, page_params, fields)
            if not rows:
                return
            yield rows
            if len(rows) < _PAGE_SIZE:
                return
            offset += _PAGE_SIZE

    # ---- 语义封装 ----
    def trade_days(self, start: date, end: date) -> list[date]:
        """交易日历(trade_cal, is_open=1) → 升序交易日列表。

        失败抛 TushareError, 由 provider 回退到"逐标的"取数模式。
        """
        out: list[date] = []
        for page in self.query_paged(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": _yyyymmdd(start),
                "end_date": _yyyymmdd(end),
                "is_open": "1",
            },
            fields=_TRADE_CAL_FIELDS,
        ):
            for row in page:
                d = _parse_date(row.get("cal_date"))
                if d is not None and start <= d <= end:
                    out.append(d)
        return sorted(set(out))

    def daily_by_date(self, day: date) -> list[dict]:
        """按交易日取全市场日K (单次返回约 5400 行)。"""
        out: list[dict] = []
        for page in self.query_paged(
            "daily", {"trade_date": _yyyymmdd(day)}, fields=_DAILY_FIELDS
        ):
            out.extend(page)
        return out

    def daily_by_symbol(self, ts_code: str, start: date, end: date) -> list[dict]:
        """按单标的取区间日K (分页兜底: 长历史可能超单页上限)。"""
        out: list[dict] = []
        for page in self.query_paged(
            "daily",
            {"ts_code": ts_code, "start_date": _yyyymmdd(start), "end_date": _yyyymmdd(end)},
            fields=_DAILY_FIELDS,
        ):
            out.extend(page)
        return out

    def adj_factor_by_date(self, day: date) -> list[dict]:
        """按交易日取全市场复权因子。"""
        out: list[dict] = []
        for page in self.query_paged(
            "adj_factor", {"trade_date": _yyyymmdd(day)}, fields=_ADJ_FACTOR_FIELDS
        ):
            out.extend(page)
        return out

    def adj_factor_by_symbol(self, ts_code: str, start: date, end: date) -> list[dict]:
        """按单标的取区间复权因子。"""
        out: list[dict] = []
        for page in self.query_paged(
            "adj_factor",
            {"ts_code": ts_code, "start_date": _yyyymmdd(start), "end_date": _yyyymmdd(end)},
            fields=_ADJ_FACTOR_FIELDS,
        ):
            out.extend(page)
        return out

    # ---- 财务 ----
    def _query_capped(
        self,
        api_name: str,
        params: dict,
        fields: str,
        depth: int = 0,
    ) -> list[dict]:
        """带 100 行封顶保护的批量查询: 触顶即把 ts_code 折半重拉(避免静默截断丢数据)。

        分批上限本身已保守(_FINA_BATCH=50), 折半只是兜底: 若某批标的都有多期数据,
        50 只也可能返回 100 行被截断。
        """
        rows = self.query(api_name, params, fields)
        codes = str(params.get("ts_code") or "").split(",")
        if len(rows) >= _FINA_ROW_CAP and len(codes) > 1 and depth < _MAX_HALVE_DEPTH:
            mid = len(codes) // 2
            return self._query_capped(
                api_name, {**params, "ts_code": ",".join(codes[:mid])}, fields, depth + 1
            ) + self._query_capped(
                api_name, {**params, "ts_code": ",".join(codes[mid:])}, fields, depth + 1
            )
        return rows

    def fina_indicator_batch(
        self, ts_codes: list[str], start: date, end: date
    ) -> list[dict]:
        """批量财务指标。ts_code 逗号分隔, 按 _FINA_BATCH 分批 + 折半兜底。

        start/end 按 **ann_date(公告日)** 筛, 不是报告期。
        """
        out: list[dict] = []
        for i in range(0, len(ts_codes), _FINA_BATCH):
            chunk = ts_codes[i : i + _FINA_BATCH]
            out.extend(
                self._query_capped(
                    "fina_indicator",
                    {
                        "ts_code": ",".join(chunk),
                        "start_date": _yyyymmdd(start),
                        "end_date": _yyyymmdd(end),
                    },
                    fields=_FINA_INDICATOR_FIELDS,
                )
            )
        return out

    def statement_by_symbol(
        self, api_name: str, ts_code: str, start: date, end: date
    ) -> list[dict]:
        """三大报表(income/balancesheet/cashflow)按单标的取区间多期。

        实测: ts_code **硬必填**(不传报 50101), 且**不支持批量**(多 code 返回 0 行且不报错)
        → 只能逐只。start/end 按 ann_date 筛。
        """
        fields = {
            "income": _INCOME_FIELDS,
            "balancesheet": _BALANCE_FIELDS,
            "cashflow": _CASHFLOW_FIELDS,
        }[api_name]
        return self.query(
            api_name,
            {
                "ts_code": ts_code,
                "start_date": _yyyymmdd(start),
                "end_date": _yyyymmdd(end),
            },
            fields=fields,
        )

    def daily_basic_by_date(self, day: date) -> list[dict]:
        """按交易日取全市场每日指标(含总股本/流通股本, 单位万股)。单次约 5550 行。"""
        out: list[dict] = []
        for page in self.query_paged(
            "daily_basic", {"trade_date": _yyyymmdd(day)}, fields=_DAILY_BASIC_FIELDS
        ):
            out.extend(page)
        return out

    def probe(self) -> list[dict]:
        """轻量探测: 取 1 条上市股票基本信息, 用于"先探后存"校验 Token。"""
        return self.query(
            "stock_basic", {"list_status": "L", "limit": 1, "offset": 0}, fields="ts_code"
        )
