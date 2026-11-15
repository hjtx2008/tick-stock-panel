# 缠论分析接入规格（设计 + 实现口径）

> 状态：**M0 算法内核已实现**（`backend/app/custom/chan/core.py` + `backend/tests/test_chan_core.py`，45 条单测全绿）。
> 本文定义算法口径、模块落点、接口契约与测试清单，供实现与验收使用。
> 编写时间：2026-11-13 · 起点 commit：`53fbef2`
> 决策进度：**D1 / D2 / D3 / D5 / D6 已由使用者拍板**（2026-11-13），见 §11。
> 下一步：**M1 结构分析页**（后端 API + 前端 K 线叠加）→ ⏸ **停点**交使用者肉眼验收结构。
> ⚠️ §4.4 / §4.5 / §4.7 的**实现口径与早期文字有出入的，以本文「M0 实现口径」小节为准**。
>
> 标记约定：
> - ✅ **已核实** —— 引用的接口在当前仓库真实存在，附文件路径与行号
> - ⬜ **待确认** —— 缠论流派差异点，**必须由使用者拍板**，本文给出「默认实现」但不得据此验收
> - 🚧 **待实现** —— 本文定义、本仓库尚无对应代码

> ⚠️ 动手前必读：[`../CONTRIBUTING.md`](../CONTRIBUTING.md)、
> [`secondary-development.md`](secondary-development.md)、
> [`功能扩展开发必读.md`](功能扩展开发必读.md)。

---

## 0. 现状盘点（实测）

| 项 | 事实 | 依据 |
|---|---|---|
| 缠论相关代码 | **零**。全仓 `grep 缠论\|分型\|中枢\|线段\|背驰\|chanlun\|czsc` 只命中 `项目回顾.md` 两处设想 | — |
| 第三方缠论库 | **无**。`backend/pyproject.toml` 无 `chanlun`/`czsc`/`pandas-ta` | — |
| 日 K 数据 | **约 270 个交易日**（2025-09-08 ~ 2026-10-23） | `data/kline_daily/` |
| 分钟 K 数据 | **仅 5 个分区 / 2 只标的 / 482 行** | `data/kline_minute/` |
| Tick / 逐笔 | **无此数据集**。provider 能力集为 `daily/adj_factor/realtime/minute/depth5/financial/full_minute`；`data/depth5/` 为空 | `backend/app/data_providers/capabilities.py` |
| 价格双口径 | enriched 同时保留**前复权** `open/high/low/close` 与**不复权** `raw_high/raw_low/raw_close` | `backend/app/indicators/pipeline.py:94-103` `ENRICHED_STORAGE_COLS` |
| 复权因子 | `data/adj_factor/all.parquet`（ETF 独立 `adj_factor_etf/`），schema `symbol, trade_date, ex_factor`，**事件型非累积**（每次除权的 pre/post 比值） | 实测 |
| 前复权会漂移 | `_apply_adj_factor` 的基准 `total_factor` 取因子表**最后一行** → 新增除权事件会让**历史前复权价整体漂移** | `pipeline.py:276-281` |
| 回测方向 | **纯多头**。`SignalMatrix` 只有 entry/exit 布尔位，无 short 状态 | `backend/app/backtest/matrix.py:501` |

### 0.1 由此确定的能力边界

| 缠论形态 | 可行性 | 结论 |
|---|---|---|
| 日线级 分型/笔/线段/中枢/背驰/买卖点 | ✅ | **本规格的目标形态** |
| 分钟级 | ⚠️ | 数据仅 5 天，仅能作原型验证，不作为交付目标 |
| Tick 级 | ❌ | 无逐笔数据集，**不做**（需先扩 provider 能力，超出本项目范围） |
| 缠论卖空 / 多空 | ❌ | 现有回测引擎 long-only，**不做**。语义定为「买点做多、卖点平仓」 |

---

## 1. 目标与非目标

### 1.1 目标

1. 提供一个**纯函数**的缠论结构计算内核：包含处理 → 分型 → 笔 → 线段 → 中枢 → 走势类型 → 背驰 → 买卖点。
2. 每个结构都带**确认时间**（`confirmed_at`），保证回测**无未来函数**。
3. 对外暴露一个只读 API + 一个可视化页面，**在 K 线上叠加笔 / 线段 / 中枢 / 买卖点**。
4. 可作为**策略**接入选股与回测，买卖点级别可归因。

### 1.2 非目标（明确不做）

- 不做 Tick 级、不做分钟级正式交付、不做多空。
- 不做「AI 自动生成缠论策略」——策略由人手写（`filter_history`）或由缠论内核供数。
- **不引入第三方缠论库**：口径不可控、无法逐条对齐、复现性差。核心算法自实现。
- 不在 M0~M3 阶段改 `backend/app/indicators/pipeline.py`（落列见 M4，见 §10）。

---

## 2. 数据口径（**必须先定，否则算法无意义**）

| 口径项 | 取值 | 说明 |
|---|---|---|
| **价格口径** `price_mode` | `qfq`（默认）/ `raw` / `hfq` | ✅ 已定，见 §2.1；配 `adj_anchor_date` |
| **级别** `level` | 仅 `1d`（日线）；**预留次级别递归接口** | ✅ 已定，见 §4.8；分钟数据不足，M0/M1 不实现分钟计算 |
| **样本窗口** | 至少 **250 根**日 K 才有意义的线段/中枢 | 本地日 K 有 270 个交易日，**刚好够**，新数据同步后更充裕 |
| **数据源** | `repo.get_daily_asset(asset_type, symbol, start, end)` | ✅ `backend/app/api/stock_analysis.py:125` 同款取法 |
| **资产类型** | 股票 / ETF / 指数 分流 | ✅ 同上，`repo.resolve_asset_type(symbol)` |

### 2.1 价格口径（✅ **已由使用者拍板：D2**）

**决策**：采用「**存原始价 + 复权因子序列**」，库内数据**永远稳定**；
**复权口径是暴露的参数**，由调用方选择。

#### 2.1.1 双层结构

```
【存储层 · 永久稳定 · 只读】
  原始价   data/kline_daily_enriched: raw_high / raw_low / raw_close
  复权因子 data/adj_factor/all.parquet : symbol, trade_date, ex_factor  (事件型, 非累积)
           ETF 独立: data/adj_factor_etf/
                          │
                          ▼
【计算层 · 口径参数化 · 本项目新增】
  price_mode + adj_anchor_date  →  实际用于结构计算的价格序列
```

> ✅ 存储层**已经满足**该决策，无需改动任何 Parquet schema、无需重算历史。
> 本项目要做的只是：**不再依赖 enriched 里的前复权列**（它会漂移），
> 而是从 `raw_*` + `adj_factor` **自己算**，口径由参数决定。

#### 2.1.2 复权公式

沿用仓库 `_apply_adj_factor`（✅ `pipeline.py:242`）的事件型因子语义，
把「基准 = 因子表最后一行」换成「**基准 = 指定锚点日**」：

设 `cum(D)` = 该标的截至日期 D（含）所有除权事件 `ex_factor` 的累积积；
设 `F` = 锚点日 A 的 `cum(A)`。

| `price_mode` | 公式 | 语义 | 稳定性 |
|---|---|---|---|
| `raw` | `p(D) = raw(D)` | 不复权 | ✅ 完全稳定 |
| **`qfq`（默认）** | `p(D) = raw(D) × cum(D) / F` | 前复权，**锚点日价格保持不变** | ✅ 稳定（见下） |
| `hfq` | `p(D) = raw(D) × cum(D)` | 后复权，最早价格保持不变 | ✅ 稳定 |

**为什么 qfq 也稳定**：对任意 `D ≤ A`，`cum(D)` 与 `cum(A)` 都只由「D/A 之前已发生的事件」决定，
后续新增的除权事件（trade_date > A）**不会改变**它们 → 历史价位不动。
这是与仓库现有前复权（基准随最新事件漂移）的**唯一区别**，也是可复现性的来源。

#### 2.1.3 默认参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `price_mode` | `raw` / `qfq` / `hfq` | **`qfq`** | 见下方理由 |
| `adj_anchor_date` | `YYYY-MM-DD` 或 `null` | `null` → 取**样本内最后交易日** | 缺省时必须在响应里**回显实际锚点**，便于复现 |

**默认选 `qfq` 的理由**：
1. **消除除权假缺口** —— 缠论的笔/线段对缺口敏感，除权跳空会凭空打断结构；
2. **锚点固定即稳定** —— 满足「库里数据永远稳定」的要求，且不牺牲连续性；
3. 与仓库其余部分（前复权 enriched、回测成交价）口径一致，避免同一页面上两套价位。

**同时保留 `raw` 的理由**：缠论的**缺口分析**要用真实缺口；且挂单/止损价位必须落在真实价上。
因此 API 响应里**同时回传 `raw_close` 序列**与 `price_mode`，由前端决定画哪套。

**除权日标记**：内核用 `ex_factor` 事件表标出 `ex_rights`（除权日），
用于区分「真实缺口」与「除权缺口」，供后续缺口分析使用。

### 2.2 确认滞后（**无未来函数的核心机制**）

缠论的每一级结构都存在确认滞后：分型要等右侧 K 线出现、笔要等终点分型成立、线段要等破坏、中枢要等三笔重叠。

**硬规则：任何结构都不得使用其 `confirmed_at` 之后的 K 线信息。**

实现方式：每个结构都带 `confirmed_index` / `confirmed_date`（= 该结构被确认时所依赖的最后一根 K 线）。回测与选股只允许使用 `confirmed_date <= 目标日` 的结构。

> 📌 本仓库现有 `compute_indicators()` 的所有列都带 `.over("symbol")` 但**没有滞后保护**，
> 缠论列**必须自带 `shift`/滞后对齐**，不能依赖引擎兜底。

---

## 3. 模块落点与分级

| 组件 | 路径 | 分级 | 触碰热点 |
|---|---|---|---|
| 缠论内核 | `backend/app/custom/chan/core.py` | L2（新增模块） | 无 |
| HTTP 端点 | `backend/app/custom/chan/api.py` → 由 `backend/app/custom/chan.py` 的 `setup()` 注册 | L2 | 无（✅ `registrar.include_router`，`backend/app/extensions/registry.py:37`） |
| 前端类型 + 请求 | `frontend/src/lib/api.ts` | L2 | ⚠️ 热点，但**契约变更必须同步**（`CONTRIBUTING §2.3`） |
| 前端查询键 | `frontend/src/lib/queryKeys.ts` | L2 | ⚠️ 同上 |
| K 线叠加层 | `frontend/src/components/EChartsCandlestick.tsx` | L2 | ⚠️ 热点（加 prop，不改既有行为） |
| 缠论页面 | `frontend/src/custom/chan/ChanPage.tsx` + `extension.tsx` | L2 | 无（✅ 路由+导航注册，零核心改动） |
| 策略（选股） | `data/strategies/custom/chan_*.py` | L1 | 无 |
| 策略（回测） | `backend/app/strategy/builtin/chan_*.py` | L2 | `strategy/engine.py` 不改 |

### 3.1 为什么后端放 `backend/app/custom/` 而不是 `backend/app/api/chan.py`

✅ `backend/app/extensions/loader.py:38-58` 自动发现 `app/custom/*.py`（下划线开头的不加载），
`setup(registrar)` 里 `registrar.include_router(router)` 即可注册路由，**无需修改 `backend/app/main.py`**（`secondary-development.md` §5.2 热点清单）。

### 3.2 为什么前端类型必须动 `lib/api.ts`

✅ `frontend/src/lib/api.ts` 里的 `request<T>()` **是模块私有、未导出**（`api.ts:32`），
前端扩展拿不到它。因此新增 `api.chanStructure()` 是**唯一不重复实现第二套请求逻辑**的做法
（`secondary-development.md` §3.4 禁止自建平行客户端）。这属于契约同步，**允许**，
但需在 PR 描述中标注为热点改动。

> 备选（不推荐）：在 `frontend/src/custom/chan/` 内自带 fetch 封装 —— 复制了超时/401/toast 逻辑，违反「不平行实现第二套逻辑」。

---

## 4. 算法规格

### 4.0 总体流水线

