import { createApp } from 'vue'
import App from './App.vue'
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
  createApp(App).mount('#app')
  await loadScript('/js/voice.js')
}

bootstrap()
