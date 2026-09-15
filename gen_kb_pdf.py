"""
生成《智能零售分析系统 业务知识库》PDF
内容全部取自本项目真实素材：
  - README.md（系统概述、技术栈、FAQ）
  - config/settings.py（全局参数、Agent 记忆/LTM 参数）
  - config/roi_zones.yaml（业务区域/货架配置）
  - config/thresholds.yaml（热度/异常评估口径）与 skills/*（统计口径）
  - agents/master_agent.py、base_agent.py、long_term_memory.py（工具/记忆）
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, ListFlowable, ListItem, HRFlowable,
)

# ---- 中文字体 ----
pdfmetrics.registerFont(TTFont("SimHei", r"C:/Windows/Fonts/simhei.ttf"))
# 统一用 .ttf 单字体（simhei / simsun），确保 PDF 文本层可正确提取 Unicode（供 RAG 切片）
pdfmetrics.registerFont(TTFont("MSYahei", r"C:/Windows/Fonts/simhei.ttf"))

OUT = "业务知识库_智能零售分析系统.pdf"

# ---- 样式 ----
C = colors.HexColor("#1f3a5f")   # 主题深蓝
C2 = colors.HexColor("#3a6ea5")
G = colors.HexColor("#eef3fa")

def S(name, **kw):
    return ParagraphStyle(name, **kw)

st_title = S("title", fontName="SimHei", fontSize=22, leading=28, textColor=C,
             alignment=TA_CENTER, spaceAfter=4)
st_sub = S("sub", fontSize=11, leading=15, textColor=C2, alignment=TA_CENTER)
st_h1 = S("h1", fontName="SimHei", fontSize=15, leading=20, textColor=colors.white,
          spaceBefore=12, spaceAfter=6)
st_h2 = S("h2", fontName="SimHei", fontSize=12, leading=17, textColor=C,
          spaceBefore=8, spaceAfter=4)
st_body = S("body", fontSize=10.5, leading=16, textColor=colors.HexColor("#222222"))
st_bullet = S("bullet", fontSize=10.5, leading=16, textColor=colors.HexColor("#222222"))
st_cell = S("cell", fontSize=9.5, leading=13)
st_note = S("note", fontSize=9, leading=13, textColor=colors.HexColor("#666666"))

def h1(text):
    t = Table([[Paragraph(text, st_h1)]], colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t

def h2(text):
    return Paragraph(text, st_h2)

def body(text):
    return Paragraph(text, st_body)

def bullets(items):
    return ListFlowable(
        [ListItem(Paragraph(i, st_bullet), leftIndent=12) for i in items],
        bulletType="bullet", start="•", leftIndent=12, bulletFontSize=10,
    )

def kv_table(header, rows, widths):
    data = [[Paragraph(hx, S("hd", fontName="SimHei", fontSize=9.5, leading=13,
                            textColor=colors.white)) for hx in header]]
    for r in rows:
        data.append([Paragraph(str(c), st_cell) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), C2),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c4d4")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), G))
    t.setStyle(TableStyle(style))
    return t

def note(text):
    return Paragraph(text, st_note)

def hr():
    return HRFlowable(width="100%", thickness=0.6, color=C2, spaceBefore=6, spaceAfter=6)

story = []

# ==================== 封面 ====================
story.append(Spacer(1, 24 * mm))
story.append(Paragraph("智能零售分析系统", st_title))
story.append(Paragraph("业务知识库（Business Knowledge Base）", st_sub))
story.append(Spacer(1, 6 * mm))
story.append(Paragraph("适用场景：门店视频分析 · 顾客行为理解 · 智能问答 Agent 支撑",
                       S("cov", fontSize=10, leading=14, alignment=TA_CENTER,
                         textColor=colors.HexColor("#555555"))))
story.append(Spacer(1, 6 * mm))
story.append(hr())
story.append(note("内容来源：本项目真实实现（README、config/settings.py、config/roi_zones.yaml、"
                  "config/thresholds.yaml、skills/*、agents/*）。如有与代码冲突，以代码为准。"))
story.append(PageBreak())

# ==================== 1 系统概述 ====================
story.append(h1("一、系统概述"))
story.append(body("本系统是一套面向门店场景的智能零售分析系统，整合了货架热度分析、异常行为告警、"
                  "顾客表情分析、本地语音唤醒、语音问答、会话记录、MySQL/SQLite 持久化与查询历史向量召回。"))
story.append(h2("核心技术栈"))
story.append(bullets([
    "后端：Python 3.13 · FastAPI · Uvicorn · WebSocket",
    "视觉：Ultralytics YOLO26n 行人检测 + ByteTrack 跟踪 + YOLOv8n-face 人脸检测 + MobileNetV3 表情识别",
    "语音：sherpa-onnx KWS 唤醒词（小零 等）+ 流式中文 ASR + edge-tts 服务端中文语音合成",
    "AI/Agent：LangChain + DeepSeek（OpenAI 兼容接口）构建 tool-calling Agent",
    "存储：MySQL 优先、SQLite 自动回退；Qdrant + Ollama(bge-small-zh-v1.5) 向量召回",
    "前端：Vue 构建版（frontend-vue/dist）优先，缺失时回退原生 JS 版（frontend/）；另含微信小程序端",
]))

# ==================== 2 业务区域配置 ====================
story.append(h1("二、业务区域（ROI）配置"))
story.append(body("系统通过 ROI 区域划分业务语义，货架、收银台、出口分别对应不同分析逻辑。"
                  "配置来自 config/roi_zones.yaml 与 config/roi_zones_local.yaml。"))
story.append(kv_table(
    ["区域ID", "类型", "业务标签", "说明"],
    [
        ["shelf_A", "shelf", "1号货架 - 零食区", "货架热度/关注度统计目标"],
        ["shelf_B", "shelf", "2号货架 - 饮料区", "货架热度/关注度统计目标"],
        ["shelf_C", "shelf", "3号货架 - 日用品区", "货架热度/关注度统计目标"],
        ["checkout", "checkout", "收银台", "异常行为判断（绕过收银台）"],
        ["exit", "exit", "出口", "异常行为判断（直接离店）"],
    ],
    [26 * mm, 26 * mm, 52 * mm, 60 * mm],
))
story.append(note("注：区域类型 type ∈ {shelf, checkout, exit}；异常检测依赖收货架区、是否经过收银台、"
                  "是否直接走向出口等区域访问序列。"))

# ==================== 3 货架热度口径 ====================
story.append(h1("三、货架热度统计口径"))
story.append(body("货架热度采用三维评分：到访人次、总停留时长、深度兴趣人数，"
                  "三者经最大值归一化后加权得到 0–100 的热度分。"))
story.append(kv_table(
    ["指标", "含义", "实现要点"],
    [
        ["visit_count 到访人次", "首次进入该区域计 1 次", "同一轨迹(track_id)同一区域仅计一次（去重）"],
        ["total_dwell_seconds 总停留", "所有人在该区域停留的总秒数", "按真实时间差 enter_ts 计算"],
        ["deep_interest_count 深度兴趣人数", "停留超过深度阈值的人数", "阈值 POPULARITY_DWELL_THRESHOLD=30 秒"],
        ["heat_score 热度分", "加权综合热度（0–100）", "= (visit_norm*0.2 + dwell_norm*0.5 + deep_norm*0.3) * 100"],
        ["staff 店员", "店员轨迹被专门识别并降权", "连续停留阈值 900s / 累计阈值 1800s"],
        ["current_visitors 当前人数", "当前停留于该区域的人数", "按轨迹中心点所在 zone 统计"],
        ["avg_dwell 平均停留", "人均停留秒数", "dwell_total / dwell_count"],
    ],
    [38 * mm, 40 * mm, 86 * mm],
))
story.append(h2("关键阈值（config/thresholds.yaml）"))
story.append(bullets([
    "dwell_threshold_seconds：30 —— 停留超过 30 秒视为对货架“感兴趣”",
    "min_track_hits：3 —— 轨迹稳定（被跟踪 ≥3 帧）后才开始计数",
    "time_window_minutes：60 —— 统计时间窗口",
    "min_zone_enter_frames：5 —— 至少进入区域 5 帧才算有效停留（防误入）",
]))

# ==================== 4 异常行为检测 ====================
story.append(h1("四、异常行为检测口径"))
story.append(body("系统对顾客轨迹进行多层评分，异常评分越高越需关注。评分按可靠性分为三层（L1/L2/L3），"
                  "并设有“店员/需关注(≥50)/高度可疑(≥70)”三个分级阈值；店员轨迹会自动降低评分。"))
story.append(h2("4.1 评分权重"))
story.append(kv_table(
    ["层级", "行为", "分值", "说明"],
    [
        ["L1 轨迹逻辑", "skip_checkout", 30, "去过货架区但未经过收银区直接去出口"],
        ["L1 轨迹逻辑", "no_shelf_visit", 20, "未浏览货架但直接离开"],
        ["L1 轨迹逻辑", "avoid_checkout_path", 15, "轨迹绕开收银台"],
        ["L1 轨迹逻辑", "exit_directly_after_shelf", 15, "离开货架区后直接走向出口"],
        ["L2 姿态动作", "hand_inward", 20, "手部向身体内侧移动（口袋/包方向）"],
        ["L2 姿态动作", "rapid_arm_retract", 15, "取物后手臂快速收缩"],
        ["L2 姿态动作", "body_blocking", 10, "身体刻意遮挡手部动作"],
        ["L3 综合模式", "fast_multi_shelf", 10, "短时间内快速穿越多个货架"],
        ["L3 综合模式", "frequent_lookback", 5, "频繁回头/环顾四周"],
        ["L3 综合模式", "avoid_camera", 10, "刻意躲避摄像头视角"],
    ],
    [30 * mm, 44 * mm, 20 * mm, 70 * mm],
))
story.append(h2("4.2 分级阈值"))
story.append(kv_table(
    ["阈值", "取值", "含义"],
    [
        ["ANOMALY_ALERT_THRESHOLD_WATCH", 50, "评分 ≥ 50 → 需关注"],
        ["ANOMALY_ALERT_THRESHOLD_HIGH", 70, "评分 ≥ 70 → 高度可疑（建议人工复核）"],
        ["ANOMALY_STAFF_PENALTY", 40, "店员评分扣减"],
    ],
    [50 * mm, 30 * mm, 84 * mm],
))
story.append(note("内容安全约束：输出禁用“偷窃/盗窃/小偷”等法律定性词，统一使用“可疑行为、异常模式、"
                  "需要关注”等中性措辞；所有高风险结论需附“建议人工复核”。"))

# ==================== 5 顾客表情分析 ====================
story.append(h1("五、顾客表情分析口径"))
story.append(body("入口摄像头做人脸检测 + 表情识别，输出表情分布、正负情绪占比与情感趋势。"))
story.append(kv_table(
    ["项", "取值/规则", "说明"],
    [
        ["正情绪", "happy, neutral", "positive_count 统计口径"],
        ["负情绪", "angry, disgust, fear, sad", "negative_count 统计口径"],
        ["置信度过滤", "EMOTION_CONF_THRESHOLD=0.3", "低于阈值忽略"],
        ["趋势最少样本", "EMOTION_TREND_MIN_SAMPLES=10", "不足则报告“数据不足”"],
        ["情绪变化阈值", "±LARGE=0.1 / ±SMALL=0.03", "判定明显改善/下降"],
        ["表情多数表决", "每 10 帧表决一次", "LOCAL_SAMPLE_FRAMES"],
        ["批量入库", "每 100 条批量写库", "LOCAL_BATCH_SAVE"],
        ["人脸检测", "FACE_DETECT_CONF=0.4, FACE_MIN_SIZE=20", "YOLOv8n-face"],
    ],
    [32 * mm, 56 * mm, 76 * mm],
))
story.append(h2("情绪趋势结论规则"))
story.append(bullets([
    "delta > +0.10 → 顾客情绪明显好转，购物体验改善",
    "delta > +0.03 → 顾客情绪略有改善",
    "delta < -0.10 → 顾客情绪明显下降，建议关注服务或环境",
    "delta < -0.03 → 顾客情绪轻微下降",
    "其余 → 顾客情绪基本稳定，无明显变化",
]))

# ==================== 6 语音链路 ====================
story.append(h1("六、本地语音链路"))
story.append(bullets([
    "唤醒（KWS）：sherpa-onnx zipformer 唤醒词模型，唤醒词含“小零/小宁/小林/小明/小星”等，"
    "触发阈值 KWS_THRESHOLD=0.25，音频 16kHz。",
    "指令识别（ASR）：唤醒后使用 sherpa-onnx 流式中文 ASR（zipformer-zh-14M），识别用户指令。",
    "语音回复（TTS）：edge-tts 服务端中文语音合成，手机端通过 HTTPS 播放。",
    "交互流程：说“小零” → 系统回复“我在” → 5 秒内说“打开摄像头”等指令。",
]))

# ==================== 7 Agent 架构 ====================
story.append(h1("七、智能问答 Agent 架构"))
story.append(body("系统基于 LangChain 的 tool-calling Agent（ChatOpenAI 对接 DeepSeek，deepseek-chat，"
                  "温度 0.1）构建，将业务技能封装为工具，由 LLM 决定何时调用与融合。"))
story.append(h2("7.1 可用工具"))
story.append(kv_table(
    ["工具名", "能力"],
    [
        ["get_shelf_popularity", "货架热度（到访/停留/排名）"],
        ["get_anomaly_alerts", "可疑行为告警"],
        ["get_emotion_stats", "顾客表情/情绪分布与趋势"],
        ["search_chat_history", "按关键词检索历史会话"],
        ["get_heat_report", "系统定期生成的热度汇报摘要"],
        ["get_sales_comparison", "客流量热度 vs 实际销量，转化率与归因诊断"],
        ["get_movement_paths", "顾客购物动线（区域关联）"],
        ["get_hourly_traffic", "按小时聚合客流，高峰/低谷"],
        ["get_zone_depth", "区域“深度兴趣 vs 销量”四象限分析"],
    ],
    [52 * mm, 112 * mm],
))
story.append(h2("7.2 路由与安全"))
story.append(bullets([
    "意图路由（intent_router）：正则结构骨架识别“报数”vs“分析”意图。",
    "语义路由闸（semantic_router）：对正则漏网的口语问句用向量相似度兜底，"
    "相似度 ≥ 0.75 判定“像分析问句”→ 降级走 LLM（fail-safe 方向，规范了模板劫持）。",
    "安全约束：区分闲聊/元问题（不调工具）、限制无关话题、识别并忽略用户消息中的“系统指令”注入。",
    "回复风格：简洁专业 1–3 句，不使用 emoji 与特殊符号。",
]))

story.append(h2("7.3 记忆分层"))
story.append(kv_table(
    ["记忆层", "载体", "作用"],
    [
        ["短期记忆", "LangGraph 检查点（按 thread_id 保存会话消息）", "保留当前会话上下文"],
        ["会话摘要压缩", "检查点消息估算 token 超过 4000×0.7 触发压缩，保留最近 6 条", "长会话历史压缩"],
        ["长期记忆", "agent_long_term_memory（MySQL/SQLite）按 session_id 存摘要", "跨会话连续性"],
        ["向量索引", "Qdrant SESSION_SUMMARY_COLLECTION + bge 向量", "语义检索长期记忆"],
        ["查询历史召回", "Qdrant 并存混合检索（MySQL 关键词 + 向量语义 + 时间衰减）", "历史问答增强"],
    ],
    [30 * mm, 66 * mm, 68 * mm],
))

# ==================== 8 存储与向量召回 ====================
story.append(h1("八、数据存储与向量召回"))
story.append(bullets([
    "持久化：MySQL（Retail_assistant 库）优先，未配置/不可用时自动回退 SQLite（data/）。",
    "业务表覆盖：表情、问答历史、会话、语音日志、TTS 缓存、视频记录、ROI 配置、长期记忆等。",
    "向量召回：Qdrant 本地模式（单进程）或 QDRANT_URL 的 Server 模式（Docker 多实例）切换；"
    "embedding 由 Ollama bge-small-zh-v1.5 提供，维度 512，余弦距离。",
    "混合检索 search_messages_hybrid：MySQL 关键词精确匹配（视为满相关）+ Qdrant 向量语义匹配，"
    "统一按（相关性 × 时间衰减）排序，任一路径失败自动降级。",
    "工程防护：智能切片（≤500 字符）、emoji/非 BMP 清洗、超限自动降级，避免 Ollama 500。",
]))

# ==================== 9 配置参数速查 ====================
story.append(h1("九、核心配置参数速查"))
story.append(kv_table(
    ["参数", "取值", "说明"],
    [
        ["API_HOST / API_PORT", "0.0.0.0 / 8000", "FastAPI 服务地址"],
        ["LLM_MODEL", "deepseek-chat", "Agent 模型"],
        ["LLM_TEMPERATURE", "0.1", "更稳定，适合业务数据场景"],
        ["AGENT_VECTOR_RECALL_LIMIT", 3, "Agent 向量召回条数"],
        ["AGENT_VECTOR_MIN_SCORE", 0.45, "召回最低相似度"],
        ["AGENT_MEMORY_HISTORY_MAX", 100, "内存历史上限"],
        ["AGENT_SUMMARY_MAX_TOKENS / TRIGGER", "4000 / 0.7", "摘要压缩阈值"],
        ["AGENT_LONG_TERM_INJECT_LIMIT", 3, "新会话注入长期记忆条数"],
        ["AGENT_LONG_TERM_KEEP_RECORDS", 200, "长期记忆保留条数"],
        ["VECTOR_EMBED_MAX_CHARS", 500, "bge 512 token 上限的防护"],
        ["HEAT_REPORT_INTERVAL_SECONDS", 600, "定期热度汇报间隔"],
        ["VECTOR_SEARCH_DEFAULT_LIMIT", 10, "向量默认返回条数"],
    ],
    [56 * mm, 46 * mm, 62 * mm],
))

# ==================== 10 FAQ ====================
story.append(h1("十、常见问题（FAQ）"))
story.append(kv_table(
    ["问题", "答复"],
    [
        ["为什么没有写入 MySQL？", "检查启动终端是否配置 mysql_root 环境变量，以及启动日志是否显示"
         "“[OK] 数据写入: MySQL Retail_assistant”；若显示“回退到 SQLite”说明未生效。"],
        ["语音没有回复？", "手机端必须使用 HTTPS；确认浏览器允许麦克风；确认服务端可访问 edge-tts 外网；"
         "手机浏览器可能需要先点击一次“语音输入”解锁 AudioContext。"],
        ["向量搜索失败？", "确认 Ollama 已启动并已安装 qllama/bge-small-zh-v1.5；若本地 Qdrant 被其他进程占用，"
         "需先停止旧服务再启动。"],
        ["如何强制使用原生前端？", "访问 http://localhost:8000/?vue=0，或在构建产物缺失时系统自动回退原生版。"],
    ],
    [50 * mm, 114 * mm],
))

story.append(Spacer(1, 8 * mm))
story.append(hr())
story.append(note("— 结束 —　生成内容取自项目真实实现，如需执行请以代码为准。"))

doc = SimpleDocTemplate(
    OUT, pagesize=A4,
    leftMargin=20 * mm, rightMargin=20 * mm,
    topMargin=18 * mm, bottomMargin=18 * mm,
    title="智能零售分析系统 业务知识库", author="Retail Assistant",
)
doc.build(story)
print("[OK] 已生成:", OUT)
