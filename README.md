# 智能零售分析系统 v4

面向线下门店的 AI Agent 落地系统：以摄像头为感知入口，把 CV 检测结果结构化落库，
再由 LangGraph Agent 完成自然语言问答与运营归因，最终通过**网页与微信小程序双端**交付。

核心主张是**先保证数据可信、再给出结论**——设备故障与"真实零客流"被明确区分，
销量来源（真实接入 / 演示 / 测试）显式标注，避免用不可信数据指导经营决策。

> **演进记录见 [`改进记录.md`](改进记录.md)**：v3 → v4 的全部改进按「问题 → 做法 → 验证 → 收益」记录，
> 含实测验证数字、设计取舍与踩坑复盘。

### v4 相对 v3 的主要变化

| 维度 | v3 | v4 |
|---|---|---|
| 鉴权 | 完全开放 | 账号密码登录 + root/platform 双角色 + 限流 + 一次性票据 |
| 端 | 仅网页 | 网页 + **微信小程序**（各自适配鉴权） |
| Agent | 8 个工具、函数式汇报 | **15 个工具** + **LangGraph StateGraph** 汇报工作流 |
| 数据可信 | 无 | **可信度门禁** + **来源显式标注**（pos/simulated/test） |
| 业务能力 | 仅当前统计 | **同期对比**（昨天/上周）+ 销量接入与**目录自动同步** |
| CV | 单路 | **多摄像头模块化**（按标签加载、数据隔离、多路真并行） |
| 质量 | 手动跑评测 | 80 条评测集 + **CI 门禁** + 压测驱动优化（QPS 6×） |

## 项目介绍

系统主要包含以下能力：

- 零售视频分析：YOLO 行人检测 + ByteTrack 跟踪 + ROI 货架热度统计
- 异常行为分析：轨迹异常、绕过收银台、人群聚集等告警
- 人脸表情分析：入口摄像头人脸检测 + 表情识别 + SQLite/MySQL 记录
- 本地语音唤醒：sherpa-onnx KWS，支持“小零”等唤醒词
- 本地指令识别：唤醒后使用 sherpa-onnx 流式 ASR 识别指令
- 语音回复：edge-tts 服务端 TTS，手机端也可播放
- 自然语言问答：LangChain Agent + DeepSeek，支持流式回答
- 会话记录：新建、保存、切换、重命名、删除会话
- MySQL 持久化：表情、问答历史、会话、语音日志、TTS 缓存、视频记录、ROI 配置
- 向量召回：Qdrant 本地模式 + Ollama bge-small-zh-v1.5

## 技术栈

- Python 3.13
- FastAPI + Uvicorn + WebSocket
- Ultralytics YOLO26n + ByteTrack
- PyTorch + OpenCV
- sherpa-onnx：KWS 唤醒 + 流式中文 ASR
- edge-tts：服务端中文语音合成
- LangChain + DeepSeek：Agent 问答
- MySQL：业务数据持久化
- Qdrant + Ollama：查询历史向量召回
- HTML/CSS/JS：前端界面

## 目录结构

```text
.
├── agents/                  # LangChain Agent + 模块注册表 + 百度地图工具 + 数据可信度/同期对比
├── api/                     # FastAPI 路由 + 鉴权（security.py / routes/auth.py）
├── config/                  # 全局配置 + ROI + cameras.yaml
├── cv_engine/               # YOLO、跟踪、表情、ROI、多路视频引擎
├── skills/                  # 热度、告警、表情 + 客流/空货架模块
├── frontend-vue/            # 浏览器前端（Vue3 + Vite，唯一前端）
├── miniprogram/             # 微信小程序端（问答/看板/监控/摄像头管理/语音）
├── evals/                   # 自动化评测集（80 条）+ CI 门禁
├── benchmark/               # 并发压测脚本与报告
├── data/                    # 运行时数据：SQLite、鉴权库、销量投递目录、日志
├── 改进记录.md               # v3 → v4 改进记录（问题/做法/验证/收益）
├── all_models/              # sherpa-onnx 语音模型
├── sherpa-onnx-kws-*        # KWS 唤醒词模型
├── yolo26n.pt               # YOLO 小模型
├── best.pt                  # 人脸检测模型
├── mobilenetv3_fer_best.pth # 表情识别模型
├── mysql_db.py              # MySQL 数据层
├── vector_memory.py         # Qdrant 向量召回
├── requirements.txt
└── run.py
```

