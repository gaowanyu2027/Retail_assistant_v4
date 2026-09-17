"""
查询历史向量召回
使用 Qdrant 本地模式 + Ollama bge-small-zh-v1.5
"""
import threading
import atexit
import os
import time
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from config.settings import (
    PROJECT_ROOT,
    VECTOR_BREAKER_COOLDOWN_SECONDS,
    VECTOR_BREAKER_FAILS,
    VECTOR_BULKHEAD_MAX_CONCURRENT,
    VECTOR_BULKHEAD_WAIT_SECONDS,
    VECTOR_EMBED_MAX_CHARS,
    VECTOR_SEARCH_DEFAULT_LIMIT,
)

COLLECTION_NAME = "query_history_vectors"
SESSION_SUMMARY_COLLECTION = "session_summary_vectors"
EMBED_MODEL = "qllama/bge-small-zh-v1.5"
# Ollama 嵌入服务地址（语义闸 + 向量召回共用 _embed()，所以这一处决定两者的可用性）。
#
# ⚠ 曾经**硬编码**为 http://127.0.0.1:11434，容器化后直接失效：
#   容器里的 127.0.0.1 是**容器自己**，不是宿主机。实测在容器内
#     127.0.0.1:11434      → Connection refused
#     host.docker.internal:11434 → 200 OK
#   后果是"语义闸 fail-open 关闭 + 向量召回退化为纯关键词"——
#   两者都只在日志里留一行提示，不报错，属于静默降级。
#
# 故改为环境变量可覆盖（与 QDRANT_URL 一致的处理方式）：
#   - 本机直跑：不设即可，默认值保持不变（向后兼容）
#   - 容器内跑：由 docker-compose 注入 host.docker.internal 地址
OLLAMA_EMBED_URL = os.environ.get(
    "OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embeddings"
)
VECTOR_SIZE = 512
# Qdrant 接入模式：配置 QDRANT_URL 时使用 Server 模式（Docker 部署），
# 否则回退本地嵌入式模式（项目原单进程方案，存 qdrant_data/）
QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
QDRANT_PATH = Path(
    os.environ.get("QDRANT_PATH") or (PROJECT_ROOT / "qdrant_data")
)

_client: QdrantClient | None = None
_client_lock = threading.Lock()


# ==================== 向量层熔断 ====================
#
# 与 `db_engine` 的 MySQL 熔断同思路（同一套阈值/冷却语义），原因也一模一样，
# 但这里的危害**实测更大**：Qdrant 停掉后并发 40 个 `/api/chat/search`：
#
#     向量检索本身   中位 86.2s、最大 120s（超时），只有 29/40 成功
#     **旁路接口**   `/api/chat/sessions`（与向量毫无关系、同样走 to_thread）
#                    中位 8ms，但**最大被拖到 52.2s**
#
# 根因有两层：① 每次调用都要白等 `_embed`(Ollama, 60s) 与 Qdrant(30s) 的超时；
# ② `_get_client()` 里的 `collection_exists()` 是**持 `_client_lock` 做的网络调用**，
#    于是并发请求在锁上排队，把"依赖故障"放大成"线程池被吃光 → 全站排队"。
#
# 策略：连续失败 N 次后进入冷却窗口，窗口内**直接快速失败**（不发任何网络请求），
# 冷却结束放一次探测，成功即复位。故障代价从"每次等超时"降到"首几次 + 之后瞬时"。
#
# ⚠ 范围说明：这里用**一个**熔断保护整个"向量能力"（Ollama 嵌入 + Qdrant 检索/写入），
# 而不是每个依赖一个 —— 因为对调用方来说能力是同一个（不可用就退化为关键词/SQL），
# 且两者任一挂掉，降级行为完全一致。若要更精细的区分（例如"嵌入挂了但检索还能用"），
# 拆成两个熔断即可，接口不用改。
_breaker_lock = threading.Lock()
_breaker_fails = 0
_breaker_until = 0.0
# 舱壁：限制**同时**进入向量层的调用数（信号量 + 有上限的等待）
_bulkhead = threading.BoundedSemaphore(VECTOR_BULKHEAD_MAX_CONCURRENT)


