// 后端地址与会话的本地存储。局域网自用: 地址形如 http://192.168.1.10:3018
const KEY_BASE_URL = 'tsp_base_url'
const KEY_SESSION = 'tsp_session'

function normalizeBaseUrl(raw) {
  let url = String(raw || '').trim()
  if (!url) return ''
  if (!/^https?:\/\//i.test(url)) url = 'http://' + url
  return url.replace(/\/+$/, '')
}

function getBaseUrl() {
  return wx.getStorageSync(KEY_BASE_URL) || ''
}

function setBaseUrl(raw) {
  const url = normalizeBaseUrl(raw)
  wx.setStorageSync(KEY_BASE_URL, url)
  return url
}

function getSession() {
  return wx.getStorageSync(KEY_SESSION) || ''
}

function setSession(token) {
  if (token) wx.setStorageSync(KEY_SESSION, token)
  else wx.removeStorageSync(KEY_SESSION)
}

module.exports = { normalizeBaseUrl, getBaseUrl, setBaseUrl, getSession, setSession }