> 注：`frontend/`（原生 JS 版前端）已在 v4 移除，Vue 为唯一浏览器前端。

## 环境安装

### 1. 创建 Python 环境

推荐使用 Conda：

```powershell
conda create -n py313 python=3.13 -y
conda activate py313
```

### 2. 安装依赖

在项目根目录执行：

```powershell
pip install -r requirements.txt
```

如果缺少 PyMySQL、qdrant-client、edge-tts，也可以单独安装：

```powershell
pip install PyMySQL qdrant-client edge-tts
```

### 3. 配置环境变量

系统从环境变量读取密钥与配置（**所有值均不在代码/仓库中硬编码**）。下面是完整清单，标注了**必选 / 可选**。

> 提示：可用 Windows 用户环境变量（`$env:xxx`）或 `.env` 文件。若用 `.env`，请在项目根目录创建且**不要提交**（`gitignore` 已忽略 `.env`）；模板见下方 `.env.example`。

#### 必选（核心功能启动需要）

| 变量 | 用途 | 示例 |
|---|---|---|
| `dazuoye_api` | **DeepSeek API Key**（Agent 问答、LLM 调用） | `sk-xxxx` |
| `mysql_root` | **MySQL root 密码**（会话/表情/问答持久化） | `你的密码` |
| `baidu_map_ak` | 百度地图**服务端 AK**（竞品/商圈/地理编码/距离） | `百度AK` |
| `baidu_map_sk` | 百度地图**服务端 SK**（sn 签名，检索/矩阵必需） | `百度SK` |

```powershell
$env:dazuoye_api    = "你的DeepSeek API Key"
$env:mysql_root     = "你的MySQL密码"
$env:baidu_map_ak   = "你的百度地图AK"
$env:baidu_map_sk   = "你的百度地图SK"
```

> **没有配置 `mysql_root` 时**：系统自动回退到 SQLite（`data/agent_checkpoints.db`），不会写 MySQL，启动日志会提示。

#### 可选（增强/切换功能）

| 变量 | 用途 | 默认值 | 说明 |
|---|---|---|---|
| `MYSQL_HOST` | MySQL 主机 | `127.0.0.1` | 远程 MySQL 时改 |
| `MYSQL_PORT` | MySQL 端口 | `3306` | |
| `MYSQL_USER` | MySQL 用户 | `root` | |
| `SANITIZE_FACES` | 人脸脱敏开关 | `0`（关） | 设 `1` 启用推帧/展示时对人脸打码（合规） |
| `QDRANT_URL` | Qdrant Server 地址 | 空（嵌入式） | 设置后启用 **Server 模式**（Docker/多进程/多机） |
| `QDRANT_API_KEY` | Qdrant Server 密钥 | 空 | 云托管/带认证时填 |
| `QDRANT_PATH` | 本地嵌入向量库目录 | `qdrant_data/` | 仅嵌入式模式用 |
| `SHERPA_ONNX_PROVIDER` | 本地语音推理设备 | `cpu` | 有 CUDA 可设 `cuda` |
| `BAIDU_MCP_ENABLED` | 百度官方 MCP 叠加 | 空 | 设 `1` 启用 14 个通用地图工具 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 可观测 Public Key | 空 | 两个 key 齐了才启用 Trace |
| `LANGFUSE_SECRET_KEY` | Langfuse 可观测 Secret Key | 空 | 同上 |