class VectorUnavailable(RuntimeError):
    """向量层熔断中：近期连续失败，冷却窗口内**不再尝试**（避免每次白等超时）。

    与"Qdrant 返回错误"区分：那些是单次失败，会照常降级；
    这个表示"我们已经知道它不可用"，直接走降级路径。
    """


def breaker_open() -> bool:
    """熔断是否处于打开（冷却）状态。"""
    with _breaker_lock:
        return time.time() < _breaker_until


def breaker_remaining() -> float:
    """剩余冷却秒数（未熔断时为 0）。"""
    with _breaker_lock:
        return max(0.0, _breaker_until - time.time())


def breaker_record(ok: bool) -> None:
    """记录一次向量调用结果，用于维护熔断状态。"""
    global _breaker_fails, _breaker_until
    with _breaker_lock:
        if ok:
            _breaker_fails = 0
            _breaker_until = 0.0
            return
        _breaker_fails += 1
        if _breaker_fails >= VECTOR_BREAKER_FAILS and time.time() >= _breaker_until:
            _breaker_until = time.time() + VECTOR_BREAKER_COOLDOWN_SECONDS
            print(f"[Vector] 向量层连续失败 {_breaker_fails} 次，"
                  f"熔断 {VECTOR_BREAKER_COOLDOWN_SECONDS:.0f}s"
                  f"（期间直接降级为关键词检索，不再白等超时）")


def breaker_reset() -> None:
    """手动复位熔断（测试/运维用）。"""
    global _breaker_fails, _breaker_until
    with _breaker_lock:
        _breaker_fails = 0
        _breaker_until = 0.0


def _breaker_guard(desc: str) -> None:
    """冷却期内直接抛 `VectorUnavailable`（**不发任何网络请求**）。"""
    remain = breaker_remaining()
    if remain > 0:
        raise VectorUnavailable(
            f"向量层熔断中（{desc}），剩余冷却 {remain:.0f}s —— 直接降级，不等待超时"
        )


def _qdrant(desc: str, fn, *args, **kwargs):
    """带**熔断 + 舱壁**的向量层调用（Ollama / Qdrant 都走它）。

    两层保护各管一件事，缺一不可：
    - **熔断**：管"稳态故障" —— 连续失败 N 次后，后续请求在冷却期内直接降级；
    - **舱壁**：管"同一瞬间的突发" —— 熔断打开之前可能已有几十个请求同时涌入，
      它们会一直占着线程池 worker 等超时（实测把**旁路接口**拖到 78s）。
      这里限制同时进入的调用数，拿不到许可就**立即降级**而不是排队。

    成功清零失败计数；失败累加，达到阈值即进入冷却。
    """
    _breaker_guard(desc)
    if not _bulkhead.acquire(timeout=VECTOR_BULKHEAD_WAIT_SECONDS):
        raise VectorUnavailable(
            f"向量层并发已达上限（{VECTOR_BULKHEAD_MAX_CONCURRENT}，等 "
            f"{VECTOR_BULKHEAD_WAIT_SECONDS:.1f}s 未获许可）—— {desc} 直接降级，不排队"
        )
    try:
        out = fn(*args, **kwargs)
    except Exception:
        breaker_record(False)
        raise
    finally:
        _bulkhead.release()
    breaker_record(True)
    return out


def _close_client():
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


atexit.register(_close_client)


