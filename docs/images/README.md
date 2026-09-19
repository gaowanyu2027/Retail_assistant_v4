# 界面/证据图 —— 拍摄清单（README 首屏用）

README 里「界面预览」那一节已经写好，**目前是注释状态**（避免图片缺失时显示裂图）。
你按下面拍 6 张、放进本目录，然后把 README 里 `===== 界面预览` 那行的 `<!--` 与结尾 `===== -->` 删掉即可。

> ⚠ **定位提醒**：这个作品按 **AI Agent 开发** 讲，所以截图顺序也按"Agent 证据优先"排 ——
> 前 3 张是**能证明工程能力**的（问答质量、评测门禁、CI），后 3 张才是产品界面。
> 招聘方 30 秒能扫到的就是这 6 张。

## 拍摄前准备

```powershell
python run.py                 # 或 docker compose up -d
# 登录 http://127.0.0.1:8000（忘了口令：python tools/reset_password.py）
```

## 六张图

| 文件名 | 拍什么 | 画面里必须能看到 | 怎么造 |
|---|---|---|---|
| `01-chat.png` | **自然语言问答** | 提问原文 + 回答 + **数据可信度提示**（例如"当前数据未采集/不可信"）——**这是全项目最该展示的一张**（证明 Agent 不会编结论） | 先**不**开摄像头，直接问「今天客流怎么样？」→ 回答里会出现可信度提示 ✓ |
| `02-eval.png` | **85 条断言式评测跑通** | 终端里的评测输出（通过数/用例 id 列表） | `python evals/run_evals.py`（需 MySQL + LLM Key） |
| `03-ci.png` | **CI 双门禁** | GitHub Actions 页面（`unit` + `eval` 两个 job 都绿）或终端 `python tests/run_tests.py` 的 `PASS 161/161` | 推代码后打开仓库 Actions 页；或本地跑单测截图 |
| `04-monitor.png` | 实时监控（感知层） | 画面 + **FPS ~30** + 活跃轨迹 + 总访客 | 打开「服务器摄像头」（有行人的 30 秒循环视频） |
| `05-dashboard.png` | 经营看板 | 货架热度排行（有数字）+ 告警列表 | 同上，播一会儿让数据积累 |
| `06-metrics.png` | **LLM 用量/成本指标** | JSON 里的 `calls` / `tokens` / `latency` / `by_tag` | 见下方命令 |

### `06-metrics.png` 怎么拍

```powershell
# 取一个会话令牌（浏览器 F12 → Application → Cookies 里找 retail_sid，或直接用它）
curl.exe -H "Authorization: Bearer <会话令牌>" http://127.0.0.1:8000/api/metrics/llm
```

（把返回的 JSON 截下来即可；想更好看可以贴进编辑器截图。若先问几句问题再取，`by_tag` 里就有 `answer` 记录。）

## 拍摄要求

- 窗口宽度 **1280-1600px**；PNG；单张 < 400KB（超了压一下或存 JPG）
- 六张**风格一致**（同一浏览器、同一缩放、同一主题）
- **务必检查敏感信息**：右上角用户名、真实金额/店名、`.env` 里的 Key、内网 IP
- 终端截图把**命令行里的密码/Key 打码**（例如 `--password '***'`）

## 可选

- **架构图**：README 里已用 mermaid 画好（GitHub 直接渲染 ✓），不需要图片。
  要放进简历 PDF（有些工具不渲染 mermaid）可以：VS Code 装 mermaid 插件导出 PNG，
  或直接截 GitHub 渲染出来的图。