```
原始日K (symbol, date, high, low, close, volume)
   │  price_mode=raw → 用 raw_high/raw_low/raw_close
   ▼
① 包含关系处理 merge_klines        → MergedBar[]（保留 → 原始索引的映射）
   ▼
② 分型识别     find_fractals       → Fractal[]（top/bottom）
   ▼
③ 成笔         build_strokes       → Stroke[]（up/down）
   ▼
④ 线段         build_segments      → Segment[]
   ▼
⑤ 中枢         build_pivots        → Pivot[]（zg/zd/level）
   ▼
⑥ 走势类型     classify_trend      → trend / consolidation
   ▼
⑦ 背驰与买卖点 build_signals       → Signal[]（b1/b2/b3/s1/s2/s3 + 中文 label）
```

**每一级都是纯函数**，输入上一个级的输出，便于单测与逐步验证。

> ⚠️ **M1.1 澄清（2026-11-14）**：级次顺序与原始描述一致，但**中枢目前由「笔」构造**
> （笔中枢，`build_pivots(strokes, params)`），**不是**由线段构造。
> `build_segments` 的产物目前**只用于页面展示**——`build_signals` 虽接收 `segments` 参数，
> 但函数体内**并未使用**它。因此当前实现是「合并→分型→笔→（线段：仅展示）→中枢」的
> **单向叠层**，线段不参与中枢与买卖点计算。参数 `overlap_mode='segment'` 尚不支持
> （传值直接报错，fail-closed），见 §4.5。

---

### 4.1 ① 包含关系处理 `merge_klines`

**定义**：相邻两根 K 线 A（前）、B（后），若 A 完全包含 B 或 B 完全包含 A，即为包含关系。

```
包含: (A.high >= B.high and A.low <= B.low) or (A.high <= B.high and A.low >= B.low)
```

处理规则（方向 `dir` 由**前面最近的非包含关系**决定，序列起始默认为向上）：

| 方向 | 处理后 high | 处理后 low |
|---|---|---|
| 向上包含 `up` | `max(A.high, B.high)` | `max(A.low, B.low)` |
| 向下包含 `down` | `min(A.high, B.high)` | `min(A.low, B.low)` |

> 每根处理后 K 线必须保留它覆盖的**原始 K 线索引区间** `[start_index, end_index]`，
> 后续所有结构都要能映射回**真实日期与真实价格**（否则图上画不出线段端点）。

**参数**：`include_mode` = `strict`（默认，按缠论原文）/ `none`（不处理，仅调试用）

**边界**：
- 序列不足 2 根 → 原样返回。
- 方向初始值：默认 `up`（实现细节，无需拍板；如需调整改一行）。

---

### 4.2 ② 分型识别 `find_fractals`

在**处理后** K 线序列上，连续三根 `k[i-1], k[i], k[i+1]`：

```
顶分型: k[i].high > k[i-1].high and k[i].high > k[i+1].high
    and k[i].low  > k[i-1].low  and k[i].low  > k[i+1].low
底分型: k[i].low  < k[i-1].low  and k[i].low  < k[i+1].low
    and k[i].high < k[i-1].high and k[i].high < k[i+1].high
```

**确认滞后**：`k[i]` 的分型要到 `k[i+1]` 出现才成立 →
`confirmed_index = i + 1`，`confirmed_date` = 第 `i+1` 根 K 线的日期。

**映射**：分型的 `date` 取 `k[i]` 覆盖的原始区间**最后一根**原始 K 线的日期
（缠论用 K 线的极值时刻；实际取区间末端以保证 `date` 单调不减）。

**边界**：
- 序列首尾不足 3 根 → 不成型。
- 顶分型需「其后有下跌」才成立——**本实现不做此过滤**（默认行为，实现细节），
  因为该过滤会引入额外滞后且流派分歧大；成笔阶段自然淘汰无效分型。
  若使用者要求严格版此过滤，作为 `require_follow_through: bool` 参数暴露（默认 `False`）。

---

### 4.3 ③ 成笔 `build_strokes`

**✅ 已定（使用者决策）**：实现**严格原文版**，**成笔条件做成参数暴露**。
严格原文版包含两个变体，默认值见下表，调用方可覆盖。

| 版本 | 规则 |
|---|---|
| **新笔**（2020 年修订后，默认） | 相邻顶/底分型之间**至少有 1 根不属于任何分型的独立 K 线**，且两端分型**不共用 K 线** |
| 老笔 | 相邻顶/底分型之间至少 **5 根处理后 K 线** |

**参数（暴露给调用方）**：

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `stroke_version` | `new` / `old` | `new` | 笔的定义版本 |
| `min_gap_bars` | int | 1 | 两端分型之间最少独立 K 线数（新笔口径） |
| `min_k_count` | int | 5 | 两端分型之间最少处理后 K 线数（老笔口径） |
| `strict_no_shared_kline` | bool | `True` | 两端分型是否禁止共用 K 线 |

**输出** `Stroke`：`direction`(up/down)、`start`/`end`（分型）、`k_count`、`confirmed_date`。

**实现要点**：
- 按 K 线顺序扫描分型序列，维护「上一个有效分型」与「笔的方向」。
- 同类分型（连续两个顶）→ 保留**更高**的那个（顶）或**更低**的那个（底），
  并把 `confirmed_date` 更新为更晚的那个（**不得回退确认时间**）。

**边界**：
- 分型序列不足 2 个 → 无笔。
- 首尾未完成笔（只有起点没有终点）→ **不输出**（避免把未确认结构当成信号）。

---

### 4.4 ④ 线段 `build_segments`

**这是缠论实现中最难、最有流派差异的一环。⬜ 详细规则待确认。**

#### M0 实现口径（特征序列法，已落代码）

1. 线段至少由 **`min_strokes_per_segment`（默认 3）笔**构成。
2. **特征序列元素** = 与线段方向**相反**的笔：
   - 上升线段 → 元素是向下笔；元素的高点 = 该笔起点价（顶），低点 = 该笔终点价（底）；
   - 下降线段 → 元素是向上笔；元素的高点 = 该笔终点价（顶），低点 = 该笔起点价（底）。
3. **特征序列分型**（线段被破坏的第一种情况）：
   - 上升线段的特征序列出现**顶分型**（元素高点最高）→ 候选线段终点；
   - 下降线段的特征序列出现**底分型**（元素低点最低）→ 候选线段终点。
4. **确认（第一元素被破坏）**：候选分型**之后**的那一笔必须突破特征序列分型
   **第一元素**的极值（上升线段：后续向上笔的终点 > 第一元素高点；
   下降线段：后续向下笔的终点 < 第一元素低点）→ 线段成立。未突破则该分型不成立，继续向后找。
5. **线段终点** = 特征序列分型元素的极值点：
   上升线段 → 向下笔的**起点**（顶）；下降线段 → 向上笔的**起点**（底）。
6. 下一线段从当前线段的**终点那一笔**开始（两段共享端点），方向必然相反。
7. **第二种破坏情况（特征序列缺口）→ 未实现**（D4 待确认）。

#### 参数

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_strokes_per_segment` | int | 3 | 每段最少笔数 |
| `feature_fractal_mode` | `strict` / `extreme` | `extreme` | 特征序列分型的判定口径：`strict` = 高点与低点**同时**最极端（严格原文版）；`extreme` = 只比较与线段同向的极值（实务通行版）。**D13 待确认** |
| `feature_gap_break` | bool | `False` ⬜ | 第二种破坏情况，**M0 未实现**（传入不生效） |

> ⚠️ 实测差异：在真实日线数据上，`strict` 常常**一个线段都出不来**（特征序列里
> 高低点不同时极端），`extreme` 才能切出可用的线段。默认值选 `extreme`，
> 但这是**待拍板**项（§9 金标准样本到位后按样本重判）。

**确认滞后**：线段破坏被确认的那一笔的 `confirmed_date` = 该线段的 `confirmed_date`。

**边界**：
- 笔数不足 → 不成段。
- 末段未被破坏 → **不输出**（同 §4.3，未确认结构不外泄）。

---

### 4.5 ⑤ 中枢 `build_pivots`

**定义**：连续三笔的重叠部分构成中枢。

```
ZG = min(三笔各自的高点)
ZD = max(三笔各自的低点)
中枢成立条件: ZG > ZD
```

- **中枢延伸**：后续笔与中枢区间仍有重叠 → `end` 延后，`end_index` 推进。
- **中枢新生**：出现与当前中枢**完全不重叠**的笔 → 当前中枢结束，新中枢候选开始。

#### M0 实现口径（两处与早期文字不同，务必按此）

1. **中枢方向 = 进入中枢的那一段的方向**（= 中枢第一笔的**前一笔**的方向；
   中枢从第 0 笔开始时退化为第一笔方向）。
   ⚠️ 早期文字写的是「中枢第一笔的方向」——那样做的话相邻中枢的方向必然
   上下交替，**趋势将永远无法成立**（实测）。这是 M0 修正的关键一点。
2. **离开中枢的那一笔的处理**：若离开笔与中枢**同向** → 它属于中枢之间的走势，
   下一个中枢从再下一笔开始扫；若离开笔与中枢**反向** → 它是下一个中枢的
   「进入段」，计入下一个中枢。

**参数（暴露给调用方，✅ 使用者要求「中枢构造做成参数暴露」）**：

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_strokes_per_pivot` | int | 3 | 构造中枢所需最少笔数（严格原文 = 3） |
| `strict_overlap` | bool | `True` | 是否要求严格重叠（`ZG > ZD`；严格原文为真） |
| `overlap_mode` | `three_strokes` | `three_strokes` | 严格原文按**笔**重叠构造。⚠️ M0 **只实现这一种**，传 `segment` 会直接报错（fail-closed） |
| `pivot_level_mode` | `flat` / `extended` | `flat` | `flat` = 中枢区间在延伸中**不扩张**；`extended` = 延伸时同步扩张 ZG/ZD。⚠️ 早期文字写的 `nested`（递归嵌套）**未实现** |

**输出** `Pivot`：`direction`、`zg`、`zd`、`start_date`、`end_date`、`start_index`、
`end_index`、`stroke_count`、`level`、`nested_level`。

> ⚠️ **中枢级别**（1 分钟/5 分钟/30 分钟/日/周 的递归嵌套）在单级别日线口径下**不实现**
> —— 本仓库无足够分钟数据。`nested_level` 字段先固定为 `0`（日线级别），为将来留位。

#### 段级中枢 `build_segment_pivots`（✅ 第 5 步已实现）

**定义**：用**线段**而非笔构造的中枢（缠论口径：中枢由三个次级别走势类型重叠构成）。

构造逻辑与笔级中枢**完全同构**（同一套 `_build_pivots_from_ranges` 内核，
只换输入单元）：连续 `min_segments_per_pivot` 段价格区间的重叠部分，
`ZG = min(各段高点)`、`ZD = max(各段低点)`，`ZG > ZD` 才成立；延伸 / 离开笔的处理
与笔级一致。

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_segments_per_pivot` | int | 3 | 段中枢所需连续段数（< 3 直接 `ValueError`） |

**输出** `Pivot`（`nested_level = 1`）。⚠️ **坐标系与笔级中枢不同**：
段中枢的 `start_index` / `end_index` 是**线段下标**（不是笔下标）。

**存放位置**：与笔级中枢**分开存放**（`ChanStructure.pivots` vs
`ChanStructure.segment_pivots`），不混在一个列表里 —— 索引坐标系不同，混用会算错。

**用途**：段级结构展示 + 段背驰的形态归属判定（§4.7）。**不参与**笔级买卖点判定。

---

### 4.6 ⑥ 走势类型 `classify_trend`

| 类型 | 判定 |
|---|---|
| **趋势** | 存在**至少两个依次不重叠的同向中枢** |
| **盘整** | 只有一个中枢（或中枢重叠） |

#### M0 实现口径

- 下跌趋势：**最后两个** `down` 中枢依次不重叠（`ZG_末 < ZD_前`）；
- 上涨趋势：**最后两个** `up` 中枢依次不重叠（`ZD_末 > ZG_前`）；
- 其余 → `oscillation`（盘整）。

方向优先级：先看**最近一个**中枢的方向；该方向不成立时再看另一方向。

> ⚠️ **M1.1 修正（2026-11-14）**：M0 原实现取的是**前两个**同向中枢。
> 判据要描述**当前**结构，若取最前两个，一只先涨后跌的票会永远保留开头的上涨结构，
> 导致买点被误杀、卖点被放宽（实测 400 只样本：一买 37→46，一卖 28→18）。
> 现统一改为「**最后两个**同向中枢」（`same_direction_trend()`），
> `build_signals` 的一买/一卖门控与 `classify_trend` 共用同一判据。

⬜ **D11 待确认**：中枢「不重叠」的严格定义（本文按「区间完全分离」实现）。

---

### 4.7 ⑦ 背驰与买卖点 `build_signals`

#### 背驰（力度比较）—— ✅ **已由使用者拍板：D6**

**不使用 MACD 面积等指标力度，改用纯价格几何的「笔力度 / 笔背离」判定。**

```
笔幅度 := |笔终点价 - 笔起点价|              (绝对价格差)
笔天数 := 笔跨越的交易日数 (K 线根数)
笔力度 := 笔幅度 / 笔天数 * 100