def _smart_slice(text: str, max_tokens: int = 400, hard_limit: int = 500) -> str:
    """智能切片（bge-small-zh 512 token 上限的工程防护）。

    策略（按优先级）：
    1. 估算 token（中文 1 字符≈1 token，len 近似）≤ max_tokens → 不切
    2. 超过时优先按段落（换行）切：逐段累加至接近 max_tokens
    3. 单段仍超限 → 在 [max_tokens, hard_limit] 区间内按最近句号切（不切断句子）
    4. 区间内无句号 → 硬切到 hard_limit（兜底，仍 < 512 防 Ollama 500）
    """
    if len(text) <= max_tokens:
        return text

    # 2) 段落优先（换行分割）
    if "\n" in text:
        acc = ""
        for part in text.split("\n"):
            candidate = f"{acc}\n{part}" if acc else part
            if len(candidate) > hard_limit:
                break
            acc = candidate
            if len(acc) >= max_tokens * 0.8:  # 接近上限即停，保留余量
                break
        if acc and len(acc) <= hard_limit:
            return acc

    # 3) 句号就近切（在 [max_tokens, hard_limit) 内找最近句号，避免切断句子）
    for end in range(max_tokens, min(hard_limit, len(text))):
        if text[end] in "。！？．!?；;":
            return text[: end + 1]
    # 4) 硬切兜底
    return text[:hard_limit]


def _embed(text: str) -> list[float]:
    clean_text = (text or "").strip()
    if not clean_text:
        clean_text = "空"
    # 清洗：移除 emoji 与非 BMP 符号（Ollama bge-small-zh 对 emoji 输入返回 500，
    # 实测含 emoji 的回答会导致整条向量化失败）
    clean_text = "".join(
        ch for ch in clean_text
        if ord(ch) <= 0xFFFF and not (0x1F000 <= ord(ch) <= 0x1FAFF)
    )
    if not clean_text:
        clean_text = "空"
    resp = _qdrant(
        "embed",
        httpx.post,
        OLLAMA_EMBED_URL,
        json={"model": EMBED_MODEL, "prompt": _smart_slice(clean_text)},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    embedding = data.get("embedding")
    if not embedding and isinstance(data.get("data"), list) and data["data"]:
        embedding = data["data"][0].get("embedding")
    if not embedding:
        raise RuntimeError("Ollama embedding 返回为空")
    return embedding


def _get_client() -> QdrantClient:
    global _client
    # 熔断冷却期内**连客户端都不建**（建客户端/探测 collection 都是网络调用，
    # 而这正是 Qdrant 不可用时的第一个失败点）。
    _breaker_guard("get_client")
    with _client_lock:
        if _client is None:
            if QDRANT_URL:
                # Server 模式（Docker 容器部署）：解除单进程锁，支持多实例
                _client = QdrantClient(
                    url=QDRANT_URL,
                    api_key=QDRANT_API_KEY or None,
                    timeout=30,
                )
            else:
                # 本地嵌入式模式（单进程，原方案）
                data_dir = QDRANT_PATH
                data_dir.mkdir(parents=True, exist_ok=True)
                _client = QdrantClient(path=str(data_dir))
        for collection in (COLLECTION_NAME, SESSION_SUMMARY_COLLECTION):
            if not _client.collection_exists(collection):
                _client.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(
                        size=VECTOR_SIZE,
                        distance=models.Distance.COSINE,
                    ),
                )
        return _client


def upsert_message(
    message_id: int,
    session_id: str,
    seq_no: int,
    title: str,
    question: str,
    answer: str,
    created_at: str,
    owner: str = "",
):
    """将一条查询历史写入向量库。

    `owner`（B3）：归属账号，写进 payload 供**检索时过滤**。
    修复前 payload 里没有归属，`search_messages` 会召回**所有人**的历史问答 ——
    而这条路径的检索结果会直接进入 LLM 上下文，是最隐蔽的泄露点。
    """
    vector = _embed(f"{title}\n{question}\n{answer}")
    client = _qdrant("get_client", _get_client)
    with _client_lock:
        _qdrant("upsert", client.upsert,
            collection_name=COLLECTION_NAME,
            points=[
                models.PointStruct(
                    id=int(message_id),
                    vector=vector,
                    payload={
                        "message_id": int(message_id),
                        "session_id": session_id,
                        "seq_no": int(seq_no or 0),
                        "title": title,
                        "question": question,
                        "answer": answer or "",
                        "created_at": created_at,
                        "owner": owner or "",
                    },
                )
            ],
        )


