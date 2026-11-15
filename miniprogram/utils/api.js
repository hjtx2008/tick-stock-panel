// 后端接口清单 (与网页端 frontend/src/lib/api.ts 同一批端点, 只取手机端用到的)。
const { request, get, post, qs } = require('./request')
const config = require('./config')

module.exports = {
  // ---- 认证 ----
  authStatus: () => get('/api/auth/status'),
  async login(password) {
    const res = await request('/api/auth/login', { method: 'POST', data: { password } })
    if (!res.session) throw new Error('登录成功但未取到会话, 请升级微信基础库后重试')
    config.setSession(res.session)
    return res.data
  },
  logout: () => post('/api/auth/logout').finally(() => config.setSession('')),

  // ---- 自选 (enriched 最新日; 盘中由后端实时行情回写, 轮询即可拿到最新价) ----
  watchlistEnriched: () => get('/api/watchlist/enriched'),

  // ---- 个股日 K (前复权, 盘中含当日实时蜡烛) ----
  // days 是自然日。后端只缓存最近约 300 天, 超出部分要扫全部分区 (实测 11~18 秒),
  // 手机端取 290 天保证秒开。
  klineDaily: (symbol, days = 290) => get('/api/kline/daily?' + qs({ symbol, days }), { timeout: 60000 }),

  // ---- 选股 ----
  strategies: () => get('/api/screener/strategies'),
  cachedSummary: () => get('/api/screener/cached-summary'),
  cachedResult: (strategyId) => get('/api/screener/cached-result/' + encodeURIComponent(strategyId)),
  // summary_only + 日线 → 后端渐进返回: 快策略先返回, 慢策略后台继续写缓存
  runAllStrategies: () => post('/api/screener/run_all', { summary_only: true }, { timeout: 60000 }),

  // ---- 缠论 ----
  // 单股缠论结构当前要 11~18 秒 (后端单股历史扫描慢, 见 README 已知问题), 超时放宽到 60 秒
  chanStructure: (symbol, days = 250) =>
    get(
      '/api/chan/structure?' + qs({ symbol, days, price_mode: 'qfq', include: 'structure,signals,divergences' }),
      { timeout: 60000 },
    ),
  // 全市场扫描约 5~10 秒
  chanScreen: (body) => post('/api/chan/screen', body, { timeout: 120000 }),

  // ---- 告警 ----
  alerts: (days = 3, limit = 200) => get('/api/alerts?' + qs({ days, limit })),
}
