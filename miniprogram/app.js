App({
  onLaunch() {
    // 首次启动没有后端地址时引导到设置页 (tabBar 页只能 switchTab)
    const config = require('./utils/config')
    if (!config.getBaseUrl()) {
      wx.switchTab({ url: '/pages/settings/index' })
    }
  },
})
