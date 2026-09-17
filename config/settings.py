"""
全局配置模块 — 统一管理路径、设备、模型参数等
整合零售视频分析 + 门店人脸表情分析双系统
"""
import os
from pathlib import Path

# 项目根目录：代码中统一以此解析模型、数据、配置路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 配置文件目录
CONFIG_DIR = PROJECT_ROOT / "config"
# 数据目录：SQLite、日志、视频、快照等
DATA_DIR = PROJECT_ROOT / "data"


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载（零依赖）。

    支持：`KEY=VALUE`、`#` 注释、空行、值两侧的单/双引号。
    **已存在的真实环境变量优先**（不覆盖），便于临时用 `$env:` 覆盖 .env。
    """
    try:
        if not path.is_file():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:      # 真实环境变量优先
                os.environ[key] = val
    except Exception as e:                          # .env 有问题不应阻断启动
        print(f"[Config] .env 加载失败（已忽略）: {e}")


# 在任何 os.environ.get(...) 读取之前加载 .env（否则配置项读不到）
_load_dotenv(PROJECT_ROOT / ".env")

# ==================== 设备配置 ====================
try:
    import torch
    # 计算设备：有 CUDA 用 GPU，否则用 CPU
    DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
    # GPU 是否使用 FP16 加速
    USE_FP16 = True if DEVICE != "cpu" else False
except ImportError:
    DEVICE = "cpu"
    USE_FP16 = False

# ==================== 模型路径（相对路径，使用时需基于 PROJECT_ROOT 解析） ====================
# YOLO 行人检测/跟踪模型
YOLO_MODEL_PATH = "yolo26n.pt"
# 人脸检测模型
FACE_MODEL_PATH = "best.pt"
# 表情识别模型
EMOTION_MODEL_PATH = "mobilenetv3_fer_best.pth"

# ==================== 视频源配置 ====================
# 默认视频源（图片序列或视频文件）
DEFAULT_VIDEO_SOURCE = str(PROJECT_ROOT / "data" / "MOT16" / "train" / "MOT16-04" / "img1")
# 帧截图保存目录
FRAME_SAVE_DIR = str(DATA_DIR / "frames")
# 分析结果视频输出路径
OUTPUT_VIDEO_PATH = str(DATA_DIR / "tracking_result.mp4")

# ==================== ROI配置文件 ====================
# 服务器摄像头 ROI 配置
ROI_CONFIG_PATH = "config/roi_zones.yaml"
# 本地浏览器摄像头 ROI 配置
LOCAL_ROI_CONFIG_PATH = "config/roi_zones_local.yaml"
# 业务阈值配置
THRESHOLDS_CONFIG_PATH = "config/thresholds.yaml"

# ==================== DeepSORT 超参 ====================
# 轨迹最大丢失帧数（超过后删除轨迹）
MAX_AGE = 50
# 轨迹最少命中帧数
MIN_HITS = 3
# 目标关联 IOU 阈值
IOU_THRESH = 0.4
# 马氏距离阈值
MAHAL_THRESH = 9.4877
# 外观特征余弦相似度阈值
COS_THRESH = 0.15
# 特征库最大容量
FEAT_BANK_MAX = 150

# ==================== YOLO 检测参数 ====================
# 检测置信度阈值
YOLO_CONF = 0.25
# NMS IOU 阈值
YOLO_IOU = 0.45
# 推理输入尺寸
YOLO_IMGSZ = 640
# 行人类别 ID
PERSON_CLASS_ID = 0
# YOLO 跟踪器配置
YOLO_TRACKER = "bytetrack.yaml"

# ==================== ID回收池参数 ====================
# 轨迹最大丢失帧数
RECYCLE_MAX_LOST = 60
# 轨迹回收余弦相似度阈值
RECYCLE_COS_THRESH = 0.12

# ==================== 视频输出参数 ====================
# 基础/目标帧率
VIDEO_FPS = 30
# 自适应帧率下限
VIDEO_MIN_FPS = 5
# 自适应帧率上限
VIDEO_MAX_FPS = 30
# 摄像头请求帧率
VIDEO_CAMERA_FPS = 30
# 推送画面宽度（高度按原始宽高比计算）
VIDEO_OUTPUT_WIDTH = 1280
# 零售视频 JPEG 压缩质量
VIDEO_JPEG_QUALITY = 70
# 表情视频 JPEG 压缩质量
EMOTION_JPEG_QUALITY = 75
# 定期热度汇报间隔（秒）
HEAT_REPORT_INTERVAL_SECONDS = 600
# 服务器摄像头采集宽度
VIDEO_CAMERA_WIDTH = 1280
# 服务器摄像头采集高度
VIDEO_CAMERA_HEIGHT = 720
# 服务器摄像头采集编码
VIDEO_CAMERA_FOURCC = "MJPG"
# 摄像头内部缓冲区大小，越小延迟越低
VIDEO_CAMERA_BUFFER_SIZE = 1
# 零售视频每帧都检测/编码
FRAME_SKIP = 1
# 人脸表情识别间隔帧数
FACE_EMOTION_INTERVAL = 5
# 零售热度快照写入帧间隔
RETAIL_STATS_SAVE_FRAME_INTERVAL = 75
# WebSocket 接收命令超时（秒）
WS_RECEIVE_TIMEOUT_SECONDS = 0.005
# WebSocket 每帧推送后休眠间隔（秒）
WS_SEND_INTERVAL_SECONDS = 0.005
# WebSocket 空闲轮询休眠间隔（秒）
WS_POLL_SLEEP_SECONDS = 0.01
# JSON 元数据推送间隔帧数
WS_METADATA_INTERVAL = 5
# 服务端推送 FPS 统计打印间隔（秒）
WS_FPS_LOG_INTERVAL = 5
# 自适应帧率降帧系数
ADAPTIVE_FPS_SLOW_RATIO = 1.2
# 自适应帧率升帧系数
ADAPTIVE_FPS_FAST_RATIO = 0.6
# 自适应降帧倍率
ADAPTIVE_FPS_DOWN_FACTOR = 0.8
# 自适应升帧倍率
ADAPTIVE_FPS_UP_FACTOR = 1.1
# 客户端帧队列最大长度
CLIENT_FRAME_QUEUE_MAXLEN = 3
# 视频空闲等待/重试间隔（秒）
VIDEO_IDLE_SLEEP_SECONDS = 0.05
# 摄像头丢帧清理间隔
CAMERA_FRAME_DROP_INTERVAL = 200
# 每次清理时额外 grab 的帧数
CAMERA_FRAME_DROP_BATCH = 5
# Agent 向量召回数量
AGENT_VECTOR_RECALL_LIMIT = 3
# Agent 向量召回最低相似度
AGENT_VECTOR_MIN_SCORE = 0.45
# Agent 从 MySQL 加载的会话历史条数
AGENT_SESSION_HISTORY_LIMIT = 10
# Agent 内存历史最大条数
AGENT_MEMORY_HISTORY_MAX = 100
# Agent 清理后保留的历史条数
AGENT_MEMORY_HISTORY_KEEP = 50
# 自然语言查询会话缓存有效期（秒）
QUERY_SESSION_TTL = 1800

# ==================== Agent 长会话摘要压缩 / 长期记忆 ====================
# 摘要压缩触发阈值：检查点消息估算 token 超过该值×触发比例时执行压缩
AGENT_SUMMARY_MAX_TOKENS = 4000
# 触发比例（70% 阈值触发）
AGENT_SUMMARY_TRIGGER_RATIO = 0.7
# 压缩后保留的最近消息条数（早期消息被摘要替换）
AGENT_SUMMARY_KEEP_LAST = 6
# 生成的对话摘要最大字符数
AGENT_SUMMARY_MAX_CHARS = 600
# 新会话开始时注入的历史会话摘要条数（跨会话长期记忆）
AGENT_LONG_TERM_INJECT_LIMIT = 3
# 摘要关键词提取上限
AGENT_LONG_TERM_KEYWORD_LIMIT = 10

# ==================== Agent 存储清理策略 ====================
# 每个会话(thread)保留的最新检查点数量（LangGraph 恢复只用最新，旧的可安全清理）
AGENT_CHECKPOINT_KEEP_PER_THREAD = 20
# 长期记忆最多保留条数（超出删除最旧的）
AGENT_LONG_TERM_KEEP_RECORDS = 200
# 工具调用日志 / 语音指令日志保留天数
AGENT_LOG_KEEP_DAYS = 30
# 热度汇报保留条数
AGENT_HEAT_REPORT_KEEP_RECORDS = 500
# 会话内容搜索最大返回条数
CHAT_SEARCH_LIMIT = 50
# 会话向量搜索最大返回条数
CHAT_VECTOR_SEARCH_LIMIT = 20
# 向量生成文本最大长度（bge-small-zh 的 512 token 上限：中文 1 字符≈1 token，
# 实测 548 字符超限时 Ollama 返回 500，故保守取 500 留余量）
VECTOR_EMBED_MAX_CHARS = 500
# 向量搜索默认返回条数
VECTOR_SEARCH_DEFAULT_LIMIT = 10
# ---- 向量层熔断（与 db_engine 的 MySQL 熔断同思路）----
# 连续失败该次数后进入冷却窗口，窗口内**直接降级为关键词检索**，不再发起网络调用。
# 为什么必须做：Qdrant 停掉时实测并发 40 个向量检索 → 中位 86s、最大 120s，
# 且把**与向量无关**的接口（同样走 to_thread）拖到 52s —— 坏依赖吃光了线程池。
VECTOR_BREAKER_FAILS = int(os.environ.get("VECTOR_BREAKER_FAILS", "3"))
# 冷却时长（秒）：到期后放一次探测，成功即复位
VECTOR_BREAKER_COOLDOWN_SECONDS = float(
    os.environ.get("VECTOR_BREAKER_COOLDOWN_SECONDS", "15")
)
# ---- 向量层舱壁（bulkhead）：限制**同时**进入向量层的调用数 ----
# 为什么光有熔断不够（实测教训）：40 个请求在**同一瞬间**到达时，它们会在熔断
# 打开**之前**就全部通过入口守卫 —— 实测"熔断已生效"但 40 并发的中位延迟仍是
# 108s、旁路接口仍被拖到 78s。熔断管的是"后续请求"，管不住"同一瞬间的突发"。
# 真正要限制的是"有多少个线程池 worker 被这个**可选**依赖占住"：向量层是增强能力，
# 不该占用超过个位数的 worker。
VECTOR_BULKHEAD_MAX_CONCURRENT = int(
    os.environ.get("VECTOR_BULKHEAD_MAX_CONCURRENT", "2")
)
# 拿不到许可时的等待上限（秒）：等到就正常执行，等不到就**直接降级**（不排队）
VECTOR_BULKHEAD_WAIT_SECONDS = float(
    os.environ.get("VECTOR_BULKHEAD_WAIT_SECONDS", "2.0")
)
# 人脸检测置信度阈值
FACE_DETECT_CONF = 0.4
# 人脸脱敏合规开关：开启（SANITIZE_FACES=1）后推帧/展示时对人脸区域打码
# （个人信息保护法：人脸为最高敏信息，识别用于统计但展示不泄露身份）
SANITIZE_FACES = os.environ.get("SANITIZE_FACES", "0") == "1"
# 数据可信度门禁：超过该秒数没有任何新帧，视为视频源断流/未启动。
# 此时各统计的 0 应理解为「无数据」而不是「无客流」（区分设备故障与真实业务）
DATA_STALE_SECONDS = int(os.environ.get("DATA_STALE_SECONDS", "90"))
# 人脸最小检测尺寸
FACE_MIN_SIZE = 20
# 活跃可疑轨迹评分阈值
ANOMALY_ACTIVE_THRESHOLD = 50
# 告警队列最大长度
ANOMALY_ALERT_QUEUE_MAX = 500
# 全局日志队列最大长度
ANOMALY_GLOBAL_LOG_MAX = 5000
# 已告警轨迹去重容量
ANOMALY_ALERTED_MAX = 2000
# 店员异常评分扣减
ANOMALY_STAFF_PENALTY = 40
# 未访问货架直接离开的命中帧阈值
ANOMALY_NO_SHELF_HIT_THRESHOLD = 10
# 快速穿越货架区域数量阈值
ANOMALY_FAST_MULTI_SHELF_COUNT = 3
# 快速穿越轨迹命中帧阈值
ANOMALY_FAST_MULTI_SHELF_HITS = 100
# 异常评分 L1 权重
ANOMALY_L1_WEIGHTS = {
    "skip_checkout": 30,
    "no_shelf_visit": 20,
    "avoid_checkout_path": 15,
    "exit_directly_after_shelf": 15,
}
# 异常评分 L2 权重
ANOMALY_L2_WEIGHTS = {
    "hand_inward": 20,
    "rapid_arm_retract": 15,
    "body_blocking": 10,
}
# 异常评分 L3 权重
ANOMALY_L3_WEIGHTS = {
    "fast_multi_shelf": 10,
    "frequent_lookback": 5,
    "avoid_camera": 10,
}
# 异常告警 watch 阈值
ANOMALY_ALERT_THRESHOLD_WATCH = 50
# 异常告警 high 阈值
ANOMALY_ALERT_THRESHOLD_HIGH = 70
# 表情时间线最大长度
EMOTION_TIMELINE_MAX = 10000
# 最近表情记录最大长度
EMOTION_RECENT_MAX = 200
# 最近表情记录默认查询条数
EMOTION_RECENT_DEFAULT_LIMIT = 20
# 表情置信度过滤阈值
EMOTION_CONF_THRESHOLD = 0.3
# 表情趋势最少样本数
EMOTION_TREND_MIN_SAMPLES = 10
# 情绪变化大阈值
EMOTION_DELTA_LARGE = 0.1
# 情绪变化小阈值
EMOTION_DELTA_SMALL = 0.03
# 热度评分权重
POPULARITY_HEAT_WEIGHTS = {
    "visit": 0.2,
    "dwell": 0.5,
    "deep": 0.3,
}
# 深度兴趣停留阈值
POPULARITY_DWELL_THRESHOLD = 30.0
# 店员判定连续停留阈值
POPULARITY_STAFF_THRESHOLD = 900.0
# 店员累计判定阈值
POPULARITY_STAFF_CUMULATIVE = 1800.0
# 热度统计最少轨迹命中帧数
POPULARITY_MIN_TRACK_HITS = 3
# 热度去重缓存最大轨迹数
POPULARITY_MAX_TRACKED = 5000
# 轨迹最大丢失帧数
TRACK_MAX_LOST = 30
# 人群聚集人数阈值
EVENT_CROWD_THRESHOLD = 5
# 轨迹异常检查帧数
EVENT_TRAJECTORY_CHECK_FRAMES = 90
# 人群聚集事件去重窗口
EVENT_CROWD_FRAME_WINDOW = 30

# ==================== API配置 ====================
# FastAPI 监听地址
API_HOST = "0.0.0.0"
# FastAPI 监听端口
API_PORT = 8000

# ==================== LLM配置（Agent层） ====================
# LLM 供应商
LLM_PROVIDER = "openai"
# LLM 模型名
LLM_MODEL = "deepseek-chat"
# LLM API Key，从环境变量读取
LLM_API_KEY = os.environ.get("dazuoye_api", "")
# OpenAI 兼容接口地址
LLM_BASE_URL = "https://api.deepseek.com"
# LLM 生成温度
# LLM 生成温度（0.1：更稳定、方差更小，适合业务数据场景；过高会产生随机行为）
LLM_TEMPERATURE = 0.1

# ==================== 百度地图（POI 竞品/商圈/地理编码/距离测算） ====================
# 服务端 AK（Web 服务 API 要求服务端类型）+ SK（sn 签名，地点检索/距离矩阵强制要求）
BAIDU_MAP_AK = os.environ.get("baidu_map_ak", "")
BAIDU_MAP_SK = os.environ.get("baidu_map_sk", "")

# ==================== 表情分析数据库配置（来自 final_work） ====================
# SQLite 表情数据库路径
EMOTION_DB_PATH = str(DATA_DIR / "shop_emotion.db")
# 视频输出目录
VIDEO_OUTPUT_DIR = str(DATA_DIR / "videos")

# ==================== 视频输入源（统一入口：open_source / kind=file 白名单） ====================
# ⚠ 这是**安全边界**（台账 B6）：修复前 WS 的 `start_file` 接受**任意路径**，
#   登录账号就能让服务端打开容器内任意可解码文件（并当存在性探测器）。
#   现在 `kind=file` 只允许这些目录（相对项目根或绝对路径，逗号分隔）；
#   `kind=upload` 更是只接受**文件名**，真实路径由服务端拼接。
#   服务端自己配置的摄像头（config/cameras.yaml 的 source）不受此限制 —— 那是可信配置。
VIDEO_ALLOWED_DIRS = [
    _p.strip() for _p in os.environ.get(
        "VIDEO_ALLOWED_DIRS", "data/videos,data/sources").split(",") if _p.strip()
]
# 上传视频落盘的子目录（必须与 `POST /api/videos` 的保存位置一致，供 kind=upload 映射）
VIDEO_UPLOAD_SUBDIR = os.environ.get("VIDEO_UPLOAD_SUBDIR", "videos")

# ==================== 本地语音唤醒模型配置 ====================
# sherpa-onnx 推理设备：cpu / cuda
SHERPA_ONNX_PROVIDER = os.environ.get("SHERPA_ONNX_PROVIDER", "cpu")
# KWS 唤醒词模型目录
KWS_MODEL_DIR = PROJECT_ROOT / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
# KWS 关键词配置文件
KWS_KEYWORDS_FILE = KWS_MODEL_DIR / "keywords.txt"
# KWS 音频采样率
KWS_SAMPLE_RATE = 16000
# 唤醒词触发阈值，越低越容易唤醒
KWS_THRESHOLD = 0.25
# 唤醒词 token 打分权重
KWS_SCORE = 2.0
# 判断“开始说话”的能量阈值
KWS_ENERGY_THRESHOLD = 0.01
# 语音段结束静音时长
KWS_SILENCE_SECONDS = 0.35
# 补尾静音时长
KWS_TAIL_PADDING_SECONDS = 0.4
# 单个语音段最大时长
KWS_MAX_SEGMENT_SECONDS = 2.0
# 唤醒关键词拼音列表
KWS_KEYWORD = (
    "x i\u01ceo l \u00edng :2.0 #0.15 @\u5c0f\u96f6/"
    "x i\u01ceo n \u00edng @\u5c0f\u5b81/"
    "x i\u01ceo l \u00edn @\u5c0f\u6797/"
    "x i\u01ceo m \u00edng @\u5c0f\u660e/"
    "x i\u01ceo x \u012bng @\u5c0f\u661f/"
    "x i\u01ceo q \u012bng @\u5c0f\u6e05/"
    "x i\u01ceo y \u012bng @\u5c0f\u82f1/"
    "x i\u01ceo b \u012bng @\u5c0f\u51b0/"
    "x i\u01ceo p \u00edng @\u5c0f\u5e73/"
    "x i\u01ceo x \u012bn @\u5c0f\u5fc3/"
    "x i\u01ceo j \u012bn @\u5c0f\u91d1/"
)

# ==================== 本地流式中文识别模型配置（临时麦克风测试） ====================
# ASR 模型目录
ASR_MODEL_DIR = PROJECT_ROOT / "all_models" / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
# ASR 音频采样率
ASR_SAMPLE_RATE = 16000
# ASR 语音能量阈值
ASR_ENERGY_THRESHOLD = 0.01
# ASR 结束补尾静音时长
ASR_TAIL_PADDING_SECONDS = 0.4
# ASR 单段最大时长
ASR_MAX_SEGMENT_SECONDS = 5.0
# 自动增益目标 RMS
ASR_TARGET_RMS = 0.1
# 自动增益最大放大倍数
ASR_MAX_GAIN = 5.0

# 表情分析降采样配置
# 每 10 帧做一次表情多数表决
LOCAL_SAMPLE_FRAMES = 10
# 每 100 条表决记录批量写入数据库
LOCAL_BATCH_SAVE = 100
# 保存视频的帧率
LOCAL_VIDEO_FPS = 10
# 单个视频最长 5 分钟
LOCAL_VIDEO_MAX_SECONDS = 300

# 缓存清理配置
# 缓存清理周期（秒）
CACHE_CLEAN_INTERVAL_SECONDS = 600
# 缓存文件最大保留时长（小时）
CACHE_MAX_AGE_HOURS = 24
# 数据库记录保留天数
DB_RECORD_KEEP_DAYS = 30

# ==================== 销量自动同步（POS 目录投递） ====================
# 真实零售常见形态：POS/ERP 定时把导出的 CSV 投递到共享目录。
# 开启后后台线程定时扫描该目录并自动导入销量，取代「人手调接口」。
# 处理成功归档到 processed/，失败移到 failed/（便于排查），同一文件不会重复导入。
SALES_INBOX_ENABLED = os.environ.get("SALES_INBOX_ENABLED", "1") == "1"
# 投递目录（POS 导出放这里）
SALES_INBOX_DIR = os.environ.get("SALES_INBOX_DIR") or str(DATA_DIR / "sales_inbox")
# 扫描周期（秒）
SALES_INBOX_INTERVAL_SECONDS = int(os.environ.get("SALES_INBOX_INTERVAL_SECONDS", "60"))

# ==================== 登录鉴权 ====================
# 总开关：关闭后所有接口不再校验登录（仅本地调试/自动化测试用）
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "1") == "1"
# 会话有效期（小时）—— 空闲超时：有活动会滑动续期，最长不超过下面的绝对上限
AUTH_SESSION_HOURS = int(os.environ.get("AUTH_SESSION_HOURS", "12"))
# 会话**绝对上限**（小时，从创建算起）：滑动续期不会越过它。
# 没有这条，会话可以无限续命（滑动窗口的经典漏洞）。
AUTH_SESSION_MAX_HOURS = int(os.environ.get("AUTH_SESSION_MAX_HOURS", "24"))
# 剩余时间不足该小时数时才考虑续期（避免每次请求都写库）
AUTH_SESSION_RENEW_THRESHOLD_HOURS = int(
    os.environ.get("AUTH_SESSION_RENEW_THRESHOLD_HOURS", "2")
)
# 同一会话两次续期写库的最小间隔（秒）——get_session 跑在每个请求上，必须节流
AUTH_SESSION_TOUCH_SECONDS = int(os.environ.get("AUTH_SESSION_TOUCH_SECONDS", "60"))
# 会话来源 IP：默认记录**直连对端** IP。若部署在反向代理后面（此时对端是代理 IP），
# 可置 1 改为信任 `X-Forwarded-For` 的第一跳 —— ⚠ 该头可被客户端伪造，
# 所以默认关闭；它只用于审计展示，不参与任何安全判定。
AUTH_TRUST_FORWARDED_FOR = os.environ.get("AUTH_TRUST_FORWARDED_FOR", "0") == "1"
# 首次启动自动创建的 root 账号名
AUTH_ROOT_USERNAME = os.environ.get("AUTH_ROOT_USERNAME", "root")
# 初始 root 密码；不设则随机生成并在终端打印一次
AUTH_ROOT_PASSWORD = os.environ.get("AUTH_ROOT_PASSWORD", "")
# 鉴权数据库：独立于业务库（鉴权不应因 MySQL 不可用而把所有人锁在门外）
AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH") or str(DATA_DIR / "auth.db")
# 鉴权库的 SQLite journal 模式。
#
# ⚠ 默认 **DELETE 而不是 WAL**：`data/auth.db` 位于 Windows 宿主目录的 bind mount
# （Docker Desktop 走 9p/drvfs），而 **WAL 需要 mmap 共享内存文件（`-shm`）**，
# 9p 对 mmap 支持不完整 —— 实测在**容器重建后**该库直接打不开：
#     sqlite3.OperationalError: disk I/O error（连 PRAGMA table_info 都读不了）
# 后果是登录接口 500（鉴权链路全挂），而 MySQL/业务链路看着还正常，很难联想到"换了个容器"。
#
# 鉴权是**单进程 + 低频写入**，DELETE（回滚日志）模式完全够用，且不依赖 mmap；
# 若把 auth.db 放到 Linux 原生卷（named volume）或非 9p 文件系统，
# 可设 `AUTH_SQLITE_JOURNAL_MODE=WAL` 拿回读写并发。
AUTH_SQLITE_JOURNAL_MODE = os.environ.get("AUTH_SQLITE_JOURNAL_MODE", "DELETE").upper()

# ---- 登录限流（防暴力破解）----
# 同一「用户名 + IP」在窗口内失败达到该次数即锁定
AUTH_MAX_FAILED_ATTEMPTS = int(os.environ.get("AUTH_MAX_FAILED_ATTEMPTS", "5"))
# 失败计数窗口（秒）：窗口内累计，超出则重新计数
AUTH_FAILED_WINDOW_SECONDS = int(os.environ.get("AUTH_FAILED_WINDOW_SECONDS", "900"))
# 锁定时长（秒）：首次锁定；重复触发按倍数递增（上限见 AUTH_LOCKOUT_MAX_SECONDS）
AUTH_LOCKOUT_SECONDS = int(os.environ.get("AUTH_LOCKOUT_SECONDS", "300"))
# 锁定时长上限（秒）
AUTH_LOCKOUT_MAX_SECONDS = int(os.environ.get("AUTH_LOCKOUT_MAX_SECONDS", "3600"))
# 同一 IP 的总失败上限（= 用户名上限 × 该倍数）：防「撞库式」换用户名试探
AUTH_IP_ATTEMPT_MULTIPLIER = int(os.environ.get("AUTH_IP_ATTEMPT_MULTIPLIER", "5"))

# ---- 口令哈希强度 ----
# PBKDF2-HMAC-SHA256 迭代轮数（慢哈希；OWASP 对 SHA256 的建议值为 60 万）
AUTH_PBKDF2_ITERATIONS = int(os.environ.get("AUTH_PBKDF2_ITERATIONS", "600000"))
# 口令最小长度
AUTH_MIN_PASSWORD_LEN = int(os.environ.get("AUTH_MIN_PASSWORD_LEN", "8"))

# ---- Cookie 传输安全 ----
# 是否给会话 Cookie 加 Secure 标志（仅 https 传输）。
# 默认 auto：按请求协议自动判定（https 加、http 不加），避免本地 http 调试时登录失败。
# 可显式设 1/0 强制开关。
_auth_cookie_secure = os.environ.get("AUTH_COOKIE_SECURE", "auto").strip().lower()
AUTH_COOKIE_SECURE = {"1": True, "true": True, "0": False, "false": False}.get(_auth_cookie_secure)

# ---- 跨域（CORS）----
# 允许跨域访问的来源，逗号分隔。**留空 = 不开启跨域**（前端与后端同源，本就不需要 CORS）。
# 仅当跨域前端确实存在时才配置，例：AUTH_CORS_ORIGINS=http://localhost:5173
#
# 为什么默认关闭：此前是 allow_origins=["*"] + allow_credentials=True，
# 实测会被反射成「Access-Control-Allow-Origin: <任意站点> + 允许凭据」——
# 等于把登录态暴露给任意网站（当前仅靠 Cookie 的 SameSite=Lax 兜底，
# 一旦为跨站嵌入改成 SameSite=None 就会立刻变成可利用漏洞）。
AUTH_CORS_ORIGINS = [
    o.strip() for o in os.environ.get("AUTH_CORS_ORIGINS", "").split(",") if o.strip()
]

# 接口文档（/docs、/redoc、/openapi.json）是否公开。
# 默认 0（需登录）：对已上鉴权的系统，公开完整接口清单等于给攻击者一张地图。
# 登录用户可正常访问文档（同源 Cookie 自动携带），因此本地开发无需开启。
AUTH_PUBLIC_DOCS = os.environ.get("AUTH_PUBLIC_DOCS", "0") == "1"

# ---- 一次性票据（给 WS / 音频等『无法自定义请求头』的场景）----
# 有效期（秒）：票据短时效，用后即焚；即便出现在访问日志里也已失效。
AUTH_TICKET_TTL_SECONDS = int(os.environ.get("AUTH_TICKET_TTL_SECONDS", "60"))
# 是否允许把「主会话令牌」放在 URL query（?token=）。
# **默认 0 = 拒绝**：会话令牌进 URL 会落到访问日志/反代日志（CWE-598），
# 需要 URL 传凭据的场景请改用 /api/auth/ws-ticket 换取一次性票据（?ticket=）。
AUTH_ALLOW_QUERY_TOKEN = os.environ.get("AUTH_ALLOW_QUERY_TOKEN", "0") == "1"


def ensure_dirs():
    """确保所有运行时目录存在"""
    for d in [DATA_DIR, DATA_DIR / "logs", DATA_DIR / "snapshots",
              FRAME_SAVE_DIR, DATA_DIR / "videos"]:
        os.makedirs(d, exist_ok=True)
