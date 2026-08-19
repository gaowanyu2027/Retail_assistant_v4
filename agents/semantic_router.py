"""
语义路由闸：用向量相似度补正则的漏网（分析意图语义闸）

背景：口语变体是开放集（"受嘛影响/受那些因素/跟销量有关吗/咋提高…"），
intent_router 的正则结构骨架已覆盖绝大多数，但总有漏网。本模块把
"该走 LLM 的分析问句"作为范例存成内存向量库，当正则判定某问句可走模板时，
再查一次语义相似度——**像分析问句 → 降级 LLM**。

为什么存"分析问句"而不是"报数问句"（反直觉但关键）：
- Embedding 编码的是主题相似，不是意图相似。"货架热度"与"货架热度受啥影响"
  主题完全相同、语义距离很近，但路由意图相反（一个报数、一个归因）。
  若存报数正例库，分析问句极易蹭到近邻 → 模板劫持源头。
- 分析问句共享意图结构（归因/策略/观点/机制），跨主题也聚类得好，
  相似度高可信；且漏网安全（走 LLM 至少答对，只是慢），误伤才致命。
- 因此方向定为：相似度高 → 降级 LLM（fail-safe 方向）。

实现：50 条范例量级用内存余弦（加载时算好向量，查询时点积），
不依赖 Qdrant 容器；Ollama 不可用时自动 disabled（fail-open），
不阻塞主链路，语义闸只是"额外的安全网"。
"""
import threading

try:
    from vector_memory import _embed
except Exception:  # pragma: no cover - 主链路不可因语义闸失败
    _embed = None

# 分析问句范例：每条一个口语变体，覆盖归因/策略/观点/机制。
#
# ⚠️ 刻意"去主题化"（不出现 货架/客流/热度/告警 等业务词）：
# 标定实证——bge-small-zh 按主题聚类，带业务词的范例句会把同主题的报数问句
# （如"1号货架现在客流怎么样"=0.735，"货架热度排名"=0.878）拉到分析范例近旁，
# 阈值无安全区。去掉业务词后，相似度由"意图结构"主导：
# 分析问句（带任意主题）共享结构 → 仍相似；纯报数问句 → 结构不同 → 相似度低。
_ANALYSIS_EXAMPLES = [
    # 归因（受…影响 / 为什么 / 原因 / 取决于 / 靠 / 跟…有关 / 导致）
    "为什么会出现这种情况",
    "受哪些因素影响",
    "原因出在哪里",
    "是什么导致的",
    "取决于什么",
    "靠什么决定的",
    "跟什么有关系",
    "为什么变化这么大",
    "什么原因造成的",
    "为什么这么少",
    # 策略（怎么+提升动词 / 建议 / 方案）
    "怎么改进",
    "如何提升",
    "有什么建议",
    "怎么优化",
    "怎样改善",
    "有什么办法",
    "该怎么做",
    "给点意见",
    "怎么才能更好",
    "如何解决",
    # 观点（你觉得 / 怎么看 / 你的看法）
    "你觉得怎么样",
    "你怎么看",
    "你的看法是什么",
    "你感觉呢",
    "你怎么评价",
    "你觉得呢",
    # 机制（怎么算 / 怎么判断 / 通过什么 / 原理）
    "怎么算出来的",
    "用什么方法统计的",
    "原理是什么",
    "怎么判断的",
    "数据怎么来的",
    "怎么工作的",
    # 过去时间（系统只有实时数据，需模型诚实说明）
    "昨天的数据有吗",
    "之前的情况怎么样",
    # 组合 / 综合分析
    "综合看看整体情况",
    "帮我分析一下",
    "对比一下",
    "评估一下现状",
    "梳理一遍整体状况",
]

# 报数正例（去主题化：纯"要数据"的形态句）。
# 仅作为**辅助观测特征**（analyze() 输出 sim_pos），**不做阈值硬闸**——
# 标定实证：bge-small-zh 精度下正例信号与负例高度相关、无稳定判别力
# （"3号货架呢" pos=0.432 会被低阈值误伤，"咋回事儿" pos=0.663 反而偏高）。
# 留作接口：未来换更强 embedding / reranker 后再决定是否并入决策。
_REPORT_EXAMPLES = [
    "排名是多少",
    "现在有多少人",
    "情况怎么样",
    "有哪些",
    "统计一下",
    "数据给我看看",
    "数量是多少",
    "查一下",
    "列表看看",
    "数值是多少",
    "报一下",
    "看看现在",
    "最近的情况",
    "分布如何",
    "都有哪些",
    "有多少",
    "当前数据",
    "现在怎么样",
    "汇总一下",
    "查一下最新",
]

