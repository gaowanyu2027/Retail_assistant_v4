# contracts/ — 行为契约夹具（golden fixtures）

这些 JSON 是**冻结的行为契约快照**，由测试逐字比对当前实现：

| 文件 | 冻结什么 | 来源（唯一真源） | 比对测试 |
|---|---|---|---|
| `ws_messages.json` | WS 视频流协议：帧包头（4B 大端 frame_id）、元数据节拍、frame/source_status 消息结构、本机摄像头拒绝文案 | [ws_protocol.py](../ws_protocol.py) | `tests/test_ws_protocol_fixtures.py` |
| `permissions.json` | 角色三档（root/platform/viewer）与权限矩阵 | `api/security.py`（`roles` / `ROLE_LABELS` / `perms_of()`） | `tests/test_contract_fixtures.py` |
| `guard_cases.json` | 视频源守卫（SSRF/路径）绕过清单：输入 → 放行或拒绝（含错误码） | `video_sources.py` | `tests/test_contract_fixtures.py` |
| `auth_policy.json` | 鉴权策略与两种错误响应：免登录白名单、Cookie 名、Bearer 前缀、401（状态/类型/响应体逐字节）、404（`FastAPI` 默认渲染，实测导出） | `api/security.py`（`_PUBLIC_PATHS` / `needs_auth` / 未登录响应常量） | `tests/test_contract_fixtures.py`、`edge/internal/auth/policy_test.go` |

## 为什么有这层

数据面迁 Go（混合架构）后，协议字节、权限结论、守卫判定将由 Go 侧并行实现 ——
两侧对同一输入必须给出**逐字相同**的结果。夹具是比对基准，也是"契约没有悄悄漂移"的守卫：

- 改了实现没改夹具 → 测试红（强制让变更显式化）；
- 改了夹具没改实现 → 同样红。

`guard_cases.json` 里有两条 `error_in`（十进制/短写 IP）：共同契约是**必须拒绝**，
错误码随平台解析行为不同（Windows `getaddrinfo` 失败=`dns_failed`；Linux 可能解析成功
=`host_blocked`），跨平台跑测试时两者都算通过。

`auth_policy.json` 由两侧同时比对：Python 侧看常量，Go 侧（`edge/internal/auth`）看
`needs_auth` 的判定与 401 响应字节。Go 单测读不到本目录时**直接失败**（避免夹具缺失被
当成通过）；确要在仓库外单独跑 edge 时设 `EDGE_TEST_ALLOW_MISSING_FIXTURES=1` 跳过。

## 更新夹具（仅当有意变更行为时）

先改实现与测试，再重新导出，夹具 diff 即本次变更的显式说明：

```bash
python contracts/export_fixtures.py
```

日常**不要**运行它 —— 夹具漂移应当由测试暴露，而不是被脚本悄悄抹平。