笔背离 := BIOK
          AND 笔天数 >= 前笔天数
          AND ( 笔幅度 < 前笔幅度  OR  笔力度 < 前笔力度 )
```

**术语口径**：

| 术语 | 定义 | 备注 |
|---|---|---|
| `前笔` | 与**当前笔同方向**的上一笔（背驰比较的是同向笔） | 笔序列严格上下交替，因此「前一笔同向笔」= 隔一笔 |
| `BIOK` | 该笔**成立**（通过了成笔条件、终点分型已确认） | ⚠️ **M0 口径**：按缠论「BIOK = 笔成立」理解；D9 若澄清为别的含义，只需改 `detect_bi_divergence` 里这一处判定 |
| `笔天数` | 交易日数（K 线根数），非自然日 | ⬜ **D10 待确认** |
| `笔幅度` | 绝对价格差（不是百分比） | 同标的跨期比较有意义；跨标的不可比 |

**背驰方向**：底方向（向下笔）的背离 → **一买**；顶方向（向上笔）的背离 → **一卖**。

**与标准缠论「趋势背驰 / 盘整背驰」分类的关系**：使用者给的是**统一判定**，
不区分趋势与盘整 → 实现上**不依赖走势分类**，一笔背离即成立。
走势分类（§4.6）仍保留，供分析页展示，但不参与背驰判定。

> ✅ 好处：内核**不再需要 MACD / 成交量等指标列** → 与指标流水线零耦合，
> 也不需要 `REQUIRED_FEATURES` 声明指标依赖。

#### 盘整背驰的处置口径（✅ 第 4 步补充决策：D6-补充）

**背驰判定本身不区分趋势 / 盘整**（一笔背离即成立，如上），但**盘整背驰
只作结构标记，不产生买卖点**。

理由：买卖点是**趋势语境下的结论** —— 一买的定义是「下跌趋势末端的背驰」。
盘整里的一笔力度衰减**没有方向含义**（盘整的上下沿反复被试探，谁先离开
中枢并不由这一笔的力度决定），把它当成买卖点会给出方向相反的假信号。

**实现**：

| 环节 | 行为 |
|---|---|
| `detect_bi_divergence` | **照常**判定，产出全部候选（含 `diverged` 标志） |
| `classify_divergence_scope` | 给每个候选标注 `scope`：`trend` / `consolidation`（判据同 §4.6，取**最后两个同向中枢**是否依次不重叠） |
| `build_signals` | **只消费 `scope == "trend"` 的背驰** → 盘整背驰不出买卖点 |
| API / 前端 | 盘整背驰**照常吐出并画出来**（灰色菱形「盘背」），供人工核对与选股器使用 |

**形态归属判据**（与 §4.6 同构）：取**最后两个同向中枢**，下跌方向要求
`ZG_末 < ZD_前`、上涨方向要求 `ZD_末 > ZG_前` → 判为 `trend`；否则 `consolidation`。

#### 段背驰 `detect_segment_divergence`（✅ 第 5 步已实现）

段背驰与笔背驰**判定式完全同构**（D6 拍板口径），只把比较单元从「笔」换成「段」：

```
段幅度 := |段终点价 - 段起点价|
段天数 := 段跨越的交易日数 (K 线根数)
段力度 := 段幅度 / 段天数 * 100

段背离 := 段成立
          AND 段天数 >= 前段天数
          AND ( 段幅度 < 前段幅度  OR  段力度 < 前段力度 )

前段 := 上一段**同方向**的线段 (段序列严格上下交替 → 隔一段)
```

- **未确认的末段（`confirmed=False`）不参与**段背驰判定 —— 它仍在延伸，
  幅度 / 天数都可能继续变化，否则每次刷新都会抖出新的段背驰。
- **形态归属** `classify_segment_divergence_scope`：段级中枢
  （`nested_level=1`）的 `start_index/end_index` 是**线段下标**，与
  `segment_index` 同一坐标系 → 可直接比较，判据同笔级。
- **段背驰同样只作结构标记**（不产生买卖点）—— 买卖点体系目前是笔级的。


**参数**：

| 名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `divergence_mode` | `bi_force` | `bi_force` | 力度比较方式；使用者指定，暂只实现这一种 |
| `strength_scale` | int | 100 | 笔力度的放大系数（公式中的 `* 100`） |

#### 买卖点（M0 实现口径）

| 名称 | M0 判定 | 状态 |
|---|---|---|
| **一买** | 存在**下跌趋势**（两个依次不重叠的向下中枢）且**最后一个向下中枢之后**的向下笔出现笔背离 → 该笔终点为一买 | ✅ 已实现 |
| **一卖** | 上涨趋势中，最后一个向上中枢之后的向上笔出现笔背离 → 该笔终点为一卖 | ✅ 已实现 |
| **二买** | 一买之后的**回抽笔**（隔一笔）不跌破一买低点（回抽笔自身是否背驰只作「加强」标注，**非必要条件**） | ✅ 已实现（**笔级近似次级别**，页面须标注为近似，D7 待确认） |
| **三买** | 中枢之后**向上突破 `ZG`** 的那一笔之后的第一根向下笔（回抽）终点仍 > `ZG` | ✅ 已实现 |
| **二卖 / 三卖** | 与买点镜像 | ✅ 已实现 |

> ⚠️ 与早期文字的差异：M0 的**一买要求存在下跌趋势**（两个同向不重叠中枢），
> 而不是「任何一笔背离即成立」—— 一买的定义本身就是「趋势末端的背驰」。
> 若要放宽为「盘整背驰也出信号」，改 `build_signals` 里 `down_trend` / `up_trend` 的门控即可。
> 三买/三卖按 (类型, 日期) 去重（不同中枢可能指向同一次突破与回抽）。

> ⚠️ **M1.1 修正（2026-11-14）**：
> 1. 一买/一卖的「下跌/上涨趋势」门控由「**前两个**同向中枢」改为「**最后两个**同向中枢」
>    （与 §4.6 同步），见 §4.6 说明。
> 2. **二买/二卖不再要求回抽笔本身背驰**。缠论原文的「二买」= 一买之后的第一次回调
>    **不破一买低点**；回抽笔是否背驰只是「加强」信号。旧实现把背驰当必要条件，
>    实测 400 只样本**二买恒为 0**、买点几乎绝迹。现以 `detail.pullback_diverged`
>    标记是否加强。

**输出** `Signal`：`kind`、`date`、`price`、`confirmed_date`、`detail`
（`detail` 内含背离比较的两笔索引、幅度、天数、力度，便于前端展示与人工核对）。

> ⚠️ 所有信号的 `confirmed_date` 必须 ≥ 结构的确认时间，**不得早于**。
> 回测取信号时用 `confirmed_date` 对齐成交日（T+1 开盘成交，✅ `MatcherConfig.entry_fill`）。

---

### 4.8 次级别递归接口（✅ **已由使用者拍板：D5-预留**）

**决策**：先做**日线级别**，但**预留日线内的次级别递归分析接口**。

#### 4.8.1 级别体系

| 级别 | 枚举值 | 日线内的次级别关系 | 数据现状 |
|---|---|---|---|
| 日线 | `1d` | —— | ✅ 270 个交易日 |
| 30 分钟 | `30m` | `1d` 的次级别 | ⚠️ `data/kline_minute` 仅 5 个分区 / 2 只标的 |
| 5 分钟 | `5m` | `30m` 的次级别 | ⚠️ 同上 |
| 1 分钟 | `1m` | `5m` 的次级别 | ⚠️ 同上 |

> M0/M1 **只实现 `1d`**。分钟级留接口，**fail-closed**：请求未实现的级别时
> 明确报错（`NotImplementedError` / HTTP 501），**不静默返回空结构**
> （铁律 #7：不能用字段齐备冒充"没有信号"）。

#### 4.8.2 接口预留

```python
class ChanLevel(str, Enum):
    DAY = "1d"
    M30 = "30m"
    M5 = "5m"
    M1 = "1m"


# 级别关系：sub_level_of("1d") == "30m"，sub_level_of("30m") == "5m" ...
def sub_level_of(level: ChanLevel) -> ChanLevel | None: ...


@dataclass(frozen=True)
class ChanParams:
    """所有口径参数的单一入口 (成笔条件 / 中枢构造 / 复权 / 级别 ...)。"""
    level: ChanLevel = ChanLevel.DAY
    price_mode: str = "qfq"
    adj_anchor_date: date | None = None
    stroke_version: str = "new"
    min_gap_bars: int = 1
    min_k_count: int = 5
    strict_no_shared_kline: bool = True
    min_strokes_per_pivot: int = 3
    strict_overlap: bool = True
    overlap_mode: str = "three_strokes"
    pivot_level_mode: str = "flat"
    divergence_mode: str = "bi_force"
    strength_scale: int = 100
    min_strokes_per_segment: int = 3
    feature_gap_break: bool = False


# 统一入口：级别只是参数，算法本身与级别无关
def analyze(bars: pl.DataFrame, params: ChanParams) -> ChanStructure: ...


# 次级别递归入口 (M0/M1 不实现, 但签名先定死)
def analyze_sublevel(
    daily: ChanStructure,
    sub_bars: pl.DataFrame,
    params: ChanParams,
) -> ChanStructure:
    """在日线结构的次级别上递归分析。

    - sub_bars 必须与 daily 的时间范围对齐 (按交易日映射)
    - 未实现的级别 → NotImplementedError (fail-closed)
    - 二买的判定 (次级别回调不破一买低点) 依赖此函数
    """
```

#### 4.8.3 结构对象的级别字段

所有结构（`Fractal` / `Stroke` / `Segment` / `Pivot` / `ChanSignal`）都带 `level: ChanLevel`，
为将来多级别共存留位。M0/M1 一律为 `ChanLevel.DAY`。

#### 4.8.4 与中枢嵌套的衔接

§4.5 的 `pivot_level_mode = "nested"`（日线中枢由次级别走势构成）**依赖本节接口**，
M0/M1 不实现，保持 `flat`。参数先暴露出来，避免将来改签名。

---

## 5. 数据结构

### 5.1 Python（`backend/app/custom/chan/core.py` —— ✅ 已实现，与代码一致）

```python
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

import polars as pl


class ChanLevel(StrEnum):
    """缠论级别。取值与前端 K 线周期标签保持一致。"""

    DAY = "1d"
    M30 = "30m"
    M5 = "5m"
    M1 = "1m"


def sub_level_of(level: ChanLevel) -> ChanLevel | None:
    """日线之下的次级别; 已是最低级别时返回 None。"""