```powershell
# 可选：启用功能时再设（不设则用默认值）
$env:SANITIZE_FACES         = "1"     # 人脸脱敏
$env:SHERPA_ONNX_PROVIDER   = "cuda"  # 本地语音用 GPU
$env:BAIDU_MCP_ENABLED      = "1"     # 百度 MCP
```

#### `.env.example` 模板（可复制使用）

> 复制为 `.env` 并填入真实值，**不要提交 `.env`**。

```ini
# ===== 必选 =====
dazuoye_api=你的DeepSeek API Key
mysql_root=你的MySQL密码
baidu_map_ak=你的百度地图AK
baidu_map_sk=你的百度地图SK

# ===== 可选 =====
# MYSQL_HOST=127.0.0.1
# MYSQL_PORT=3306
# MYSQL_USER=root
# SANITIZE_FACES=1
# QDRANT_URL=http://localhost:6333
# QDRANT_API_KEY=
# QDRANT_PATH=qdrant_data/
# SHERPA_ONNX_PROVIDER=cpu
# BAIDU_MCP_ENABLED=1
# LANGFUSE_PUBLIC_KEY=
# LANGFUSE_SECRET_KEY=
```

#### Ollama（可选，向量召回用）

启动 Ollama，并安装中文向量模型：

```powershell
ollama pull qllama/bge-small-zh-v1.5
```

默认向量库目录为项目下的 `qdrant_data/`，可通过环境变量修改：

```powershell
$env:QDRANT_PATH = "D:/你的目录/qdrant_data"
```

## 模型下载指引

### YOLO26n

项目推荐使用 `yolo26n.pt`。

如果项目根目录没有该文件，可以执行：

```powershell
python -c "from ultralytics import YOLO; YOLO('yolo26n.pt')"
```

也可以从其他已有项目复制到项目根目录（例如本地已下载的 yolo26n.pt 文件）。

### 人脸检测模型

项目需要：

```text
best.pt
```

该模型是 YOLOv8n-face 转换或导出的权重，请放在项目根目录。

### 表情识别模型

项目需要：

```text
mobilenetv3_fer_best.pth
```

请放在项目根目录。

### KWS 唤醒词模型

从 ModelScope 下载：

```powershell
git lfs install
git clone https://www.modelscope.cn/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.git
```

克隆后放到项目根目录：

```text
sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
```

目录内需要包含：

```text
tokens.txt
encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
decoder-epoch-12-avg-2-chunk-16-left-64.onnx
joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx
keywords.txt
```

### 流式中文 ASR 模型

项目使用：

```text
all_models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23
```

如果该目录不存在，可以从 HuggingFace 下载：

```text
https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23
```

下载后解压到：

```text
all_models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23
```

目录内需要包含：

```text
tokens.txt
encoder-epoch-99-avg-1.int8.onnx
decoder-epoch-99-avg-1.int8.onnx
joiner-epoch-99-avg-1.int8.onnx
```

### Ollama 向量模型

```powershell
ollama pull qllama/bge-small-zh-v1.5
```

模型名：`qllama/bge-small-zh-v1.5`，向量维度 512。

## 启动项目

```powershell
conda activate py313
$env:dazuoye_api = "你的DeepSeek API Key"
$env:mysql_root = "你的MySQL密码"
python run.py --port 8000
```

访问：

- 前端页面：http://localhost:8000
- API 文档：http://localhost:8000/docs

> 前端为 **Vue 构建版**（`frontend-vue/dist`，v4 起为唯一浏览器前端）。若构建产物缺失或不完整，
> 服务端会在终端打印告警并显示"前端未构建"提示页；此时执行 `cd frontend-vue && npx vite build` 重新构建即可。
> 浏览器端若 Vue 加载失败（资源缺失/挂载超时/未处理异常），页面会提示刷新并在终端输出原因。
>
> **登录**：v4 起 `/api/*` 默认需要登录。首次启动时终端会打印自动创建的 root 账号与随机密码
> （也可用环境变量 `AUTH_ROOT_PASSWORD` 预先指定）。

