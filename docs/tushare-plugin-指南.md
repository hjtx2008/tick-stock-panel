# Tushare 数据源插件 — 启用指南

## 一、这个插件做了什么

在**不改动任何 service / API 代码**的前提下，新增了一个可切换的 A 股数据源，提供三个数据集：

| 数据集 | 内容 | 说明 |
|---|---|---|
| `daily` | 日K（未复权原始价） | 复权由项目 `pipeline` 用本地因子计算，provider 不做复权 |
| `adj_factor` | 除权因子 | 官方累积因子序列 → 还原为项目契约的单事件 pre/post 比值 |
| `financial` | 财务（metrics / income / balance_sheet / cash_flow / shares） | 见第三节，逐表代价差异很大 |

未声明 `minute` / `realtime`，这两类会**自动回退**到你现有的数据源（stocksdk / tickflow），不会有任何功能缺失。

### 为什么必须做成插件

设置页的「新增自定义源」是声明式 YAML，要求扁平参数 + header/query 鉴权 + 响应为对象数组；而 Tushare 是单端点 POST `{api_name, token, params:{...}}`，token 在 body 内、参数嵌套、响应列名与二维行分离。三者结构性不匹配，只能走插件。

反过来，好处是：**注册表是自动发现的**——插件放对目录后，设置页下拉框会自己多出 `tushare` 选项，不需要改任何既有代码。

---

## 二、启用步骤（你自己操作）

### 第 1 步 — 配置 Token

1. 打开 **设置 → 数据源**，找到 **tushare** 卡片
2. 在卡片输入框里粘贴你的 Tushare Token（个人中心 → 接口TOKEN 获取）
3. 点「探测」——会先实探一次再落盘，**无效 Token 不会被保存**
4. 保存后卡片状态变为「可用」，Token 存在 `data/user_data/secrets.json`（优先级高于 `.env`）

> 也可以直接在 `backend/.env` 里写 `TUSHARE_API_KEY=你的token`，效果相同。

### 第 2 步 — 切换数据源

在同一个页面把：

- **日K** → 选 `tushare`
- **除权因子** → 选 `tushare`
- **财务** → 可选 `tushare`（先看完第三节的代价再决定）
- **实时 / 分钟** → **保持 stocksdk 不变**（Tushare 没有生产级实时接口）

### 第 3 步 — 试拉验证

在 tushare 卡片点「试拉测试」，确认返回 `rows > 0` 且字段正常：

```
daily      → columns: symbol, date, open, high, low, close, volume, amount
adj_factor → columns: symbol, trade_date, ex_factor
financial  → columns: symbol, period_end, announce_date, roe, roa, gross_margin,
             net_margin, debt_to_asset_ratio, revenue_yoy, net_income_yoy, eps, bps
```

> `financial` 的试拉固定走 **metrics**（批量接口，秒级返回），不会触发三表逐只拉取。

### 第 4 步 — 触发同步

保存设置后执行一次日K同步 / 除权因子同步，观察日志。

---

## 三、数据口径（改动代码前必看）

| 字段 | Tushare 原始 | 转换 | 项目契约 |
|---|---|---|---|
| `vol` | 手 | **不转换** | 手 |
| `amount` | **千元** | **×1000** | 元 |
| `adj_factor` | 累积因子（每日一个值）| `f(d)/f(d-1)` | 单事件 pre/post 比值 |
| `trade_date` | `20240102` | 归一 | `date` |

三个容易踩的点：

1. **`amount` 是千元**，不转换会让成交额小 1000 倍。
2. **`ex_factor` 必须是单事件比值**，直接把官方累积因子写进去会导致复权价全错。
3. **`daily_basic` 的 `total_share`/`float_share` 是万股**，必须 ×10000 转股；漏了会让所有市值/换手因子小 1 万倍。

### 财务数据集口径（2026-09 实测）