@dataclass(frozen=True)
class ChanParams:
    """缠论计算参数 —— 所有流派分歧点都在这里暴露。"""

    level: ChanLevel = ChanLevel.DAY

    # 复权口径 (§2.1)
    price_mode: Literal["raw", "qfq", "hfq"] = "qfq"
    adj_anchor_date: date | None = None

    # 成笔条件 (§4.3)
    stroke_version: Literal["new", "old"] = "new"
    min_gap_bars: int = 1
    min_k_count: int = 5
    strict_no_shared_kline: bool = True

    # 中枢构造 (§4.5)
    min_strokes_per_pivot: int = 3
    strict_overlap: bool = True
    overlap_mode: Literal["three_strokes"] = "three_strokes"
    pivot_level_mode: Literal["flat", "extended"] = "flat"

    # 背驰 (§4.7)
    divergence_mode: Literal["bi_force"] = "bi_force"
    strength_scale: int = 100

    # 线段 (§4.4)
    min_strokes_per_segment: int = 3
    feature_fractal_mode: Literal["strict", "extreme"] = "extreme"

    # 分型细节 (§4.2)
    require_follow_through: bool = False

    def validate(self) -> None:
        """参数非法直接抛错, 不做静默兜底。"""


@dataclass(frozen=True)
class MergedBar:
    """包含处理后的合并 K 线。"""

    high: float
    low: float
    start_index: int          # 参与合并的首根原始 K 线索引
    end_index: int            # 参与合并的末根原始 K 线索引
    high_index: int           # 最高点所在的原始 K 线索引
    low_index: int            # 最低点所在的原始 K 线索引
    high_date: str
    low_date: str
    end_date: str             # 合并区间末根原始 K 线的日期 (= 分型的确认日)


@dataclass(frozen=True)
class Fractal:
    """分型。"""

    kind: Literal["top", "bottom"]
    price: float              # 顶分型取 high, 底分型取 low
    date: str
    bar_index: int            # 极值所在的原始 K 线索引
    merged_index: int         # 极值所在的合并 K 线索引
    start_index: int          # 分型三 K 组合的首根原始 K 线索引
    end_index: int            # 分型三 K 组合的末根原始 K 线索引
    confirmed_date: str       # 分型被确认的日期


@dataclass(frozen=True)
class Stroke:
    """笔。"""

    direction: Literal["up", "down"]
    start: Fractal
    end: Fractal
    k_count: int              # 笔跨越的 K 线根数 (= 笔天数, 交易日口径)
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY

    @property
    def amplitude(self) -> float:
        """笔幅度 = |终点价 - 起点价| (绝对价格差)。"""
        return abs(self.end.price - self.start.price)

    @property
    def strength(self) -> float:
        """笔力度 = 笔幅度 / 笔天数 * 100 (使用者定义的力度口径, 见 §4.7)。"""
        if self.k_count <= 0:
            return 0.0
        return self.amplitude / self.k_count * 100.0


@dataclass(frozen=True)
class Segment:
    """线段。"""

    direction: Literal["up", "down"]
    start_price: float
    end_price: float
    start_date: str
    end_date: str
    start_index: int           # 起始笔在 strokes 中的索引
    end_index: int             # 结束笔在 strokes 中的索引
    stroke_count: int
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY


@dataclass(frozen=True)
class Pivot:
    """中枢。"""

    direction: Literal["up", "down"]
    zg: float                  # 中枢上沿
    zd: float                  # 中枢下沿
    start_date: str
    end_date: str
    start_index: int           # 起始笔索引
    end_index: int             # 结束笔索引
    stroke_count: int
    level: ChanLevel = ChanLevel.DAY
    nested_level: int = 0      # 中枢递归级别 (单级别时恒为 0)


@dataclass(frozen=True)
class ChanSignal:
    """买卖点。"""

    kind: Literal["b1", "b2", "b3", "s1", "s2", "s3"]
    label: str                 # 一买 / 二买 / 三买 / 一卖 / 二卖 / 三卖
    date: str
    price: float
    bar_index: int
    confirmed_date: str
    level: ChanLevel = ChanLevel.DAY
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChanStructure:
    """一次分析的完整产物 (API 响应的唯一来源)。"""

    symbol: str
    price_mode: str
    adj_anchor_date: str | None   # 实际使用的锚点 (必须回显, 便于复现)
    level: ChanLevel
    dates: list[str]              # 与前端 K 线逐行对齐
    closes: list[float]           # 用于结构计算的价格序列 (复权口径后)
    raw_closes: list[float]       # 不复权收盘
    highs: list[float]
    lows: list[float]
    fractals: list[Fractal]
    strokes: list[Stroke]
    segments: list[Segment]
    pivots: list[Pivot]
    signals: list[ChanSignal]
    trend: str                    # "up" / "down" / "oscillation"
    status: str                   # "ok" / "insufficient_data"
    adj_factor_missing: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        ...


# ── 主入口 ──────────────────────────────────────────────
def analyze(
    bars: pl.DataFrame,
    params: ChanParams | None = None,
    factors: pl.DataFrame | None = None,
    symbol: str = "",
) -> ChanStructure:
    """bars 需含 date/high/low/close (视为原始价) 或 raw_high/raw_low/raw_close;
    factors 为该标的的复权因子表 (trade_date / ex_factor)。"""


def analyze_sublevel(daily_bars, sub_bars, params=None, factors=None, symbol="") -> ChanStructure:
    """日线内的次级别递归分析 (§4.8 预留) —— M0/M1 **不实现**, 直接抛 NotImplementedError。"""
```

> 全部 `frozen=True` 不可变，符合 `CONTRIBUTING §6.2`（共享缓存用不可变快照）。
> `ChanStructure` 是**唯一对外产物** —— API 序列化、策略消费、前端渲染都从它来，
> 避免出现第二套平行结构。

### 5.2 API 契约

```
GET /api/chan/structure
    ?symbol=000001.SZ
    &days=250              # 取样交易日数（30~500）
    &price_mode=qfq        # qfq（默认） | raw | hfq
    &adj_anchor_date=      # 可选, 空 = 样本内最后交易日 (响应会回显实际锚点)
    &level=1d              # 1d（默认, 目前唯一实现）; 30m/5m/1m → 501 未实现
    &include=structure,signals   # 可选，按需裁剪
```

响应：

```jsonc
{
  "symbol": "000001.SZ",
  "price_mode": "qfq",
  "adj_anchor_date": "2026-10-23",   // 实际锚点，必须回显
  "level": "1d",
  "as_of": "2026-10-23",
  "dates": ["2026-09-01", "..."],       // 原始 K 线日期，与前端 K 线一一对应
  "closes": [12.34, "..."],             // price_mode 口径下的收盘
  "raw_closes": [13.10, "..."],         // 不复权收盘 (画真实价位/缺口时用)
  "ex_rights": ["2026-09-24", "..."],   // 除权日 (区分真实缺口与除权缺口)
  "fractals": [
    { "kind": "top", "date": "2026-09-10", "price": 15.2,
      "confirmed_date": "2026-09-11" }
  ],
  "strokes": [
    { "direction": "up", "start_date": "2026-09-01", "end_date": "2026-09-10",
      "start_price": 12.0, "end_price": 15.2, "k_count": 7,
      "amplitude": 3.2, "strength": 45.71,
      "confirmed_date": "2026-09-11" }
  ],
  "segments": [
    { "direction": "up", "start_date": "...", "end_date": "...",
      "start_price": 10.0, "end_price": 15.2, "stroke_count": 5,
      "k_count": 9, "amplitude": 5.2, "strength": 57.78,
      "confirmed": true, "confirmed_date": "..." }
  ],
  "pivots": [
    { "zg": 14.8, "zd": 13.1, "start_date": "...", "end_date": "...",
      "nested_level": 0, "confirmed_date": "..." }
  ],
  "segment_pivots": [
    { "zg": 14.8, "zd": 13.1, "start_date": "...", "end_date": "...",
      "nested_level": 1, "confirmed_date": "..." }
  ],
  "signals": [
    { "kind": "b1", "label": "一买", "date": "2026-09-18", "price": 12.8,
      "confirmed_date": "2026-09-20",
      "detail": {
        "divergence": "bi_force",
        "cur_stroke_index": 12, "prev_stroke_index": 10,
        "cur_days": 9, "prev_days": 7,
        "cur_amplitude": 2.1, "prev_amplitude": 2.6,
        "cur_strength": 23.33, "prev_strength": 37.14
      } }
  ],
  "divergences": [
    { "stroke_index": 12, "prev_index": 10, "date": "2026-09-18",
      "price": 12.8, "direction": "down", "diverged": true,
      "scope": "trend", "detail": { "cur_days": 9, "prev_days": 7, "...": "..." } }
  ],
  "segment_divergences": [
    { "segment_index": 4, "prev_index": 2, "date": "2026-09-18",
      "price": 12.8, "direction": "down", "diverged": true,
      "scope": "trend", "detail": { "cur_days": 21, "prev_days": 18, "...": "..." } }
  ],
  "summary": {
    "status": "ok",
    "bar_count": 250, "fractal_count": 41, "stroke_count": 18,
    "segment_count": 5, "pivot_count": 3, "segment_pivot_count": 1,
    "signal_count": 2,
    "divergence_count": 5, "confirmed_divergence_count": 1,
    "segment_divergence_count": 2, "confirmed_segment_divergence_count": 1,
    "trend": "down"
  }
}
```

**选股接口（第 5 步新增）**：`POST /api/chan/screen`

请求体（条件之间是「与」关系，留空 = 该维度不限）：

```jsonc
{
  "days": 250,                  // 样本长度 (交易日)
  "symbols": null,              // 股票池; null = 全市场
  "limit": 200,
  "signal_kinds": ["b1", "b2"], // 命中的买卖点类型
  "trend": ["down"],            // up / down / oscillation
  "divergence_scope": ["trend"],// trend / consolidation
  "require_divergence": false,  // 必须有确认的笔背驰
  "require_segment_divergence": false, // 必须有确认的段背驰
  "require_segment_pivot": false,      // 必须有段中枢
  "max_signal_age": 0           // 买卖点距样本末日不超过 N 根 (0 = 不限)
}
```

响应：`{ matches: [...], scanned, total_matched, elapsed_ms, as_of }`，
`matches` 每项含 `symbol` / `name` / `trend` / `last_close`（**原始价**）/ `signals` /
`divergences` / `segment_divergences` / 各级计数。

⚠️ **口径一致性**：选股器的结构结论与 `/api/chan/structure` **逐项一致**
（前复权、锚点=样本末日）—— 由 `tests/test_chan_screener.py` 的
`test_scan_frame_matches_per_symbol_analyze` 锁定。

**约定**：
- `dates` / `closes` 必须与前端 K 线数据**逐行对齐**（同一数据源、**同一 `price_mode`**），
  否则叠加会错位 —— 这是本项目历史上踩过的坑（见 `功能扩展开发必读.md` 铁律 #4/#5 的同类问题）。
  ⚠️ 前端 K 线组件当前画的是**仓库默认前复权**（基准随最新事件漂移），
  若 `price_mode=qfq` 且锚点不是最新日，**两者会有微小价差** → 叠加允许 ±0.5% 视觉容差，
  或由前端在切换 `price_mode` 时同时切换 K 线数据源。**该细节在 M1 实现时必须实测对齐。**
- `adj_anchor_date` **必须回显**，否则结果不可复现。
- `summary` 的统计口径写清是**全样本**还是**近 N 日**（铁律 #9）。
- 数据不足（`bar_count < 30`）→ 返回 `summary.status = "insufficient_data"`，
  **不返回空数组冒充「没有信号」**（铁律 #7：不能用字段齐备判断数据拉全）。
- `level` 传入未实现的级别 → **HTTP 501 + 明确说明**，不返回空结构。

---

## 6. 前端契约

### 6.1 类型（加在 `frontend/src/lib/api.ts`）—— ✅ **M1 已落地，与代码一致**

```ts
export interface ChanFractal {
  kind: string            // 'top' | 'bottom'
  date: string
  price: number
  bar_index: number
  confirmed_date: string
}
export interface ChanStroke {
  direction: 'up' | 'down'
  start_date: string
  end_date: string
  start_price: number
  end_price: number
  k_count: number
  amplitude: number      // 笔幅度 = |终点价 - 起点价|
  strength: number       // 笔力度 = 幅度 / 天数 * 100
  confirmed_date: string
}
export interface ChanSegment {
  direction: 'up' | 'down'
  start_date: string
  end_date: string
  start_price: number
  end_price: number
  stroke_count: number
  k_count: number          // 段跨越的 K 线根数
  amplitude: number       // 段幅度 = |终点价 - 起点价|
  strength: number        // 段力度 = 幅度 / k_count * 100
  confirmed_date: string
  confirmed: boolean      // False = 末段未确认 (图上画虚线)
}
export interface ChanPivot {
  direction: 'up' | 'down'
  zg: number
  zd: number
  start_date: string
  end_date: string
  stroke_count: number
  nested_level: number    // 0 = 笔级中枢, 1 = 段级中枢
  confirmed_date: string
}
export interface ChanSignal {
  kind: string            // 'b1' | 'b2' | 'b3' | 's1' | 's2' | 's3'
  label: string           // 中文标签(一买/二买/三买/一卖/二卖/三卖)
  date: string
  price: number
  bar_index: number
  confirmed_date: string
  detail: Record<string, unknown>   // 后端返回 dict (背离比较明细 / 判定依据)
}
export interface ChanSummary {
  status: string            // 'ok' | 'insufficient_data'
  symbol: string
  price_mode: string        // 'qfq' | 'raw' | 'hfq'
  adj_anchor_date: string | null
  level: string
  trend: string             // 'up' | 'down' | 'oscillation'
  bar_count: number
  fractal_count: number
  stroke_count: number
  segment_count: number
  pivot_count: number
  signal_count: number
  divergence_count?: number
  confirmed_divergence_count?: number
  segment_pivot_count?: number       // 段级中枢数量
  segment_divergence_count?: number
  confirmed_segment_divergence_count?: number
  adj_factor_missing: boolean
  warnings: string[]
}
export interface ChanDivergence {
  stroke_index: number
  prev_index: number
  date: string
  price: number
  direction: 'up' | 'down'
  diverged: boolean
  scope: 'trend' | 'consolidation'   // 盘整背驰只作标记, 不产生买卖点
  detail: Record<string, unknown>
}
export interface ChanSegmentDivergence {   // 段级背驰 (segment_index = 线段下标)
  segment_index: number
  prev_index: number
  date: string
  price: number
  direction: 'up' | 'down'
  diverged: boolean
  scope: 'trend' | 'consolidation'
  detail: Record<string, unknown>
}
export interface ChanStructureResponse {
  symbol: string
  asset_type?: string
  price_mode: string
  adj_anchor_date: string | null    // 实际锚点 (必须回显, 便于复现)
  level: string
  as_of: string | null
  dates: string[]
  closes: number[]                  // price_mode 口径
  raw_closes: number[]              // 不复权
  summary: ChanSummary
  warnings: string[]
  fractals?: ChanFractal[]          // include=structure 时返回
  strokes?: ChanStroke[]
  segments?: ChanSegment[]
  pivots?: ChanPivot[]
  segment_pivots?: ChanPivot[]      // 段级中枢 (nested_level=1)
  signals?: ChanSignal[]            // include=signals 时返回
  divergences?: ChanDivergence[]            // include=divergences 时返回
  segment_divergences?: ChanSegmentDivergence[]
}
```

配套 `api.chanStructure(symbol, days, priceMode, adjAnchorDate?, include?)` 与
`api.chanScreen(body)`，走既有 `request<T>()`（选股接口放宽超时到 180s）。

> ⚠️ **与初稿的差异**：初稿设计了 `ChanPriceMode` / `ChanLevel` / `ChanSignalKind` 三个别名类型，
> 实现时**没有引入**——`price_mode` / `level` / `kind` 在前端只用于展示与分支判断，
> 直接用 `string` + 运行时兜底更省事；后端已做白名单校验（400），不会透出非法值。
> 初稿的 `ex_rights` 字段**未实现**（M1 用不到），后端 `AdjustedSeries.ex_rights` 暂未外泄。

### 6.2 查询键（加在 `frontend/src/lib/queryKeys.ts`）—— ✅ **M1 已落地**

```ts
// 缠论结构分析 (后端扩展 chan.analysis) — 日线级别, 不进 SSE 刷新
chanStructure: (symbol: string, days: number, priceMode: string, anchor?: string) =>
                   ['chan-structure', symbol, days, priceMode, anchor ?? ''] as const,
