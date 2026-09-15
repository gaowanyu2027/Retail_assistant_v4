import { createApp } from 'vue'
import Root from './Root.vue'
import * as echarts from 'echarts'

window.echarts = echarts

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.onload = resolve
    script.onerror = reject
    document.body.appendChild(script)
  })
}

async function bootstrap() {
  await loadScript('/js/stream.js')
  await loadScript('/js/chart.js')
  // 挂 Root：由它做登录门禁，未登录不挂载仪表盘（voice.js 在进入仪表盘前加载）
  createApp(Root).mount('#app')
}

bootstrap()
