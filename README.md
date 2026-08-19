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
├── agents/                  # LangChain Agent
├── api/                     # FastAPI 路由
├── config/                  # 全局配置与 ROI
├── cv_engine/               # YOLO、跟踪、表情、ROI
├── skills/                  # 热度、告警、表情技能
├── frontend/                # 前端页面
├── data/                    # SQLite、视频、日志
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

#### DeepSeek API Key

```powershell
$env:dazuoye_api = "你的DeepSeek API Key"
```

#### MySQL

系统会自动创建 `Retail_assistant` 数据库，需要提供可建库的 MySQL 账号：

```powershell
$env:mysql_root = "你的MySQL密码"
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "root"
```

如果没有配置 `mysql_root`，系统会回退到 SQLite，不会自动写 MySQL。

#### Ollama（向量召回可选）

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