chanKline:     (symbol: string, days: number) => ['chan-kline', symbol, days] as const,
// 缠论选股 — 全市场扫描, 手动触发
chanScreen:    (payload: unknown) => ['chan-screen', payload] as const,
```

> ⚠️ **不要**加进 `SSE_INVALIDATE_PREFIXES`（日 K 类 key 刻意不带 `watchlist-` 前缀，
> 避免被 SSE 高频失效）。

### 6.3 K 线叠加（`frontend/src/components/EChartsCandlestick.tsx`）—— ✅ **M1 已落地**

新增三个类型 + 一个聚合 prop（与既有 `ChartMarker` / `ChartRange` / `ChartPriceLine` 并列）：

```ts
/** 缠论「笔 / 线段」: 两点连线 (用 markLine 斜线绘制) */
export interface ChartStroke {
  startDate: string
  startPrice: number
  endDate: string
  endPrice: number
  direction: 'up' | 'down'
}
/** 缠论「中枢」: 二维矩形 (用 markArea 绘制) */
export interface ChartPivot {
  startDate: string
  endDate: string
  zg: number      // 中枢上沿
  zd: number      // 中枢下沿
  direction: 'up' | 'down'
  nestedLevel?: number   // 0 = 笔级中枢 (紫), 1 = 段级中枢 (青)
}
/** 缠论「背驰」: 背驰标记点 */
export interface ChartDivergence {
  date: string
  price: number
  direction: 'up' | 'down'
  scope: 'trend' | 'consolidation'
  diverged: boolean
}
/** 缠论叠加层: 笔(细) / 线段(粗) / 中枢(矩形) / 分型(点) / 背驰(标记) */
export interface ChanChartOverlay {
  strokes?: ChartStroke[]
  segments?: ChartStroke[]
  pivots?: ChartPivot[]
  fractals?: ChartFractal[]
  divergences?: ChartDivergence[]
  segmentDivergences?: ChartDivergence[]   // 段背驰 (pin 形状 + 橙色)
}
// Props 新增: chanOverlay?: ChanChartOverlay
```

**渲染映射**（`buildOption` 内）：

| 缠论要素 | ECharts 载体 | 写法 |
|---|---|---|
| 笔 / 线段 | `markLine.data` | `[{ xAxis: d1, yAxis: p1 }, { xAxis: d2, yAxis: p2, lineStyle, label:{show:false} }]` —— 两点斜线，**ECharts 原生支持** |
| 中枢 | `markArea.data` | `[{ xAxis: start, yAxis: zd, itemStyle }, { xAxis: end, yAxis: zg }]` —— 二维矩形，**ECharts 原生支持** |
| 买卖点 | `markPoint`（复用 `ChartMarker`） | `kind: 'buy' \| 'sell'`，`label: '一买' / '三卖'` |

**约束与实现说明**：
- 现有 `markArea` 只给 `xAxis`（铺满全高）；中枢项**另起一个二维端点对**追加进同一个
  `markAreaData` 数组（ECharts 的 `markArea.data` 每项独立判定 1-D / 2-D，可混排）。
- 所有日期必须在 `dateIndexMap` 内，否则**直接跳过该条**（不画半截线）。
- `updateCompactPresentation()` 的紧凑模式增量更新只改 `markPoint`，
  `setOption` 按 series 深度合并，**不会清掉** `markLine` / `markArea` → 缩放后笔/中枢不会「跳出去」。
- 重绘后必须恢复 `dataZoom`（`userZoomRef` 逻辑），新图层走同一套路径（同一个 `buildOption`）。
- 配色：笔 = 涨红跌绿细线（`#C74040` / `#2D9B65`，宽 1），线段 = 同色粗线（宽 2.5），
  中枢 = 低透明度填充 `rgba(139,92,246,0.12)` + `rgba(139,92,246,0.5)` 边框（紫灰系，避开 MA 紫）。
- `chanOverlay` 必须在页面侧用 `useMemo` 固化引用，否则每次渲染都会触发**全量 setOption**（缩放被重置）。

### 6.4 页面（`frontend/src/custom/chan/`）

```
frontend/src/custom/chan/extension.tsx      # FrontendExtension: routes + navigation
frontend/src/custom/chan/ChanPage.tsx       # 结构分析页
frontend/src/custom/chan/ChanScreenPage.tsx # 选股页 (第 5 步)
```

```ts
const extension: FrontendExtension = {
  id: 'chan.analysis',
  apiVersion: 1,
  routes: [
    { id: 'chan-structure', path: '/chan', component: ChanPage },
    { id: 'chan-screen', path: '/chan/screen', component: ChanScreenPage },
  ],
  navigation: [
    { id: 'chan-structure', routeId: 'chan-structure', label: '缠论结构', icon: GitBranch },
    { id: 'chan-screen', routeId: 'chan-screen', label: '缠论选股', icon: Search },
  ],
}
```

> 选股页的命中行可点回 `/chan?symbol=xxx` → 结构页读取 `?symbol=` 预填标的。

> ✅ `frontend/src/extensions/types.ts` 约定：路由必须是**静态绝对路径**；
> `navigation.routeId` 必须引用**同一扩展内**已注册的路由。
> ✅ 扩展路由不与核心路径冲突（`/chan` 未被占用）。

**图表生命周期铁律**（`功能扩展开发必读.md` 铁律 #1/#2/#3）：
- 图表容器**常驻渲染**，loading/empty 用绝对定位覆盖层，**不要** `rows.length > 0 && <Chart/>`。
- 若自建图表组件，照抄 `frontend/src/components/MarketVolumeChart.tsx:254-279`
  （state 化 callback ref + init/setOption 合并同一 effect + `removeAttribute('_echarts_instance_')` + `isDisposed()` 防御 + rAF 兜底）。
- 若复用 `EChartsCandlestick`，注意它的 init 与 setOption 是**两个 effect**（✅ `:985` / `:1167`），
  **不要新增第三个依赖同一 chart 的 effect**。

**验收**：Playwright 走「进入 → 切走 → 切回 ×6」，每次 `document.querySelectorAll('canvas').length === 1`，且无 pageerror（铁律 #10）。

---

## 7. 性能预算

| 场景 | 目标 | 说明 |
|---|---|---|
| 单股 250 根日 K 全量计算 | **< 200 ms** | 纯 Python 逐股循环可接受；**不要**逐行 Polars 循环 |
| 批量 100 只 | < 5 s | |
| 全市场（5000+ 只）批量 | **不做** | 若需要，走策略引擎的 `parallel=True` 有界线程池 |

**实现约束**：
- 单股计算用**普通 Python 列表/循环**（缠论是状态机，Polars 表达力差）；
  但**数据读取必须用 Polars 批量**（✅ `CONTRIBUTING §6.3`：优先表达式和批处理）。
- 纯函数 → 天然可缓存；缓存键 = `(symbol, price_mode, bars_hash)`。

---

## 8. 单元测试清单（M0 验收）

新建 `backend/tests/test_chan_core.py`。**每个测试都必须断言业务结果，不能只断言"没抛异常"**（`CONTRIBUTING §9`）。

### 8.1 包含处理

| # | 用例 | 期望 |
|---|---|---|
| T1.1 | 向上包含：A(h=10,l=8), B(h=9,l=8.5) | 结果 h=10, l=8.5 |
| T1.2 | 向下包含：A(h=10,l=8), B(h=9.5,l=9) | 结果 h=9.5, l=9 |
| T1.3 | 无包含 | 原样保留 |
| T1.4 | 三连包含 | 逐根累积处理，结果仍满足「相邻无包含」 |
| T1.5 | 索引映射 | 每根 MergedBar 的 `end_index - start_index + 1` 之和 == 原始 K 线条数 |

### 8.2 分型

| # | 用例 | 期望 |
|---|---|---|
| T2.1 | 标准顶分型 | 识别为 top，`confirmed_index == i+1` |
| T2.2 | 标准底分型 | 识别为 bottom |
| T2.3 | 非分型（三根单调） | 不识别 |
| T2.4 | 首尾不足三根 | 不识别，不抛异常 |
| T2.5 | **确认滞后** | 分型 `confirmed_date` 严格晚于其 `date` |

### 8.3 笔

