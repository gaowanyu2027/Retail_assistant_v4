"""
查询历史向量召回
使用 Qdrant 本地模式 + Ollama bge-small-zh-v1.5
"""
import threading
import atexit
import os
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from config.settings import (
    PROJECT_ROOT,
    VECTOR_EMBED_MAX_CHARS,
    VECTOR_SEARCH_DEFAULT_LIMIT,
)

COLLECTION_NAME = "query_history_vectors"
SESSION_SUMMARY_COLLECTION = "session_summary_vectors"
EMBED_MODEL = "qllama/bge-small-zh-v1.5"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
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
    resp = httpx.post(
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
):
    """将一条查询历史写入向量库。"""
    vector = _embed(f"{title}\n{question}\n{answer}")
    client = _get_client()
    with _client_lock:
        client.upsert(
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
                    },
                )
            ],
        )


def delete_message(message_id: int):
    """从向量库删除一条消息。"""
    client = _get_client()
    with _client_lock:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=models.PointIdsList(points=[int(message_id)]),
        )


def search_messages(query: str, limit: int = VECTOR_SEARCH_DEFAULT_LIMIT):
    """按语义相似度搜索查询历史。"""
    query = (query or "").strip()
    if not query:
        return []
    vector = _embed(query)
    client = _get_client()
    with _client_lock:
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=limit,
            with_payload=True,
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
            "score": round(float(hit.score), 4),
        })
    return results


def _summary_point_id(session_id: str) -> str:
    """把 session_id 映射为稳定的 UUID（Qdrant point id 要求）。"""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ltm:{session_id}"))


def upsert_session_summary(session_id: str, summary: str, keywords: str = ""):
    """把一条长期记忆摘要写入向量索引（摘要索引层）。

    失败时（Ollama/Qdrant 不可用）自动降级，不影响主流程。
    """
    try:
        vector = _embed(f"{keywords}\n{summary}")
        client = _get_client()
        with _client_lock:
            client.upsert(
                collection_name=SESSION_SUMMARY_COLLECTION,
                points=[
                    models.PointStruct(
                        id=_summary_point_id(session_id),
                        vector=vector,
                        payload={
                            "session_id": session_id,
                            "summary": summary,
                            "keywords": keywords,
                        },
                    )
                ],
            )
    except Exception as e:
        print(f"[Vector] 摘要向量索引失败 session={session_id}: {e}")


def delete_session_summary(session_id: str):
    """从摘要索引删除一条长期记忆。"""
    try:
        client = _get_client()
        with _client_lock:
            client.delete(
                collection_name=SESSION_SUMMARY_COLLECTION,
                points_selector=models.PointIdsList(points=[_summary_point_id(session_id)]),
            )
    except Exception as e:
        print(f"[Vector] 摘要向量删除失败 session={session_id}: {e}")


def search_session_summaries(query: str, limit: int = 3) -> list[dict]:
    """按语义相似度检索长期记忆摘要（跨会话记忆的向量索引）。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        vector = _embed(query)
        client = _get_client()
        with _client_lock:
            response = client.query_points(
                collection_name=SESSION_SUMMARY_COLLECTION,
                query=vector,
                limit=limit,
                with_payload=True,
            )
            hits = response.points if response else []
        results = []
        for hit in hits:
            payload = hit.payload or {}
            results.append({
                "session_id": payload.get("session_id", ""),
                "summary": payload.get("summary", ""),
                "keywords": payload.get("keywords", ""),
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
):
    """混合检索：MySQL 关键词精确匹配 + Qdrant 向量语义匹配，
    统一按（相关性 × 时间衰减）排序，任一通道不可用时自动降级。

    返回结构与 search_messages 一致（含 score 字段）。
    """
    query = (query or "").strip()
    if not query:
        return []

    kw_hits: list[dict] = []
    try:
        import mysql_db
        for r in mysql_db.search_chat_messages(query, limit=keyword_limit):
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
        vec_hits = search_messages(query, limit=limit * 2)
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


def reindex_all() -> int:
    """把 MySQL 中全部查询历史重建到向量库（逐条容错：单条失败跳过并统计，不中断整体）。"""
    import mysql_db
    records = mysql_db.get_all_query_history_records()
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
            )
            count += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"[Vector] reindex 跳过 id={record['message_id']}: {e}")
    if failed:
        print(f"[Vector] reindex 完成: 成功 {count} 条，失败跳过 {failed} 条")
    return count