启动后终端应看到：

```text
[OK] 数据写入: MySQL Retail_assistant
[OK] 查询历史向量索引完成: N 条
```

## 容器化运行（Docker Compose）

除上面的本地直跑方式外，也可用 Docker 一键拉起**业务侧**全套服务。

### 前置

```powershell
copy .env.example .env      # 填入 dazuoye_api / mysql_root 等（.env 已被 gitignore）
```

### 启动

```powershell
docker compose up -d --build
docker compose logs -f backend     # 观察启动日志（首次会打印 root 账号与随机密码）
```

访问 **http://localhost:8000**。停止：`docker compose down`（数据在命名卷里，不会丢）。

### 编排内容

| 服务 | 镜像 | 宿主端口 | 说明 |
|---|---|---|---|
| `backend` | 本项目（多阶段构建） | **8000** | FastAPI + Agent + 前端静态资源 |
| `mysql` | mysql:8.0 | 不发布 | 业务数据（`depends_on` + healthcheck 确保就绪后再起后端） |
| `qdrant` | qdrant/qdrant | 不发布 | 向量检索（Server 模式，支持多进程） |
| `redis` | redis:7-alpine | 不发布 | **常驻**（本项目的缓存层）。代码当前尚未读写 Redis，见下方「Redis 定位」 |

> 只有 `backend` 对外发布端口。MySQL/Qdrant/Redis 仅在 compose 网络内被 backend 通过
> 服务名访问，**不暴露到宿主机**——既避免端口冲突，也少一个攻击面。
> 需要用 GUI 客户端连库时，取消 `docker-compose.yml` 里对应 `ports` 的注释即可（已限 `127.0.0.1`）。

### 统一入口：一条命令管起全部环境