| # | 用例 | 期望 |
|---|---|---|
| T3.1 | 标准顶底交替 | 生成 up/down 交替的笔 |
| T3.2 | 连续两个顶分型 | 只保留更高的那个 |
| T3.3 | 连续两个底分型 | 只保留更低的那个 |
| T3.4 | 新笔版本：分型间无独立 K 线 | **不成笔** |
| T3.5 | 老笔版本：分型间 < 5 根 | **不成笔** |
| T3.6 | 端点未完成 | 末笔不输出 |
| T3.7 | 笔方向严格交替 | 不出现连续两笔同向 |

### 8.4 线段

| # | 用例 | 期望 |
|---|---|---|
| T4.1 | 少于 3 笔 | 不成段 |
| T4.2 | 特征序列顶分型 | 上升线段在该处结束 |
| T4.3 | 末段未破坏 | 不输出末段 |
| T4.4 | 线段方向交替 | — |

### 8.5 中枢

| # | 用例 | 期望 |
|---|---|---|
| T5.1 | 三笔重叠 | `zg = min(highs)`, `zd = max(lows)`，且 `zg > zd` |
| T5.2 | 三笔不重叠 | 不成中枢 |
| T5.3 | 延伸 | 后续笔仍重叠 → `end_date` 延后 |
| T5.4 | 新生 | 不重叠笔 → 中枢结束 |
| T5.5 | 边界 `zg == zd` | 不成中枢（严格重叠） |

### 8.6 走势 / 背驰 / 买卖点

| # | 用例 | 期望 |
|---|---|---|
| T6.1 | 两个同向不重叠中枢 | `trend == 'down'/'up'` |
| T6.2 | 单中枢 / 中枢重叠 | `trend == 'oscillation'` |
| T6.3 | **笔力度计算** | `strength == amplitude / k_count * 100`（逐笔断言数值） |
| T6.4 | **笔背离成立** | 构造 `BIOK=True`、`天数 >= 前笔天数`、`幅度 < 前笔幅度` → 产生一买 |
| T6.5 | **笔背离不成立（天数不足）** | `天数 < 前笔天数` → 不产生信号 |
| T6.6 | **笔背离不成立（力度不减）** | 天数达标但幅度与力度**都** >= 前笔 → 不产生信号 |
| T6.7 | **力度分支独立生效** | 幅度 >= 前笔但力度 < 前笔 → **仍产生**信号（验证 `OR` 分支） |
| T6.8 | 三买 | 回抽不破 `zg` → 产生 `b3` |
| T6.9 | 二买（M0/M1 用笔近似） | 一买后回调不破一买低点 → 产生 `b2`（**回抽笔自身背驰非必要条件**，仅作 `detail.pullback_diverged` 加强标记） |
| T6.10 | 走势判据取**最后两个**同向中枢 | 前两个不重叠、后两个重叠 → `oscillation`；前两个重叠、后两个不重叠 → 对应方向趋势 |

### 8.7 **无未来函数（最高优先级）**

| # | 用例 | 期望 |
|---|---|---|
| T7.1 | **截断测试（核心）**：对同一只票，取前 N 根算一次，再取前 N+M 根算一次 | 截断样本里**已确认**的结构（在长样本中同样成立、且终点距样本末 ≥ 2 根）在长样本中必须**逐字段一致**（含 `confirmed_date`）—— 若不一致，说明有未来函数 |
| T7.2 | 每个结构的 `confirmed_date` ≥ 其 `date` | 全部成立 |
| T7.3 | 信号 `confirmed_date` ≥ 所属结构 `confirmed_date` | 全部成立 |

> T7.1 是**整个 M0 最重要的一条测试**，必须实现。
>
> ⚠️ **口径说明（实现时澄清）**：缠论结构会被后续 K 线**修订**（同型分型被更极端者取代，
> 于是最后一笔的终点会变），所以「前 N 根的所有结构完全一致」这个字面表述**过严**。
> 真正要保证的是**不依赖未来数据**：任何在截断样本里**已经确认**（终点离样本末 ≥ 2 根）
> 的结构，在更长的样本里必须原样存在。M0 的 `test_truncated_analysis_does_not_depend_on_future_bars`
> 断言的就是这一条。

### 8.8 复权口径（✅ D2 已定）

| # | 用例 | 期望 |
|---|---|---|
| T8.1 | `price_mode=raw` | 计算价 == `raw_high/raw_low/raw_close`（逐值相等） |
| T8.2 | `price_mode=qfq`，该标的**无除权事件** | 计算价 == raw（因子全为 1） |
| T8.3 | `price_mode=qfq`，有除权事件 | **锚点日价格 == raw(锚点日)**；锚点之前的 K 线被向下调整 |
| T8.4 | **稳定性测试（D2 核心）**：给锚点**之后**追加一条新的除权事件后重算 | 锚点日及之前的**所有价格完全不变**；锚点之后的价格不变（`cum` 不含未来事件） |
| T8.5 | `price_mode=hfq` | 最早一根 K 线价格 == raw(最早) |
| T8.6 | 该标的无因子数据 | 不抛异常，价格退化为 raw，并在响应中标记 `adj_factor_missing` |
| T8.7 | 锚点日在样本之外 | 回退到样本内最后交易日，并在响应中回显**实际**锚点 |

> T8.4 是 D2「库里数据永远稳定」的**直接断言**，必须实现。

---

## 9. 金标准标注样本协议（⬜ 需要使用者提供）

没有标注样本，缠论实现的正确性**无法验收**（流派差异极大）。

### 9.1 样本格式

存放于 `backend/tests/fixtures/chan/`（不进 `data/`，不进 git 大文件）：

```
backend/tests/fixtures/chan/<sample_id>.json
```

```jsonc
{
  "sample_id": "000001.SZ_2026Q3",
  "symbol": "000001.SZ",
  "price_mode": "qfq",              // 必须与标注时看到的价格口径一致
  "adj_anchor_date": "2026-10-23",  // qfq 的锚点; null = 样本末日
  "bars": [                       // 不复权原始 K 线（供测试复现，无需入库）
    { "date": "2026-07-01", "high": 12.30, "low": 12.05, "close": 12.18 }
  ],
  "ex_factors": [                 // 该标的的除权事件（复权口径测试需要）
    { "trade_date": "2026-08-10", "ex_factor": 1.032 }
  ],
  "fractals": [
    { "kind": "top", "date": "2026-07-15", "price": 12.98 }
  ],
  "strokes": [
    { "direction": "up", "start_date": "2026-07-03", "end_date": "2026-07-15",
      "start_price": 12.10, "end_price": 12.98 }
  ],
  "segments": [ /* 同构 */ ],
  "pivots": [
    { "zg": 12.70, "zd": 12.35, "start_date": "2026-07-16", "end_date": "2026-07-28" }
  ],
  "signals": [
    { "kind": "b1", "label": "一买", "date": "2026-08-05", "price": 12.02 }
  ]
}
```

> 📌 标注里的**价格必须与 `price_mode` 一致**。
> 若标注时看的是**不复权**行情，就把 `price_mode` 写成 `"raw"`；
> 若看的是**前复权**（多数看盘软件默认），写 `"qfq"` 并给出锚点。
> 口径不一致 → 价格对不上 → 回归测试全部失败。
> **建议**：标注时直接用**不复权**（`raw`），因为它不受锚点影响、最稳定，
> 复权口径的验证交给 §8.8 的 T8.x 单元测试去覆盖。

### 9.2 提供要求

- **3~5 个样本**，覆盖：单边趋势、盘整震荡、含背驰的下跌段。
- 每样本 **100~250 根** K 线。
- 标注精度：**到笔**（不需要精确到每一个分型）。
- 标注人：使用者本人（这是验收基准，不是教学材料）。

### 9.3 验收用法

每个样本生成一条 pytest，断言：
- 笔的**起止日期序列**与标注一致（允许末尾未完成笔的差异）。
- 中枢的 `zg/zd` 误差 ≤ 1 个最小价格变动单位。
- 买卖点日期完全一致。

---

## 10. 阶段与验收

| 阶段 | 内容 | 验收 | 触碰热点 |
|---|---|---|---|
| **M0** ✅ **已实现** | `core.py` 全算法（**成笔条件 / 中枢构造 / 复权口径全部参数化**）+ `test_chan_core.py`（45 条） | ✅ pytest 45 passed；✅ ruff 全绿；✅ T7.1 截断测试通过；⏳ **金标准回归待 §9 样本** | 无 |
| **M1** ✅ **已实现** | 后端扩展 + API + 前端类型 + **结构分析页** + K 线叠加 | ✅ `npx tsc -b --force` + `pnpm build` 通过；✅ Playwright 切走切回 ×6 canvas===1、无 pageerror；⏳ **金标准回归待 §9 样本** | `api.ts`、`queryKeys.ts`、`EChartsCandlestick.tsx` |
| **M1.1** ✅ **已实现** | 信号口径修正：趋势判据改「最后两个同向中枢」；二买/二卖放宽为「不破前低/不破前高」 | ✅ 新增 3 条回归测试；✅ chan/extension 59 passed；✅ ruff 全绿；✅ 400 只样本复测；✅ Playwright 像素差分复验 | 无（仅 `core.py`） |
| ⏸ **停点** | **M1 完成后停下，交使用者肉眼验收结构是否正确**，再决定后续 | 使用者确认 | — |
| **M2** ✅ **第 5 步已实现（落点改写）** | **缠论选股器** —— 落 `backend/app/custom/chan/screener.py` + `POST /api/chan/screen` + 选股页 | ✅ 84 条 chan/extension 测试通过；✅ 全市场 5584 只 / 250 根实测 **7.7s**；✅ 选股结论与结构页**逐项一致**（回归测试锁定）；✅ `npx tsc -b --force` 通过；✅ Playwright 走通 `/chan` + `/chan/screen`，canvas===1、无 pageerror | 无（扩展目录内） |
| **M3**（后续） | 回测策略（`matrix_native`）+ 参数网格 | 信号日≠成交日、T+1、无未来函数 | `strategy/builtin/` |
| **M4**（可选，后续） | 缠论列沉淀到 `pipeline.py` | 需重算 enriched 缓存；`csg_` 自定义信号 / DSL / 评分可零改动复用 | **`pipeline.py`（热点）** |
| **M5**（不建议） | 因子 DSL 加缠论算子 | 成本最高（前端硬编码算子表要手工同步），等 M4 后多数需求不需要 | `dsl.py` + `FactorEditor.tsx` |

> ✅ **已定（使用者决策）**：**先做结构分析页**（M0 → M1），M2 选股策略与 M3 回测**延后**，
> 在分析页被肉眼验收通过之后再启动。
> 理由：缠论的笔/线段/中枢在**没有金标准对照**前是否算对，
> 只有使用者自己能判断；先做策略会把「算法可能不对」放大成「策略结果不可信」。

> ⚠️ **M2 落点改写（第 5 步，2026-11-14）**：原计划把选股做成
> `data/strategies/custom/chan_*.py`（`filter_history` 版策略），**已否决**：
>
> | 候选落点 | 结论 |
> |---|---|
> | `backend/app/strategy/builtin/` | ❌ 内置策略全部 `matrix_native`，且 `tests/backtest/test_matrix_strategy.py` 断言内置策略数 `== 26`；缠论内核是**逐标的时序**的，无法用矩阵表达 |
> | `data/strategies/custom/` | ❌ `.gitignore` 忽略 `data/**` → **不可提交**，等于没实现 |
> | `backend/app/custom/chan/`（✅ 采纳） | 扩展目录，可提交；算法/接口/前端都在一处；不触碰内置策略的硬约束 |
>
> 因此 M2 交付为**独立选股接口 + 独立选股页**，而不是既有「选股」页里的一张策略卡片。
> 若将来要把缠论信号灌进既有策略体系（回测 / 评分 / 监控），仍需另做 M3
> （在 `pipeline.py` 沉淀缠论列，见 M4）。

### M1 实现记录（2026-11-14）

**落地文件**

