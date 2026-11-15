// wx.request 封装。
// 后端认证走 HttpOnly cookie (tf_session); 小程序不会自动管理 cookie,
// 登录时从响应里取出会话值存本地, 之后每个请求手动带 Cookie 头。
const config = require('./config')

const COOKIE_NAME = 'tf_session'

class ApiError extends Error {
  constructor(message, status, code) {
    super(message)
    this.status = status
    this.code = code
  }
}

function extractSession(res) {
  const pick = (text) => {
    const m = new RegExp(COOKIE_NAME + '=([^;,\\s]+)').exec(String(text || ''))
    return m ? m[1] : ''
  }
  for (const c of res.cookies || []) {
    const v = pick(typeof c === 'string' ? c : `${c.name}=${c.value}`)
    if (v) return v
  }
  const header = res.header || {}
  return pick(header['Set-Cookie'] || header['set-cookie'])
}

function detailOf(data, fallback) {
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string') return data.detail
    if (Array.isArray(data.detail)) return data.detail.map(d => d.msg || '').join('; ')
  }
  return fallback
}

function request(path, { method = 'GET', data, timeout = 15000 } = {}) {
  const base = config.getBaseUrl()
  if (!base) {
    return Promise.reject(new ApiError('未配置后端地址, 请到「设置」填写', 0, 'NO_BASE_URL'))
  }
  const headers = { 'content-type': 'application/json' }
  const session = config.getSession()
  if (session) headers.Cookie = `${COOKIE_NAME}=${session}`

  return new Promise((resolve, reject) => {
    wx.request({
      url: base + path,
      method,
      data,
      header: headers,
      timeout,
      success(res) {
        const { statusCode, data: body } = res
        if (statusCode >= 200 && statusCode < 300) {
          resolve({ data: body, session: extractSession(res) })
          return
        }
        if (statusCode === 401) {
          config.setSession('')
          reject(new ApiError('未登录或会话已过期, 请到「设置」登录', 401, 'UNAUTHORIZED'))
          return
        }
        const code = body && body.code
        reject(new ApiError(detailOf(body, `请求失败 (HTTP ${statusCode})`), statusCode, code))
      },
      fail(err) {
        const msg = /timeout/i.test(err.errMsg || '')
          ? '请求超时, 请确认手机与电脑在同一局域网且后端已启动'
          : '无法连接后端: ' + (err.errMsg || '网络错误')
        reject(new ApiError(msg, 0, 'NETWORK'))
      },
    })
  })
}

const get = (path, opts) => request(path, { ...opts, method: 'GET' }).then(r => r.data)
const post = (path, data, opts) => request(path, { ...opts, method: 'POST', data }).then(r => r.data)

function qs(params) {
  return Object.keys(params)
    .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`)
    .join('&')
}

module.exports = { request, get, post, qs, ApiError }
