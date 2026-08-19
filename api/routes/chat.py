"""会话记录管理 API"""
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
            results = vector_memory.search_messages(q, limit=CHAT_VECTOR_SEARCH_LIMIT)
            if results:
                return {"results": results, "mode": "vector"}
        except Exception as e:
            print(f"[Vector] 向量搜索失败，回退 SQL: {e}")

    try:
        import mysql_db
        results = mysql_db.search_chat_messages(q)
        return {"results": results, "mode": "sql"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话搜索失败: {e}")


@router.get("/sessions")
async def list_sessions():
    """列出会话记录。"""
    try:
        import mysql_db
        return {"sessions": mysql_db.list_chat_sessions()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话列表获取失败: {e}")


@router.post("/sessions")
async def create_session(payload: SessionCreate):
    """新建会话。"""
    session_id = "sess_" + uuid.uuid4().hex[:16]
    try:
        import mysql_db
        mysql_db.create_chat_session(session_id, payload.title)
        return {"session_id": session_id, "title": payload.title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话创建失败: {e}")


@router.post("/sessions/{session_id}/save")
async def save_session(session_id: str, payload: SessionSave | None = None):
    """保存会话标题/更新时间。"""
    try:
        import mysql_db
        mysql_db.save_chat_session(session_id, payload.title if payload else None)
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
        mysql_db.save_chat_session(session_id, title)
        return {"status": "ok", "session_id": session_id, "title": title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话重命名失败: {e}")


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话、消息，并清理对应向量。"""
    try:
        import mysql_db
        import vector_memory
        message_ids = mysql_db.delete_chat_session(session_id)
        for message_id in message_ids:
            try:
                vector_memory.delete_message(message_id)
            except Exception as e:
                print(f"[Vector] 删除向量失败 message_id={message_id}: {e}")
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
            "messages": mysql_db.get_chat_messages(session_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"消息获取失败: {e}")