| 文件 | 内容 |
|---|---|
| `backend/app/custom/chan/api.py` | HTTP 薄层：`GET /api/chan/structure`（薄层：参数校验 / 取数 / 序列化，算法全在 `core.py`） |
| `backend/app/custom/chan/__init__.py` | 扩展注册：`setup()` 挂路由 + `startup()` 绑定运行时 `ExtensionContext`（`repository`） |
| `backend/tests/test_chan_api.py` | 8 条接口契约测试（假仓库句柄，不依赖本机 `data/`） |
| `frontend/src/lib/api.ts` | 缠论类型 + `api.chanStructure()` |
| `frontend/src/lib/queryKeys.ts` | `chanStructure` / `chanKline` 两个 key |
| `frontend/src/components/EChartsCandlestick.tsx` | `chanOverlay` prop：笔/线段走 `markLine` 两点斜线，中枢走 `markArea` 二维矩形 |
| `frontend/src/custom/chan/ChanPage.tsx` | 结构分析页（搜索 / 周期 / 复权口径 / 图上叠加 / 摘要 / 中枢表 / 买卖点表） |
| `frontend/src/custom/chan/extension.tsx` | 前端扩展入口：路由 `/chan` + 导航「缠论结构」 |

**后端**：扩展被 `app.extensions.loader` 自动发现并注册（`EXTENSION_ID = "chan.analysis"`），
`/api/chan/structure` 实测返回 200；`level != 1d` → 501，`price_mode` 非法 → 400，
无数据 → 404，样本 < 30 根 → `summary.status = "insufficient_data"`（fail-closed，不返回空数组冒充「无信号」）。

**前端验收实测**（Playwright，Chromium）

| 项 | 结果 |
|---|---|
| `npx tsc -b --force` | ✅ 无错误 |
| `pnpm build` | ✅ 构建成功 |
| 切走→切回 ×6（SPA 内部导航） | ✅ 每次回到 `/chan` 时 `canvas === 1` |
| 整页刷新 | ✅ `canvas === 1` |
| pageerror | ✅ 无（仅 Google Fonts 拉不通的 `ERR_FAILED`，与本次改动无关） |
| 接口请求 | ✅ `/api/chan/structure` 与 `/api/kline/daily` 均 200 |

**叠加层「画出来了」的客观证据**（不靠肉眼，用像素差分：同一页面跑两次，
第二次把接口返回的 `strokes/segments/pivots` 置空后重画，两次画布做差）

| 图层 | 差分像素 | 判定 |
|---|---|---|
| 中枢矩形 | `#8c5af7` 33 594 px | ✅ 紫色填充已绘制 |
| 笔 / 线段 | 红绿线连通域 42 个，最大 `dx=24 / dy=95` | ✅ x 与 y 相关系数 \|corr\| ≈ 0.9~0.99 → **是斜线，不是水平线** |

**价格对齐验证**（图上叠加线与蜡烛必须落在同一价位口径）：

- 缠论 `closes`（qfq，锚点=样本末日）vs 日 K 接口前复权 `close`：**最大绝对差 4.99e-7**（纯四舍五入）✓
- 3 只标的（000001.SZ / 600000.SH / 000651.SZ）共 126 个笔端点：**100% 落在对应 K 线的 [low, high] 区间内** ✓
- 9 个中枢的 `[zd, zg]`：**100% 落在中枢区间内的价格极值范围内** ✓

> 结论：图上叠加与蜡烛**同口径**，不需要在前端二次复权。

**M1.1 前端复验（Playwright, Chromium, 端口 3011）**

以 `000069.SZ`（修正后同时含 b1/b2/b3/s3）为例，用**路由拦截**分别只放行
「无信号 / 仅买点 / 仅卖点 / 全部」四种返回，再对同一画布做像素差分：

| 对比 | 差分像素 | 差分区域 (bbox) | 判定 |
|---|---|---|---|
| 无信号 vs 仅买点 | 856 | 右下 `(603,396)-(917,473)` | ✅ 买点箭头 + 「一买/二买」文字已绘制 |
| 无信号 vs 仅卖点 | 376 | 中上 `(483,177)-(674,376)` | ✅ 卖点箭头已绘制 |
| 仅买点 vs 全部 | 376 | 同卖点区域 | ✅ 卖点叠加不污染买点，二者独立 |

> 结论：修正后**买点确实画在图上**（此前不是前端漏画，是后端几乎不产生买点）。
> 该标的接口实测返回：`s3 2026-05-11 / b1 2026-06-29 / s3 2026-07-17 / b2 2026-07-27 /
> b1 2026-09-16 / b2 2026-10-15 / b1 2026-10-30`。

**已知限制（M1 范围外）**

- 只做**日线级别**；分钟级别接口 501（fail-closed）；
- 买卖点标签是**笔级近似**（二买/三买非真次级别），页面未单独标注 → 见 §11 D7；
- 分型层（M0 内核已算）**没有画到图上**（当前只画笔/线段/中枢/买卖点）；
- 页面**未做**复权锚点的手工输入（接口支持 `adj_anchor_date`，前端暂用默认锚点）。

### M1.1 实现记录（2026-11-14）—— 信号口径修正

**触发**：使用者实测反馈「只见到了卖点，没有见到买点」。

**根因（两处，均为实现口径缺陷，非缠论原文问题）**

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | 一买/一卖的「下跌/上涨趋势」门控取**前两个**同向中枢，而信号点却挂在**最后一个**中枢之后 | 判据描述的是**历史**结构而非当前结构 → 先涨后跌的票买点被误杀、卖点被放宽 |
| 2 | 二买/二卖把「回抽笔本身背驰」当作**必要条件** | 实测 400 只样本 **二买恒为 0**，买点几乎绝迹 |

**修正**

1. 新增 `same_direction_trend(pivots, direction)`，统一取**最后两个**同向中枢判定；
   `classify_trend` 与 `build_signals` 共用；
2. 二买/二卖放宽为「回抽不破前低 / 反抽不破前高」，回抽笔是否背驰改用
   `detail.pullback_diverged` 作为**加强标记**。

**回归测试**（`backend/tests/test_chan_core.py`，新增 3 条）

- `test_classify_trend_uses_last_two_same_direction_pivots`：前两个不重叠、后两个重叠 → 判盘整；
- `test_classify_trend_detects_later_same_direction_pivots`：前两个重叠、后两个不重叠 → 判趋势；
- `test_second_buy_fires_when_pullback_is_not_divergent`：回抽笔不背驰但未破一买低点 → 仍出二买。

**400 只样本实测（`days=250`, `price_mode=qfq`, 2025-10-05 ~ 2026-11-14）**

| 口径 | 修正前 | 修正后 |
|---|---|---|
| 一买 `b1` | 37 | **46** |
| 二买 `b2` | 0 | **9** |
| 三买 `b3` | 157 | 157 |
| 一卖 `s1` | 28 | **18** |
| 二卖 `s2` | 0 | **1** |
| 三卖 `s3` | 435 | 435 |

> 走势分布 `oscillation 217 / down 137 / up 46`；165/400 只至少有一个买点，41 只无任何信号。
> 样本区间为下跌年 → 破 `ZD` 的机会远多于破 `ZG`，故三卖（三买）天然多于三买（一类买点），
> 属市场结构使然，非算法偏置。

### M0 实现记录（2026-11-13）

**落地文件**

| 文件 | 内容 |
|---|---|
| `backend/app/custom/chan/core.py` | 算法内核（包含处理 / 分型 / 成笔 / 线段 / 中枢 / 走势 / 背驰买卖点 / 复权口径），零 FastAPI 依赖 |
| `backend/app/custom/chan/__init__.py` | 扩展注册入口（`EXTENSION_ID = "chan.analysis"` + `setup()`），M1 在此挂 HTTP 路由 |
| `backend/tests/test_chan_core.py` | 45 条单测（M1.1 新增 3 条回归） |

**扩展机制实测**：✅ `app.extensions.loader` 自动发现 `app.custom.chan`
（`_custom_module_names()` 返回 `['app.custom.chan']`），`setup(registrar)` 注册成功，
当前不含路由（M0 阶段）。选 `backend/app/custom/` 而不是 `app/api/` 的理由见 §3.1。

**真实数据冒烟**（30 只随机标的 · 2025-09-08 ~ 2026-11-13 日线 · `price_mode=qfq`）：
每只 21~29 根笔、0~5 段线段、2~5 个中枢；走势分布 `up 3 / down 8 / oscillation 19`；
买卖点合计 55 个。无异常抛出。

**实现期发现并修正的三处口径**（早期文字与实现不一致，已在上文标注）

1. **中枢方向必须取「进入段」的方向**，不能取「中枢第一笔」的方向 ——
   否则相邻中枢方向必然交替，**趋势永远无法成立**（实测全部 `oscillation`）。
2. **特征序列分型需要两个口径**（`strict` / `extreme`）——
   严格原文版（高低同时极端）在真实数据上常常**一个线段都切不出来**。
3. **三买/三卖要用「离开中枢的那一笔 + 其后的回抽笔」**，
   不能用「中枢之后任意一根突破笔」—— 否则会漏掉真正的离开笔并产生重复信号。

**已知限制（M0 范围外）**

- 一买/一卖要求**存在同向不重叠的两个中枢**（趋势背驰），盘整背驰暂不出信号；
- 二买是**笔级近似**（页面须标注），真次级别需 §4.8 递归接口；
- **末段未确认不输出**（线段/中枢的未完成部分不外泄）；
- 只做**日线级别**，分钟级别的次级别递归接口只有签名（`analyze_sublevel` 直接抛 `NotImplementedError`）。

### M1/M2 的策略接口要点（✅ 已核实）

- `META` 需声明 `asset_types`（非空列表）与 `timeframes`（✅ `engine.py:457-469`）。
- 执行后端二选一，**不可同时**定义 `filter()` 与 `filter_history()`（✅ `strategy-guide.md:97`）。
- `filter_history` 需声明 `LOOKBACK_DAYS` 与 `REQUIRED_FEATURES`（✅ `engine.py:572`）。
- 策略只能放 `data/strategies/custom/`（用户目录）或 `backend/app/strategy/builtin/`（内置）。
- 回测侧用 `MatrixStrategy` Protocol（✅ `matrix.py:3472`），
  范例 `backend/app/strategy/builtin/ma_golden_cross.py:62`，
  `make_signal_matrix(..., entry_signal_ids=..., exit_signal_ids=...)`（✅ `matrix.py:2234`）。
- 买点级别用**不同 signal id**（`signal_chan_1buy` / `2buy` / `3buy`）→
  `TradeRecord.entry_signal_id`（✅ `backtest/engine.py:98`）→ 前端 `StrategyBacktestTrade`（✅ `api.ts:1516`），
  **无需改动撮合层**即可分级别归因。

### M2 实现记录（2026-11-14）—— 段级中枢 / 段背驰 / 缠论选股器

**落地文件**

| 文件 | 内容 |
|---|---|
| `backend/app/custom/chan/core.py` | `Segment.k_count` / `amplitude` / `strength`；`ChanParams.min_segments_per_pivot`；`_Range` + `_build_pivots_from_ranges`（笔级/段级**共用**的中枢构造内核）；`build_segment_pivots`；`SegmentDivergence` + `detect_segment_divergence` + `classify_segment_divergence_scope`；`ChanStructure.segment_pivots` / `segment_divergences` |
| `backend/app/custom/chan/screener.py` | 选股器内核：全市场一次性复权预计算 + 按标的分块 `ProcessPoolExecutor` 并行 + 条件判定 |
| `backend/app/custom/chan/api.py` | `POST /api/chan/screen`（薄层）+ `_segment_payload` 增字段 + `_segment_divergence_payload` |
| `backend/tests/test_chan_core.py` | +11 条段级用例（中枢构造 / 段背驰 / 形态归属 / 未确认末段不参与） |
| `backend/tests/test_chan_api.py` | +4 条契约用例（段级字段 / summary 计数 / 选股入参校验） |
| `backend/tests/test_chan_screener.py` | 新增 9 条：qfq 口径与 `core.apply_price_mode` 逐根一致；选股结论与逐标的 `analyze()` 逐项一致；条件判定 |
| `frontend/src/lib/api.ts` | `ChanSegmentDivergence` / `ChanSegment` 增字段 / `ChanSummary` 增计数 / `ChanScreenRequest` / `ChanScreenResponse` / `api.chanScreen()` |
| `frontend/src/lib/queryKeys.ts` | `chanScreen` |
| `frontend/src/components/EChartsCandlestick.tsx` | `ChartPivot.nestedLevel`（段中枢走青色通路）+ `ChartDivergence` + `segmentDivergences`（pin 形状 + 橙色） |
| `frontend/src/custom/chan/ChanPage.tsx` | 段中枢 / 段背驰面板 + 图例 + `?symbol=` 预填 |
| `frontend/src/custom/chan/ChanScreenPage.tsx` | 选股页（条件栏 + 结果表 + 点回详情） |
| `frontend/src/custom/chan/extension.tsx` | 路由 `/chan/screen` + 导航「缠论选股」 |

