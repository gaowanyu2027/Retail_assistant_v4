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
# 人脸检测置信度阈值
FACE_DETECT_CONF = 0.4
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

# ==================== 表情分析数据库配置（来自 final_work） ====================
# SQLite 表情数据库路径
EMOTION_DB_PATH = str(DATA_DIR / "shop_emotion.db")
# 视频输出目录
VIDEO_OUTPUT_DIR = str(DATA_DIR / "videos")

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


def ensure_dirs():
    """确保所有运行时目录存在"""
    for d in [DATA_DIR, DATA_DIR / "logs", DATA_DIR / "snapshots",
              FRAME_SAVE_DIR, DATA_DIR / "videos"]:
        os.makedirs(d, exist_ok=True)