| 项目表 | Tushare 接口 | 批量 | 全市场请求数 | 单位/口径要点 |
|---|---|---|---|---|
| `metrics` | `fina_indicator` | ✅ 50 只/批 | ≈ 111 次（秒级） | 单次返回**封顶 100 行且静默截断** → 客户端触顶自动折半重拉 |
| `income` | `income` | ❌ | ≈ 5550 次 | `ts_code` 硬必填（不传报 50101）；多 code 返回 0 行**且不报错** |
| `balance_sheet` | `balancesheet` | ❌ | ≈ 5550 次 | 表名 ≠ 接口名；权益合计用 `total_hldr_eqy_inc_min_int` |
| `cash_flow` | `cashflow` | ❌ | ≈ 5550 次 | `n_cashflow_act` 等前缀很反直觉 |
| `shares` | `daily_basic` | ✅ 按日全市场 | 1 次/月（按月抽样 24 期） | 单位**万股**，×10000 转股 |

`start_date`/`end_date` 按 **ann_date（公告日）** 筛，不是报告期。
同一报告期官方会返回多行（合并/单季/调整），插件按 `report_type=1` 优先、其次 `ann_date` 最新去重。

---

## 四、性能设计

- **全市场同步走「按交易日取数」**：单次请求上限 6000 行，全市场约 5400 只，一天一次请求就能拉完。一年历史 ≈ 243 次请求；而按 `ts_code` 逐只拉需要 5000+ 次。
- **标的数 < 100 时自动切回逐标的模式**，避免为几只股票拉回全市场再丢弃。
- **交易日历不可用时自动回退**逐标的模式（不会整体失败）。
- 请求默认节流 0.15s（约 400 次/分），留出限频余量。你 2000 积分档完全够用（`daily` / `adj_factor` 各需 120 积分）。
- **财务三表是逐只的**，节流 0.08s，全市场三表合计约 16650 次请求 ≈ 25~30 分钟（后台线程跑，前端轮询进度）。
  首装是 `latest_only=False`（每只补 8 期），之后增量只拉最新一期。
- 若你更看重速度，可以只把**财务**留给 fuyao、日K/除权因子走 Tushare —— 两者可以在设置页分别切换。

## 五、失败处理

- 个别日期/标的失败 → 只告警，继续
- **失败过半 → 直接抛错中止**，避免半截数据落库（资金流向面板曾因此吃过亏）

---

## 六、文件清单

```
backend/app/plugins/tushare/
├── plugin.yaml    # 清单: name/entry/check/datasets/api_key_env
├── __init__.py
├── client.py      # HTTP: 鉴权、信封解包、分页、节流
└── provider.py    # 契约: 单位换算、字段映射、取数模式、test_dataset

backend/tests/test_tushare_provider.py    # 34 个契约测试（daily / adj_factor）
backend/tests/test_tushare_financial.py   # 18 个契约测试（财务：单位、去重、批量上限、失败率）
```

（均为假 client，不依赖网络与 Token）

## 七、验证结果

- `ruff check` — 全通过
- `pytest tests/test_tushare_provider.py tests/test_tushare_financial.py` — **52 passed**
- 回归 fuyao / custom / preferences / settings 相关用例 — **112 passed**，现有数据源不受影响
- 真实接口冒烟（已验证）：metrics 3 只、income 2 只、shares 全市场抽样均返回正确数值
  （茅台 2026H1 营收 907.03 亿 / 归母 445.17 亿 / 总股本 12.501 亿股，与公开数据一致）
- 注册表集成：未配 Token 时灰显提示「缺少 Token」；填入后自动进入设置页候选源 `['fuyao', 'stocksdk', 'tickflow', 'tushare']`

## 八、后续可做（P2）

都在同一个插件里加即可，无需再动其他代码：

- `index_daily` — 指数日K
- `top_list` / `top_inst` — 龙虎榜
- `share_float` — 限售解禁（3000 积分）
- `fina_mainbz` — 主营业务构成
- `daily_basic` 的完整每日指标（PE/PB/换手率）目前只取了股本两列，可扩为估值快照