def delete_message(message_id: int):
    """从向量库删除一条消息。"""
    client = _qdrant("get_client", _get_client)
    with _client_lock:
        _qdrant("delete", client.delete,
            collection_name=COLLECTION_NAME,
            points_selector=models.PointIdsList(points=[int(message_id)]),
        )


def _owner_filter(owner: str | None):
    """构造归属过滤条件（B3）。`None` = 不限定（审计视角）。"""
    if owner is None:
        return None
    return models.Filter(
        must=[models.FieldCondition(key="owner", match=models.MatchValue(value=owner or ""))]
    )


def search_messages(query: str, limit: int = VECTOR_SEARCH_DEFAULT_LIMIT, owner: str | None = None):
    """按语义相似度搜索查询历史。

    `owner`（B3）：只召回该归属的历史；`None` = 不限定（审计视角）。
    注意历史向量点里没有 `owner` 字段（迁移前写入），带过滤时**不会**被召回 ——
    这正是期望行为：无归属数据只有审计视角能看到。
    """
    query = (query or "").strip()
    if not query:
        return []
    vector = _embed(query)
    client = _qdrant("get_client", _get_client)
    with _client_lock:
        response = _qdrant("query_points", client.query_points,
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=limit,
            with_payload=True,
            query_filter=_owner_filter(owner),
        )
        hits = response.points if response else []
    results = []
    for hit in hits:
        payload = hit.payload or {}
        results.append({
            "session_id": payload.get("session_id", ""),
            "title": payload.get("title", ""),
            "seq_no": payload.get("seq_no", 0),
            "question": payload.get("question", ""),
            "answer": payload.get("answer", ""),
            "created_at": payload.get("created_at", ""),
            "owner": payload.get("owner", ""),
            "score": round(float(hit.score), 4),
        })
    return results


def _summary_point_id(session_id: str) -> str:
    """把 session_id 映射为稳定的 UUID（Qdrant point id 要求）。"""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ltm:{session_id}"))


def upsert_session_summary(session_id: str, summary: str, keywords: str = "", owner: str = ""):
    """把一条长期记忆摘要写入向量索引（摘要索引层）。

    `owner`（B3）：同 `upsert_message`，摘要含用户问答内容，必须带归属。
    失败时（Ollama/Qdrant 不可用）自动降级，不影响主流程。
    """
    try:
        vector = _embed(f"{keywords}\n{summary}")
        client = _qdrant("get_client", _get_client)
        with _client_lock:
            _qdrant("upsert", client.upsert,
                collection_name=SESSION_SUMMARY_COLLECTION,
                points=[
                    models.PointStruct(
                        id=_summary_point_id(session_id),
                        vector=vector,
                        payload={
                            "session_id": session_id,
                            "summary": summary,
                            "keywords": keywords,
                            "owner": owner or "",
                        },
                    )
                ],
            )
    except Exception as e:
        print(f"[Vector] 摘要向量索引失败 session={session_id}: {e}")


def delete_session_summary(session_id: str):
    """从摘要索引删除一条长期记忆。"""
    try:
        client = _qdrant("get_client", _get_client)
        with _client_lock:
            _qdrant("delete", client.delete,
                collection_name=SESSION_SUMMARY_COLLECTION,
                points_selector=models.PointIdsList(points=[_summary_point_id(session_id)]),
            )
    except Exception as e:
        print(f"[Vector] 摘要向量删除失败 session={session_id}: {e}")


