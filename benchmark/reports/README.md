# 压测与路由等价报告索引

本目录保存 `benchmark/loadtest.py`、`benchmark/route_parity.py` 与
`benchmark/ingest_parity.py` 的原始输出。
除早期 v3 基线外，均为**本机环回环境**（`127.0.0.1`）的测量；QPS/延迟绝对值受开发机
当时负载影响，只用于同一时段内的相对对照。文件名中的时间戳为报告生成时间。

## 路由等价（route_parity）

`direct` / `gate` 分别对应直连 Python 与经 Go edge；探针按匿名/已登录两种身份各跑一遍。

| 报告 | 拓扑 | 探针组数 | 差异 |
|---|---|---|---|
| `route_parity_20261015_224838.json` | S1：8001 直连 vs 8000 edge | 115 × 2 = 230 | 0 |
| `route_parity_20261016_193419.json` | S2：8013 直连 vs 8014 edge（`EDGE_AUTH=1`） | 118 × 2 = 236 | 0 |
| `route_parity_20261017_111727.json` | S3：8013 直连 vs 8014 edge（视频接管验收） | 122 × 2 = 244 | 0 |
| `route_parity_20261017_115328.json` | S3：同上（复跑） | 122 × 2 = 244 | 0 |

## 入库等价（ingest_parity）

`ingest_parity_<label>_<时间戳>.json` 是一轮运行的快照（三张视频派生表的归一化行），
`ingest_parity_diff_<时间戳>.json` 是两份快照的对账报告。对比口径见
`benchmark/ingest_parity.py` 的模块说明：剔除运行相关字段、`retail_stats` 按 zone
折叠到最新分钟桶、数值走显式容差。

| 报告 | 写者 | 差异 |
|---|---|---|
| `ingest_parity_python_20261017_163038.json` / `ingest_parity_go_20261017_163117.json` → `ingest_parity_diff_20261017_163126.json` | 真实视频两轮（`data/sources/demo_loop.mp4`，150 帧；Python 直写 vs `GO_OWNS_INGEST_WRITES=1`） | `retail_stats` 3 vs 3，容差 0.2 下 **0**；`alert_record` / `track_visit_paths` 两轮均为 0 行 |
| `ingest_parity_replay_python_20261017_163230.json` / `ingest_parity_replay_go_20261017_163230.json` → `ingest_parity_diff_replay_20261017_163230.json` | 同一份 `kind=4` 载荷：Python `apply_local` 直写 vs 经假上行流交给真实 edge 写入 | `retail_stats` 2 / `alert_record` 1 / `track_visit_paths` 1，容差 **0** 下 **0** |

说明：

- 两轮视频快照的时间窗按 **MySQL 时钟**（`SELECT NOW()`）取且互不重叠。本机 MySQL 为 UTC、
  应用为 UTC+8，而 `retail_stats.created_at` 与 `track_visit_paths.recorded_at` 由数据库
  `CURRENT_TIMESTAMP` 生成——按墙钟取窗会差 8 小时、查不到行。
- 真实视频轮次中 `alert_record` 与 `track_visit_paths` 两轮都为空，原因是片段内每条轨迹只
  经过 1 个区域（动线要求 `min_zones=2`），且默认 `FRAME_SKIP=1` 时 `cleanup()` 不会被调用、
  没有轨迹退役（详见 `改进记录.md` 模块 D 续9）。两张表的写入等价性由回放轮覆盖。
- 回放轮的载荷包含 2 个区域、1 条告警、1 条跨 2 区域动线，`session_id` 用 140 个字符验证
  128 字符截断；Go 侧经真实 edge 二进制的 `internal/store` 落库，两边容差 0 逐字段相等。

## 并发压测（loadtest）

| 报告 | 场景 | 结果 |
|---|---|---|
| `loadtest_20261016_1441{52,219,245,312}.md` | S1 正式 A/B：c100×1000，直连/edge 交替四轮 | 32.3 → 37.8 → 38.4 → **39.0 QPS**，错误 0% |
| `loadtest_20261017_1056*.md`、`1057*.md` | S3 验收期间：c50×400，同参数重复轮次 | 38.6–40.8 QPS，错误 0% |
| `loadtest_20261017_1116*.md`、`1122*.md` | S3 验收期间：c50×400（含无视频基线与视频会话中） | 文档引用的一对：`112258` 无视频基线 35.8 QPS → `112308` 视频会话中 **43.7 QPS**；其余为同参数轮次 |
| `loadtest_20261017_165054.md` | S0 补跑：当前代码默认配置，c100×1000，`AUTH_ENABLED=0` | 34.8 QPS，0% 错误（模板轮同步写检查点） |
| `loadtest_20261017_170535.md`、`170703.md` | S0 补跑：`AGENT_REMEMBER_TEMPLATE_TURNS=0`，c100×1000 | **200.2 / 187.4 QPS**，均高于 166.9 基线；0% 错误 |
| `loadtest_20261017_165335.md`、`170526.md` | S0 补跑：同一开关的 c1 单请求对照 | 37.7ms / 26.5 QPS ↔ 23.0ms / **43.4 QPS** |
| `loadtest_20261015_2257*.md`、`2300*.md` | S1 验收前的轮次（受 langfuse 线程泄漏影响） | 10.7–29.6 QPS；**不作正式对照** |
| `loadtest_20261016_1238*.md`、`124332`、`132330` | 定位 langfuse 线程泄漏的探针轮次 | 波动大；**不作正式对照**（见 `改进记录.md` 模块 D 续5） |
| `loadtest_20260819_*.md` | v3 基线（4 份） | 见 `并发压测报告_方法与结果.md` |

`loadtest_20260910_134134.md` 因机器同时运行多个容器、数据不具可比性，按 `.gitignore` 排除，不在仓库内。
