# 智能零售分析 — 小程序端（阶段一）

微信小程序壳：**AI 问答页 + 数据看板页**，对接现有 FastAPI 后端（`/api/queries`、`/api/reports/*`、`/api/analytics/*`）。
接口与网页端**完全共用同一套**（不按端分路由），差异只在传输适配：小程序用 `Authorization: Bearer` 代替 Cookie。

## 目录结构

```
miniprogram/
├── app.js                  # 全局配置（apiBase 在这里改）
├── app.json                # 页面 + tabBar（问答/看板）
├── project.config.json     # 开发者工具配置（urlCheck=false 免域名校验）
├── utils/api.js            # wx.request 封装
└── pages/
    ├── index/              # 问答页：调 POST /api/queries（模板毫秒级 + Agent 分析）
    └── dashboard/          # 看板页：热度排行 + 告警汇总 + 转化率四象限 + 定期汇报
```

## 本地演示（3 步）

1. **启动后端**：项目根目录 `python run.py`（0.0.0.0:8000）
2. **导入小程序**：微信开发者工具 → 导入项目 → 选择本 `miniprogram/` 目录（AppID 用测试号即可）
3. **勾选免域名校验**（project.config.json 已配 `urlCheck:false`，如不生效）：
   详情 → 本地设置 → 勾选"不校验合法域名…"

模拟器里即可问答 + 看板。

## 真机预览

1. 手机与电脑同一 Wi-Fi
2. 查电脑局域网 IP：`ipconfig`（如 192.168.1.100）
3. 改 `app.js` 的 `apiBase` 为 `http://192.168.1.100:8000/api`
4. 开发者工具 → 预览 → 扫码 → 手机右上角菜单 → 打开"开发调试"（跳过域名校验）
5. Windows 防火墙放行 8000 端口

## 登录（必读）

后端**默认封启**：`/api/*` 一律需要登录，小程序必须登录后才能用。

与浏览器端的差异（这是唯一需要额外处理的地方）：

| | 浏览器端 | 小程序端 |
|---|---|---|
| 普通请求 | HttpOnly Cookie（自动） | **`Authorization: Bearer <token>`**（`wx.request` 不携带 Cookie） |
| WebSocket / 音频 URL | Cookie 自动带上，URL 里**无凭据** | **一次性票据 `?ticket=xxx`**（见下） |

**为什么 WS / 音频不用会话令牌**：这两类传输无法可靠自定义请求头（浏览器 WebSocket API
根本不支持；小程序虽支持 `wx.connectSocket` 的 header，但音频 `src` 同样不支持，且小程序
没有 Cookie jar）。若把**会话令牌**放 URL，它会进入 uvicorn / 反向代理 / 内网穿透的
**访问日志**（CWE-598），等同于泄露。因此改用一次性票据：

```
POST /api/auth/ws-ticket      ← 用 Bearer 头正常鉴权（这一步安全，不经过 URL）
   → 票据（60 秒有效、用后即焚）
ws://host/api/ws/stream?ticket=xxx        ← URL 里只是一次性票据
http://host/api/tts?text=hi&ticket=xxx
```

**即使票据落入日志也已失效**（一次性 + 短时效）。后端**默认拒绝** URL 里的主会话令牌
（`?token=`）。票据一次性 → **每次连接（含重连）都要重新签发**。

实现位置：

- `app.js` — 登录态存储（`setAuth` / `clearAuth` / `isLoggedIn` / `hasPerm`）
- `utils/api.js` — 统一带 Bearer 头；`401 → 清登录态并回登录页`；`403 → 提示权限不足`；
  `getTicket()` 签发票据；`wsUrl(path, ticket)` / `ticketUrl(path, ticket)` 附带票据
- `pages/login/login` — 登录页（账号密码，无验证码/手机号/微信）
- `pages/video/video.js`、`pages/voiceask/voiceask.js` — 每次连接前先签发票据

首次使用：启动后端时终端会打印 root 账号与随机密码（或用环境变量 `AUTH_ROOT_PASSWORD` 预先指定）。
登录后用 `hasPerm('user:manage')` 可区分 root 与平台账户的能力差异。

> 注：正式版小程序要求 `request`/`socket` 域名必须 **HTTPS + ICP 备案**，花生壳二级域名通常无法备案；
> 开发阶段可在开发者工具勾选"不校验合法域名"。

## 演示话术（面试用）

- 问"1号货架现在客流怎么样" → 模板路由毫秒返回
- 问"为什么销量差？帮我分析一下" → Agent 归因分析（转化率/动线）
- 看板页下拉刷新 → 热度排行 + 四象限诊断 + 定期汇报

## 阶段二（待做）

- 视频监控页：`camera` 截帧上传 或 `wx.connectSocket` 接 `/api/ws/stream` 二进制推帧
- 语音问答：wx 录音 → `/api/asr` → 问答 → `/api/tts` 播放
- 正式上线：云服务器 Docker 部署 + HTTPS + 域名备案（小程序合法域名要求）