本项目只依赖 compose 编排的四个服务；但**可观测平台 Langfuse 是独立的一套 compose 栈**
（在 `D:\langfuse\`，有自己的 `.env` 与 postgres 卷）。为此提供一个包装脚本：

```powershell
.\stack.ps1 up       # 启动全部（本项目 + Langfuse）
.\stack.ps1 ps       # 查看全部状态
.\stack.ps1 stop     # 停止全部（保留容器）
.\stack.ps1 down     # 停止并删除容器（卷保留，数据不丢）
.\stack.ps1 logs backend
```

只想操作本项目时，照旧直接用 `docker compose`（行为与以前完全一致）。

> ⚠ **为什么用脚本而不是 compose 的 `include:` 把两个文件合并**
>
> 实测 `include:` 会把被包含文件的服务**并入父项目**，后果很严重：
> 顶层 `name` 变成父目录名（本项目自己的 `name: retail-assistant` 被忽略）、
> 生成一整套**重复容器**与现有容器抢 8000 端口，且 Langfuse 的 postgres 卷名会从
> `langfuse_postgres_data` 变成 `<父项目名>_postgres_data`——**等于换库，
> Langfuse 的历史 trace 会全部"消失"**（其实还在旧卷里，但新容器读不到）。
>
> 用 `-f <各自的文件>` 逐个调用时，每个栈都保持自己的项目名与卷名
> （已验证：Langfuse → `name: langfuse`、卷 `langfuse_postgres_data`），互不干扰。

### Redis 定位（缓存层：先量再缓存）

Redis 已作为**常驻服务**纳入 compose（`--maxmemory 256mb --maxmemory-policy allkeys-lru`，
容量上限与 LRU 淘汰都已设，避免把宿主机内存吃光），但**代码当前还没有读写它**。

**建议按此顺序接入，并且先用 `benchmark/loadtest.py` 量出 MySQL 实际负载再决定缓存什么**，
否则会变成"因为零售系统都该有 Redis"的装饰品：

| 优先 | 用途 | 收益 |
|---|---|---|
| ① | **地图工具缓存**：现有 `_MAP_CACHE` 是进程内 dict，满 500 条时 `clear()` 一次性全清（缓存雪崩） | Redis 的 per-key TTL 天然修掉，且**直接省百度地图 API 配额** |
| ② | **热度/销量聚合查询**：前端每 5 秒轮询多个接口，每个都打 MySQL 聚合 | TTL 5~30 秒即可砍掉绝大部分重复查询 |
| ③ | **embedding 缓存**：`hash(text) -> vector` | 省掉每次语义闸/向量召回的 Ollama 往返 |

**不适合放进 Redis 的（别搬）**：

- **登录会话与限流**：刻意存在独立的 SQLite 鉴权库，保证**业务库故障时管理员仍能登录**；
  搬进 Redis 会削弱该隔离，且 Redis 重启会丢掉限流状态（暴力破解窗口重开）
- **实时视频帧 / `_active_camera`**：高频且体积大，进程内比走网络快


### 四个设计说明

**① 前端在镜像内构建（多阶段）**
`frontend-vue/dist` 被 `.gitignore` 忽略，别人 clone 后没有前端产物。因此 Dockerfile 用
node 阶段执行 `npm ci && npm run build`，再 COPY 进 Python 运行阶段——
**使用者在宿主机无需安装 Node**，`docker build` 一步到位。

**② 模型与数据用挂载，不打进镜像**
`yolo26n.pt` / `best.pt` / `mobilenetv3_fer_best.pth` / `all_models/` / KWS 模型
以只读卷挂载，`data/`（含鉴权库、销量投递目录）为可写挂载。这样镜像更小，也符合
"模型权重不进镜像"的做法。

**③ 摄像头与 GPU 留在边缘（本地）**
本编排覆盖的是"**云端/业务侧**"——业务 API、Agent 问答、数据存储。
容器内直通摄像头/GPU 在 Windows 上尤其别扭，且原始视频流不该走公网，因此建议：

```
边缘（本地/店内）:  摄像头 → YOLO 推理 → 结构化结果
                                    ↓（只传几 KB 的 JSON）