**口径一致性（选股器 ↔ 结构页）**

选股器在全市场层面一次性算复权（`cum(D)` 铺到每个 `(symbol, date)`，再逐标的除以
自己的锚点因子 `cum(样本末日)`），结果与逐标的调用 `core.apply_price_mode` **逐根一致**
→ 结构结论完全相同。回归测试 `test_qfq_frame_matches_core_apply_price_mode` 与
`test_scan_frame_matches_per_symbol_analyze` 锁死这条。

**性能实测（5584 只 / 250 个交易日 / 本机 20 核）**

| 环节 | 耗时 |
|---|---|
| 列出 8763 个分区 + 组装 250 个文件清单 | 0.2s |
| 读 250 个分区（5 列） | 0.6s |
| 全市场复权预计算（join_asof + 逐标的归一） | 0.6s |
| 分块写临时 parquet | 0.9s |
| 8 进程并行算结构 | 5.3s |
| **合计** | **≈ 7.7s** |

> ⚠️ 两个实测踩坑（都写进了代码注释）：
> 1. `lf.select("date").unique()` 全表 distinct 要 **12.4s**，`Path.glob("date=*")` 要 **3.5s**
>    → 改用 `os.scandir` + **显式文件清单**（0.2s / collect 0.6s）。
> 2. 单线程全市场 analyze 要 **37s**（纯 Python，受 GIL 限制）→ 必须多进程；
>    子进程入口必须是**模块级函数**，且调用方脚本顶层代码要有 `if __name__ == "__main__"` 保护。

**已知限制**

- 选股器只扫**股票**（enriched parquet 主体），不含 ETF / 指数；
- 全市场扫描是**同步请求**（约 8s），没有做后台任务 / 渐进式返回；
- 条件是**结构化白名单**（不是自然语言 / 不是任意公式）；
- 选股结果只给**结构摘要**，不直接给买卖建议。

**前端验收实测（Playwright / Chromium，前端 3011 + 后端 3018）**

| 检查项 | 结果 |
|---|---|
| `/chan` 渲染 | `canvas` 数 **1**（无重复画布） |
| `/chan` 段级面板 | 「段中枢」「段背驰」面板与「段背」图例均存在 |
| `/chan/screen` 条件栏 | 存在（买卖点 / 背驰等维度） |
| 点「开始扫描」 | 返回 **200 行**结果表，耗时字段可见 |
| 结果行 → 详情页联动 | 行内链接 `/chan?symbol=920405.BJ`，点击后跳转 `/chan?symbol=920405.BJ`，`canvas` 数 **1** |
| pageerror / console.error | **无** |

> 首行样例：`海希通讯 920405.BJ 13.41 上涨 二买 二买 三买 一买 一卖 三卖 24/3 4/1 笔背`
> （`笔段 24/3` = 24 笔 3 段，`4/1` = 笔级中枢 4 个 / 段级中枢 1 个）

---

## 11. 未决决策清单

**✅ 已定 5 项**（使用者 2026-11-13 决策）：D1 / D2 / D3 / D5 / D6。
**🟡 M0 期间实现方先定口径（待使用者复核）**：D9 / D11 / D13。
**🟡 第 4/5 步实现方先定口径（待使用者复核）**：D14 / D15。
**⬜ 剩余待确认**：D4 / D7 / D8 / D10 / D12。「默认实现」列是本文为推进设计而给的默认值，
未经确认**不得**作为验收标准。

| # | 决策 | 状态 | 结论 / 默认实现 | 影响面 |
|---|---|---|---|---|
| **D1** | 缠论版本 | ✅ 已定 | **严格原文版**，成笔条件与中枢构造**参数暴露** | §4.3、§4.5、全部算法 |
| **D2** | 价格口径 | ✅ 已定 | 存**原始价 + 复权因子序列**（库内永远稳定）；**复权口径暴露为参数**；默认 `qfq` + 锚点回显 | §2.1、全部算法与复现性 |
| **D3** | 成笔条件 | ✅ 已定 | 参数暴露：`stroke_version` / `min_gap_bars` / `min_k_count` / `strict_no_shared_kline` | §4.3 |
| **D4** | 线段破坏是否实现「第二种情况（特征序列缺口）」 | ⬜ 待确认 | 默认**不实现** | §4.4 |
| **D5** | 级别策略 | ✅ 已定 | **先做日线**，**预留日线内的次级别递归分析接口** | §4.8、§4.5 `pivot_level_mode` |
| **D6** | 背驰力度度量 | ✅ 已定 | **笔力度 / 笔背离公式**（使用者自定义，不用 MACD 面积） | §4.7 |
| **D7** | 二买/三买的次级别如何近似 | ⬜ 待确认 | 默认 M0/M1 用「笔」近似次级别（页面须标注为近似） | §4.7、§4.8 |
| **D8** | 是否允许引入第三方缠论库 | ⬜ 待确认 | 默认**不引入** | 依赖清单 |
| **D9** | `BIOK` 的确切含义 | 🟡 **M0 已定口径** | 按缠论「BIOK = 笔成立」实现（该笔通过成笔条件、终点分型已确认）；若澄清为别的含义，改 `detect_bi_divergence` 一处即可 | §4.7 |
| **D10** | 「笔天数」「笔幅度」口径 | ⬜ **待澄清** | 默认：天数 = 交易日（K 线根数）；幅度 = 绝对价格差（非百分比） | §4.7 |
| **D11** | 中枢「不重叠」严格定义 | 🟡 **M1.1 已定口径** | 下跌趋势取 `ZG_2 < ZD_1`，上涨趋势取 `ZD_2 > ZG_1`（区间完全分离）；**取最后两个同向中枢**（M1.1 修正，见 §4.6） | §4.6 |
| **D12** | `qfq` 默认锚点策略 | ⬜ 待确认 | 默认：样本内最后交易日，并在响应中**回显实际锚点** | §2.1.3、复现性 |
| **D13** | **特征序列分型的判定口径** | ⬜ **待确认（M0 默认 `extreme`）** | `strict` = 高点与低点同时最极端（严格原文版，实测常常切不出线段）；`extreme` = 只比同向极值（实务通行版）。**必须用 §9 金标准样本重判** | §4.4 |
| **D14** | **盘整背驰的处置口径** | ✅ **第 4 步已定（实现方口径，待使用者复核）** | 盘整背驰**只作结构标记，不产生买卖点**（笔背驰判定本身仍不区分趋势/盘整；买卖点只消费 `scope == "trend"` 的背驰） | §4.7 |
| **D15** | **段级中枢 / 段背驰是否单列** | ✅ **第 5 步已定（实现方口径，待使用者复核）** | 段级中枢（`nested_level=1`，用**线段**构造）与段背驰**单列**、与笔级**分开存放**（索引坐标系不同）；不参与笔级买卖点判定 | §4.5、§4.7 |

**另需使用者提供**：§9 的金标准标注样本（3~5 个）。

---

## 12. 变更影响面与升级兼容

| 影响 | 说明 |
|---|---|
| **数据** | 只读，不写 `data/`。不新增/修改任何 Parquet schema |
| **缓存** | 缠论结果按 `(symbol, price_mode, bars_hash)` 内存缓存；**不写入** 策略结果缓存 |
| **provider** | 不新增数据集，不改 `capabilities.py`，不绕开 provider 抽象 |
| **前端缓存** | 新增查询键 `chan-structure`，不进 `SSE_INVALIDATE_PREFIXES` |
| **历史兼容** | M0~M3 不改任何既有列/信号/策略格式 → **零迁移成本** |
| **上游升级** | 触碰的热点文件：`lib/api.ts`、`lib/queryKeys.ts`、`components/EChartsCandlestick.tsx`（均为**新增**成员，不改既有行为）→ 冲突面小；`backend/app/main.py` **不动** |
| **回滚** | 删 `backend/app/custom/chan.py`、`frontend/src/custom/chan/`、新增的 api/queryKey/prop 即可完全回滚 |

---

## 13. 完成检查表（照抄 `CONTRIBUTING §9` 与 `secondary-development.md §9`）

**M0（算法内核）已勾选的部分：**

- [x] 已证明使用的扩展点真实存在（后端 `registrar.include_router` ✅ / 前端 `FrontendExtension` ✅）
- [x] 未复制既有数据读取、缓存、API 客户端或核心页面
- [x] **T7.1 截断测试通过（无未来函数）** —— `test_truncated_analysis_does_not_depend_on_future_bars`
- [x] 单元测试覆盖正常路径 / 边界 / 失败路径（45 条，正常/边界/非法参数/缺数据四类）
- [x] `ruff check` 无新增告警（`app/custom/chan/` + `tests/test_chan_core.py` 全绿）

**M1.1（信号口径修正）已勾选的部分：**

- [x] 两处口径缺陷定位到具体代码（趋势判据取「前两个」中枢 / 二买误把背驰当必要条件）
- [x] 新增 3 条回归测试锁定新口径（`test_classify_trend_uses_last_two_same_direction_pivots` /
      `test_classify_trend_detects_later_same_direction_pivots` /
      `test_second_buy_fires_when_pullback_is_not_divergent`）
- [x] `pytest tests/test_chan_core.py tests/test_chan_api.py tests/test_extensions.py` → 59 passed
- [x] `ruff check app/custom/chan tests/test_chan_core.py` → All checks passed
- [x] 400 只样本复测（买点 37→46+9，一卖 28→18）
- [x] Playwright 像素差分复验买点确实画在图上（000069.SZ：无信号 vs 仅买点 差分 856 px）

**M2（第 5 步：段级中枢 / 段背驰 / 选股器）已勾选的部分：**

- [x] 段级中枢与笔级中枢**共用一套构造内核**（`_build_pivots_from_ranges`），不复制粘贴
- [x] 段级中枢与笔级中枢**分开存放**（索引坐标系不同：段下标 vs 笔下标）
- [x] 段背驰判定与笔背驰**同构**（D6 公式，只换比较单元）；未确认末段不参与
- [x] 盘整背驰只作结构标记、不产生买卖点（D14）
- [x] 选股器落 `backend/app/custom/chan/`（不落 builtin —— 内置策略 `== 26` + `matrix_native` 硬约束；
      不落 `data/strategies/custom/` —— `.gitignore` 忽略，不可提交）
- [x] 选股器口径与结构页**逐项一致**（`test_scan_frame_matches_per_symbol_analyze` 锁定）
- [x] 全市场扫描实测 7.7s（5584 只 / 250 根）
- [x] `npx tsc -b --force` 通过（+ `vite build` 通过）
- [x] `ruff check app/custom/chan tests/test_chan_*.py` → All checks passed
- [x] `pytest tests/test_chan_core.py tests/test_chan_api.py tests/test_chan_screener.py tests/test_extensions.py` → 84 passed
- [x] Playwright 走通 `/chan` + `/chan/screen` 交互路径：canvas===1、无 pageerror、选股出 200 行、点行跳详情页成功

**M1（分析页）与全局项待办：**

- [ ] 已确认 §11 全部决策项（D1~D8、D13）
- [ ] 已收到并落库 §9 金标准样本
- [x] `npx tsc -b --force` + `pnpm build` 通过
- [x] Playwright 走「进入→切走→切回×6」路径，canvas===1，无 pageerror
- [ ] `git diff --check` 通过，最终 diff 无调试代码与无关文件
- [ ] 最终说明含：方案分级、修改文件、缓存/数据/API/升级影响、实际验证结果、剩余风险