# 余弦相似度阈值：≥ 此值判定"像分析问句"→ 降级 LLM。
# 标定实证（evals/_calibrate.py）：模板类用例与分析范例最高 0.703
# （rep_03"看一下定期报告"），分析类真实口语变体多在 0.75+（靠啥决定的 0.969、
# 受那些东西影响 0.775、受嘛影响 0.773、你觉得呢 1.0、为啥人这么少 0.918）。
# 取 0.75：对现有模板类零误伤，只拦"极像分析"的规则漏网句。
# 定位是**高置信兜底**（规则漏网才轮得到它），不是裁决器——
# bge-small-zh 对中文短问句区分度有限，0.55-0.72 区间模板/分析完全重叠
# （"今天有没有可疑行为" 0.682 是模板，"你觉得今天客流怎么样" 0.633 是分析）。
_THRESHOLD = 0.75

_CACHE_SIZE = 256

_state = {
    "vecs": None,          # 负例（分析问句）向量
    "pos_vecs": None,      # 正例（报数问句）向量，仅观测
    "disabled": False,
    "init_lock": threading.Lock(),
}
_query_cache: dict[str, list[float]] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = s = t = 0.0
    for x, y in zip(a, b):
        dot += x * y
        s += x * x
        t += y * y
    if not s or not t:
        return 0.0
    return dot / ((s * t) ** 0.5)


def _ensure_vectors() -> None:
    """惰性初始化：首次调用时对负例+正例批量 embed（Ollama）。失败 → disabled（fail-open）。"""
    if _state["vecs"] is not None or _state["disabled"]:
        return
    with _state["init_lock"]:
        if _state["vecs"] is not None or _state["disabled"]:
            return
        if _embed is None:
            _state["disabled"] = True
            return
        vecs: list[list[float]] = []
        pos_vecs: list[list[float]] = []
        try:
            for ex in _ANALYSIS_EXAMPLES:
                vecs.append(_embed(ex))
            for ex in _REPORT_EXAMPLES:
                pos_vecs.append(_embed(ex))
        except Exception as e:
            _state["disabled"] = True
            print(f"[semantic_router] Ollama 不可用，语义闸关闭（fail-open）: {e}")
            return
        _state["vecs"] = vecs
        _state["pos_vecs"] = pos_vecs
        print(
            f"[semantic_router] 已加载 {len(vecs)} 条负例 + {len(pos_vecs)} 条正例向量"
            f"（风险阈值 {_THRESHOLD}）"
        )


def _embed_query(q: str) -> list[float] | None:
    """查询向量（带 LRU 缓存）。失败 → disabled（fail-open）并返回 None。"""
    vec = _query_cache.get(q)
    if vec is None:
        try:
            vec = _embed(q)
        except Exception:
            _state["disabled"] = True
            return None
        if len(_query_cache) >= _CACHE_SIZE:
            _query_cache.clear()
        _query_cache[q] = vec
    return vec


def analyze(question: str) -> dict:
    """输出双信号（辅助特征，不做裁决）：
    - sim_neg：与分析问句范例的最大相似度（≥ _THRESHOLD 即 risk 标记）
    - sim_pos：与报数问句范例的最大相似度（仅观测，不进决策）
    - risk：sim_neg 是否超过风险阈值（由调用方决定消费方式）
    任何异常 → 全零信号（fail-open，不阻塞主链路）。
    """
    q = (question or "").strip()
    empty = {"sim_neg": 0.0, "sim_pos": 0.0, "risk": False}
    if not q or _state["disabled"]:
        return empty
    _ensure_vectors()
    if _state["disabled"]:
        return empty
    vec = _embed_query(q)
    if vec is None:
        return empty
    sim_neg = max(_cosine(vec, ev) for ev in _state["vecs"])
    sim_pos = max(_cosine(vec, ev) for ev in (_state["pos_vecs"] or _state["vecs"]))
    return {"sim_neg": sim_neg, "sim_pos": sim_pos, "risk": sim_neg >= _THRESHOLD}


def is_analysis_like(question: str) -> bool:
    """问句与分析范例最大相似度 ≥ 阈值 → True（风险标记 → 建议降级 LLM）。

    定位：正则判"确定模板"后的**高置信兜底**——只抓极典型分析句，
    不是裁决器（bge 对中文短问句区分度有限，低阈值会误伤模板类）。
    """
    return analyze(question)["risk"]


def reset() -> None:
    """清空缓存（测试用）。"""
    _state["vecs"] = None
    _state["disabled"] = False
    _query_cache.clear()