云端（本编排）:    业务 API + Agent + MySQL/Qdrant + 多端展示
```

容器内没有视频源时，**数据可信度门禁**会明确提示"视频源未启动，数据不可信"，
而不会把 0 当作业务结论——这正好可以通过 `GET /api/reports/data-quality` 观察到。

**④ 密钥不进镜像（`.dockerignore` 挡住 `.env`）**
Dockerfile 有 `COPY . .`，会把它能看到的**一切**烤进镜像层。若 `.env` 被拷进去，
任何人 `docker run --rm -it <镜像> cat /app/.env` 就能拿到 API Key 与数据库口令，
而且该层会**永久留在镜像历史里**（即使后续删除文件也仍可挖出）。
因此 `.dockerignore` 显式排除 `.env` / `.env.*` / `*.pem` / `*.key`——
环境变量一律由 `env_file` 在**运行时**注入。这与 `.gitignore` 的规则是两套独立机制，
**只加 `.gitignore` 并不防 Docker**，容易漏。

### 从 `docker run` 迁移过来（重要）

若宿主机上已有以 `docker run` 启动的 MySQL/Qdrant，**必须先停掉再 `docker compose up`**：

```powershell
docker ps                       # 确认容器名，例如 mysql-main / qdrant
docker stop mysql-main qdrant
```

原因是**共享卷**：本编排复用同名卷（`mysql8_volume` / `qdrant-docker`），
而**同一个卷被两个 MySQL 同时挂载会损坏数据文件**。
（本编排不发布 3306/6333，所以这里不是端口冲突问题。）

复用卷的好处是**数据完整保留**，不用重新导。谨慎起见先备份：

```powershell
docker exec mysql-main mysqldump -uroot -p retail_assistant > backup.sql
```

## 功能使用

### 视频分析

点击顶部“摄像头”，可选择：

- 服务器摄像头：服务端 OpenCV 采集
- 本机摄像头：浏览器 getUserMedia 采集

摄像头启动后，按钮会变为“切换摄像头”。

### 语音助手

1. 点击顶部“语音输入”
2. 允许麦克风权限
3. 说“小零”
4. 系统回复“我在”
5. 在 5 秒内说“打开摄像头”

语音回复默认使用 edge-tts，需要服务端能访问外网。

### 自然语言问答

在页面底部输入问题，例如：

```text
哪个货架最受欢迎？
今天有没有异常告警？
顾客情绪怎么样？
```

### 会话记录

点击左侧“会话记录”按钮展开：

- 新建会话
- 保存会话
- 重命名会话
- 删除会话
- 搜索历史问答内容

会话内容和向量召回都保存在 MySQL 和 Qdrant 中。

## 常见问题

### 没有写入 MySQL

检查启动终端是否配置了 `mysql_root`，以及启动日志是否显示：

```text
[OK] 数据写入: MySQL Retail_assistant
```

如果显示“回退到 SQLite”，说明 MySQL 环境变量没有生效。

### 语音没有回复

- 手机端必须使用 HTTPS
- 确认浏览器允许麦克风
- 确认服务端可以访问 edge-tts 外网
- 手机浏览器可能需要在页面上先点击一次“语音输入”解锁 AudioContext

### 向量搜索失败

确认 Ollama 已启动，并已安装：

```powershell
ollama pull qllama/bge-small-zh-v1.5
```

如果本地 Qdrant 被另一个 Python 进程占用，需要先停止旧服务再启动新服务。

## 备注

本项目默认使用单进程运行。本地 Qdrant 模式不支持多个 Python 进程同时打开同一个向量库目录；如果后续使用多进程或多机部署，需要切换为 Qdrant Server 模式。

## 摄像头模块化（按需加载）

多摄像头按"标签 + 配置"加载分析模块，每镜头独立数据隔离：

- `config/cameras.yaml`：每个摄像头的 `type`(标签) 决定**候选模块池**，`modules` 决定**实际加载**
- `agents/analytics_module.py`：模块抽象 + 工厂注册表 + 标签候选池
- `agents/module_registry.py`：`ModuleRegistry`——按配置实例化模块、运行时增删/开关、喂数据(数据隔离)
- `config/cameras.yaml` 示例：店内镜头装 `[shelf_heat, anomaly_detect, empty_shelf]`，门口装 `[footfall]`，收银装 `[emotion_experience]`

**接口**（`/api/cameras`）：
- `GET /cameras` 列表 | `GET /cameras/scan` 识别 | `GET /cameras/modules` 候选池
- `POST /cameras/{id}/modules` 运行时加载模块 | `DELETE .../{mod}` 卸载 | `PUT .../{mod}/enabled` 开关
- `POST /cameras/active` 视频绑定 | `GET /cameras/{id}/modules/{mod}/stats` 模块统计

前端小程序"摄像头管理"设置页可**识别/添加摄像头 + 勾选模块 + 绑定视频**（显性按钮，不改代码、不重启）。

## 百度地图接入

竞品/商圈/地理编码/距离测算，WebAPI 定制 + MCP 可选：

- `agents/map_tools.py`：4 个业务工具（`check_competitors` / `analyze_surrounding` / `batch_geocode` / `calc_distances`），服务端 AK + SW 签名
- `agents/mcp_maps.py`：百度官方 MCP（14 个通用地图工具），设 `BAIDU_MCP_ENABLED=1` 叠加
- 环境变量：`baidu_map_ak` / `baidu_map_sk`
- 接口：`/api/maps/geocode` / `competitors` / `surrounding` / `distance`

Agent 可回答："我门店周边竞争如何"→ 返回竞品数量/分布/商圈潜力 + 业务建议。
