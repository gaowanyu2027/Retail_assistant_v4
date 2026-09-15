# 智能零售分析系统

一个面向门店场景的智能零售分析项目，整合了货架热度分析、异常行为告警、顾客表情分析、本地语音唤醒、语音问答、会话记录、MySQL 持久化和查询历史向量召回。

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
├── agents/                  # LangChain Agent + 模块注册表 + 百度地图工具
├── api/                     # FastAPI 路由
├── config/                  # 全局配置与 ROI + cameras.yaml
├── cv_engine/               # YOLO、跟踪、表情、ROI
├── skills/                  # 热度、告警、表情 + 客流/空货架模块
├── frontend/                # 前端页面
├── data/                    # SQLite、视频、日志
├── miniprogram/             # 微信小程序端（问答/看板/摄像头管理）
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

> 前端默认使用 **Vue 构建版**（`frontend-vue/dist`）。启动时若构建产物缺失或不完整，服务端会在终端打印告警并自动回退为**原生 JS 版**（`frontend/`）；运行期产物被破坏也会自动回退。浏览器端若 Vue 加载失败（资源缺失/挂载超时/未处理异常），页面会自动跳转原生版，并在终端输出回退原因。访问 `http://localhost:8000/?vue=0` 可手动强制原生版。

启动后终端应看到：

```text
[OK] 数据写入: MySQL Retail_assistant
[OK] 查询历史向量索引完成: N 条
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
