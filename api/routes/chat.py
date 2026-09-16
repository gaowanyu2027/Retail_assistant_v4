"""会话记录管理 API

⚠ 性能约定：本文件所有路由都是 `async def`，因此**绝不能**在路由体里直接调用
同步的 MySQL / Qdrant / Ollama 函数——那会在事件循环线程上阻塞，后果是同时刻的
视频帧推送、SSE 流、心跳全部停摆（最坏情况：向量搜索走到 `httpx.post(timeout=60)`，
全站 60 秒无响应）。

项目其它路由（`analytics.py` / `maps.py` / `tts.py` / `query.py`）都已统一用
`await asyncio.to_thread(...)`，本文件此前是**遗漏**，现已对齐。
"""
import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from config.settings import CHAT_VECTOR_SEARCH_LIMIT

router = APIRouter(prefix="/chat", tags=["chat"])


class SessionCreate(BaseModel):
    title: str = "新会话"


class SessionSave(BaseModel):
    title: str | None = None


class SessionRename(BaseModel):
    title: str


@router.get("/search")
async def search_chat(q: str = Query(default="")):
    """搜索会话标题、问题或回答内容。"""
    if q and q.strip():
        try:
            import vector_memory

            # 向量搜索内部会调 Ollama 做 embedding（同步 httpx），必须放线程池
            results = await asyncio.to_thread(
                lambda: vector_memory.search_messages(q, limit=CHAT_VECTOR_SEARCH_LIMIT)
            )
            if results:
                return {"results": results, "mode": "vector"}
        except Exception as e:
            print(f"[Vector] 向量搜索失败，回退 SQL: {e}")

    try:
        import mysql_db

        # LIKE '%kw%' 三列全表扫描，放线程池避免冻住事件循环
        results = await asyncio.to_thread(mysql_db.search_chat_messages, q)
        return {"results": results, "mode": "sql"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话搜索失败: {e}")


@router.get("/sessions")
async def list_sessions():
    """列出会话记录。"""
    try:
        import mysql_db

        return {"sessions": await asyncio.to_thread(mysql_db.list_chat_sessions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话列表获取失败: {e}")


@router.post("/sessions")
async def create_session(payload: SessionCreate):
    """新建会话。"""
    session_id = "sess_" + uuid.uuid4().hex[:16]
    try:
        import mysql_db

        await asyncio.to_thread(mysql_db.create_chat_session, session_id, payload.title)
        return {"session_id": session_id, "title": payload.title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话创建失败: {e}")


@router.post("/sessions/{session_id}/save")
async def save_session(session_id: str, payload: SessionSave | None = None):
    """保存会话标题/更新时间。"""
    try:
        import mysql_db

        await asyncio.to_thread(
            mysql_db.save_chat_session, session_id, payload.title if payload else None
        )
        return {"status": "ok", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话保存失败: {e}")


@router.put("/sessions/{session_id}/rename")
async def rename_session(session_id: str, payload: SessionRename):
    """重命名会话标题。"""
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    try:
        import mysql_db

        await asyncio.to_thread(mysql_db.save_chat_session, session_id, title)
        return {"status": "ok", "session_id": session_id, "title": title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话重命名失败: {e}")


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话、消息，并清理对应向量。"""
    def _delete_all() -> list:
        """删库 + 逐条删向量。整段放线程池：

        逐条删向量是多次同步 HTTP 往返，若在事件循环里 await 每一条，
        不仅阻塞循环还会产生 N 次协程切换开销。
        """
        import mysql_db
        import vector_memory

        message_ids = mysql_db.delete_chat_session(session_id)
        for message_id in message_ids:
            try:
                vector_memory.delete_message(message_id)
            except Exception as e:
                print(f"[Vector] 删除向量失败 message_id={message_id}: {e}")
        return message_ids

    try:
        message_ids = await asyncio.to_thread(_delete_all)
        return {
            "status": "ok",
            "session_id": session_id,
            "deleted_messages": len(message_ids),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话删除失败: {e}")


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """获取指定会话消息。"""
    try:
        import mysql_db

        return {
            "session_id": session_id,
            "messages": await asyncio.to_thread(mysql_db.get_chat_messages, session_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"消息获取失败: {e}")
