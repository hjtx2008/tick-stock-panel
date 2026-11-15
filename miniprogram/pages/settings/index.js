const api = require('../../utils/api')
const config = require('../../utils/config')

Page({
  data: {
    baseUrl: '',
    password: '',
    statusText: '',
    statusError: false,
    configured: false,
    authenticated: false,
    loggingIn: false,
  },

  onShow() {
    this.setData({ baseUrl: config.getBaseUrl() })
    if (config.getBaseUrl()) this.refreshStatus()
  },

  onBaseUrlInput(e) {
    this.setData({ baseUrl: e.detail.value })
  },

  onPasswordInput(e) {
    this.setData({ password: e.detail.value })
  },

  onTest() {
    const url = config.setBaseUrl(this.data.baseUrl)
    if (!url) {
      this.setData({ statusText: '请先填写后端地址', statusError: true })
      return
    }
    this.setData({ baseUrl: url })
    this.refreshStatus()
  },

  async refreshStatus() {
    this.setData({ statusText: '连接中…', statusError: false })
    try {
      const s = await api.authStatus()
      let text = '已连接'
      if (!s.configured) text = '已连接 (后端未设置访问密码, 局域网内可直接使用)'
      else if (s.authenticated) text = '已连接, 已登录'
      else text = '已连接, 需要登录'
      this.setData({
        statusText: text,
        statusError: false,
        configured: !!s.configured,
        authenticated: !!s.authenticated,
      })
    } catch (err) {
      this.setData({ statusText: err.message, statusError: true, configured: false, authenticated: false })
    }
  },

  async onLogin() {
    if (this.data.loggingIn) return
    if (!this.data.password) {
      wx.showToast({ title: '请输入密码', icon: 'none' })
      return
    }
    this.setData({ loggingIn: true })
    try {
      await api.login(this.data.password)
      this.setData({ password: '' })
      wx.showToast({ title: '登录成功', icon: 'success' })
      await this.refreshStatus()
    } catch (err) {
      wx.showToast({ title: err.message, icon: 'none', duration: 3000 })
    } finally {
      this.setData({ loggingIn: false })
    }
  },

  async onLogout() {
    try {
      await api.logout()
    } catch (err) {
      // 退出接口失败也清掉本地会话
    }
    this.refreshStatus()
  },
})