def search_session_summaries(query: str, limit: int = 3, owner: str | None = None) -> list[dict]:
    """按语义相似度检索长期记忆摘要（跨会话记忆的向量索引）。

    `owner`（B3）：只召回该归属的摘要；`None` = 不限定（审计视角）。
    ⚠ 当前仓库内暂无调用方，一并加上过滤以免将来接上时漏掉归属。
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        vector = _embed(query)
        client = _qdrant("get_client", _get_client)
        with _client_lock:
            response = _qdrant("query_points", client.query_points,
                collection_name=SESSION_SUMMARY_COLLECTION,
                query=vector,
                limit=limit,
                with_payload=True,
                query_filter=_owner_filter(owner),
            )
            hits = response.points if response else []
        results = []
        for hit in hits:
            payload = hit.payload or {}
            results.append({
                "session_id": payload.get("session_id", ""),
                "summary": payload.get("summary", ""),
                "keywords": payload.get("keywords", ""),
                "owner": payload.get("owner", ""),
                "score": round(float(hit.score), 4),
            })
        return results
    except Exception as e:
        print(f"[Vector] 摘要向量检索失败: {e}")
        return []


def _recency_factor(created_at: str, halflife_days: float = 7.0) -> float:
    """时间衰减：越新的记录权重越高（半衰期默认 7 天）。"""
    if not created_at:
        return 1.0
    try:
        from datetime import datetime
        dt = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
        days = (datetime.now() - dt).total_seconds() / 86400.0
        if days < 0:
            return 1.0
        return 0.5 ** (days / halflife_days)
    except Exception:
        return 1.0


def search_messages_hybrid(
    query: str,
    limit: int = VECTOR_SEARCH_DEFAULT_LIMIT,
    keyword_limit: int = 10,
    owner: str | None = None,
):
    """混合检索：MySQL 关键词精确匹配 + Qdrant 向量语义匹配，
    统一按（相关性 × 时间衰减）排序，任一通道不可用时自动降级。

    `owner`（B3）：**两个通道都要**按归属过滤（漏一个就是泄露）。`None` = 审计视角。
    返回结构与 search_messages 一致（含 score 字段）。
    """
    query = (query or "").strip()
    if not query:
        return []

    kw_hits: list[dict] = []
    try:
        import mysql_db
        for r in mysql_db.search_chat_messages(query, limit=keyword_limit, owner=owner):
            kw_hits.append({
                "session_id": r.get("session_id", ""),
                "title": r.get("title", ""),
                "seq_no": r.get("seq_no", 0),
                "question": r.get("question", ""),
                "answer": r.get("answer", ""),
                "created_at": r.get("created_at", ""),
                "score": 1.0,  # 精确命中视为满相关
            })
    except Exception as e:
        print(f"[Vector] 关键词检索失败，仅用向量召回: {e}")

    vec_hits: list[dict] = []
    try:
        vec_hits = search_messages(query, limit=limit * 2, owner=owner)
    except Exception as e:
        print(f"[Vector] 向量检索失败，仅用关键词召回: {e}")

    # 去重合并（同一会话同一问题只保留一条，向量命中优先保留）
    merged: dict[tuple, dict] = {}
    for h in vec_hits:
        merged[(h["session_id"], h["question"])] = h
    for h in kw_hits:
        key = (h["session_id"], h["question"])
        if key not in merged:
            merged[key] = h

    items = []
    for h in merged.values():
        rec = _recency_factor(h.get("created_at", ""))
        items.append({**h, "score": round(float(h.get("score", 0)) * rec, 4)})
    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:limit]


def reindex_all(owner: str | None = None) -> int:
    """把 MySQL 中的查询历史重建到向量库（逐条容错：单条失败跳过并统计，不中断整体）。

    `owner=None`（默认）重建**全部账号**的数据，这是运维/审计场景；
    重建时会把库里的归属写进向量 payload（B3），否则检索过滤会把老数据全挡在外面。
    """
    import mysql_db
    records = mysql_db.get_all_query_history_records(owner=owner)
    count = 0
    failed = 0
    for record in records:
        try:
            upsert_message(
                message_id=record["message_id"],
                session_id=record["session_id"],
                seq_no=record["seq_no"],
                title=record.get("title", ""),
                question=record["question"],
                answer=record.get("answer", ""),
                created_at=record.get("created_at", ""),
                owner=record.get("owner", ""),
            )
            count += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"[Vector] reindex 跳过 id={record['message_id']}: {e}")
    if failed:
        print(f"[Vector] reindex 完成: 成功 {count} 条，失败跳过 {failed} 条")
    return count
