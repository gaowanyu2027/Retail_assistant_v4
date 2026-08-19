"""
POST /query — 自然语言查询接口（LangChain Agent 版）

异步策略：
- 意图路由器把常见数据类问题直接模板回答（零 LLM，毫秒级）
- 其余走 Agent：_get_agent/向量召回/持久化等同步阻塞 IO 全部丢线程池，
  LLM 调用用 ainvoke/astream_events，不阻塞事件循环
"""
import asyncio
import threading
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from api.schemas import QueryRequest, QueryResponse
from config.settings import QUERY_SESSION_TTL
from agents.intent_router import route_intent

router = APIRouter(tags=["query"])

# ===== 会话级 Agent 缓存（按 session_id 复用，保持对话记忆） =====
_agent_cache: dict[str, "MasterAgent"] = {}     # type: ignore
_agent_last_used: dict[str, float] = {}
_agent_lock = threading.Lock()
SESSION_TTL = QUERY_SESSION_TTL


def _get_agent(session_id: str):
    """获取或创建 Agent 实例（按 session_id 复用，保持持久化记忆）"""
    global _agent_cache, _agent_last_used

    now = time.time()
    # 先查已有（读路径不加锁，命中即返回）
    if session_id in _agent_cache:
        _agent_last_used[session_id] = now
        return _agent_cache[session_id]

    with _agent_lock:
        # 双重检查
        if session_id in _agent_cache:
            _agent_last_used[session_id] = now
            return _agent_cache[session_id]

        # 清理过期会话（必须在锁内：并发请求在迭代时修改字典会抛
        # RuntimeError: dictionary changed size during iteration）
        expired = [sid for sid, t in _agent_last_used.items() if now - t > SESSION_TTL]
        for sid in expired:
            _agent_cache.pop(sid, None)
            _agent_last_used.pop(sid, None)

        from api.dependencies import get_popularity_skill, get_anomaly_skill, get_emotion_skill
        from agents.master_agent import MasterAgent

        pop_skill = get_popularity_skill()
        anom_skill = get_anomaly_skill()
        emo_skill = get_emotion_skill()

        agent = MasterAgent(popularity_skill=pop_skill, anomaly_skill=anom_skill, emotion_skill=emo_skill)
        _agent_cache[session_id] = agent
        _agent_last_used[session_id] = now
        return agent


def get_agent(session_id: str):
    """供语音等接口复用的 Agent 获取入口。"""
    return _get_agent(session_id)


async def _aget_agent(session_id: str):
    """异步获取 Agent：创建过程（skill 懒加载/checkpointer 探测）丢线程池。"""
    return await asyncio.to_thread(_get_agent, session_id)


def _persist_query_history(session_id: str, question: str, answer: str, intent: str = "general"):
    """写入 MySQL 查询历史，并同步到向量库。

    会话首条消息（seq_no == 1）时用 LLM 生成简短会话标题（失败回退问题前 N 字）。
    注意：包含 MySQL 与可能的 LLM 标题调用，调用方应通过 asyncio.to_thread 放入线程池。
    """
    try:
        import mysql_db
        import vector_memory
        from datetime import datetime

        message_id, seq_no = mysql_db.save_query_history(
            session_id=session_id,
            question=question,
            answer=answer,
            intent=intent,
        )

        # 标题：首条消息用 LLM 生成，其余沿用已有标题
        title = question.strip()[:60]
        if seq_no == 1:
            try:
                from agents.base_agent import generate_session_title
                title = generate_session_title(question)
                mysql_db.update_chat_session_title(session_id, title)
            except Exception as e:
                print(f"[Agent] 会话标题更新失败: {e}")

        vector_memory.upsert_message(
            message_id=message_id,
            session_id=session_id,
            seq_no=seq_no,
            title=title,
            question=question,
            answer=answer,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as e:
        print(f"[Vector] 查询历史持久化失败: {e}")


# ===== 后台持久化（fire-and-forget，性能关键） =====
# 压测实证（benchmark/loadtest.py）：模板请求同步 await 持久化时，MySQL 写 +
# Ollama embedding 串行排队，毫秒级模板被拖成 avg 1.2s、QPS 仅 ~22。
# 改为 create_task 后台执行 + 信号量限流（防 Ollama/MySQL 被并发打爆），
# 响应立即返回；持久化失败仅打印告警，不影响主链路。
_persist_sem = asyncio.Semaphore(16)


def fire_persist(session_id: str, question: str, answer: str, intent: str = "general"):
    """后台异步持久化：不阻塞响应。限流 16 并发，超出排队。"""
    async def _do():
        try:
            async with _persist_sem:
                await asyncio.to_thread(_persist_query_history, session_id, question, answer, intent)
        except Exception as e:
            print(f"[Persist] 后台持久化失败: {e}")

    asyncio.create_task(_do())


@router.post("/queries", response_model=QueryResponse)
async def create_query(request: QueryRequest):
    """处理自然语言查询（非流式）— 创建一次查询并返回结果。

    RESTful：查询作为资源，POST /api/queries 提交问题。
    """
    try:
        agent = await _aget_agent(request.session_id)

        # 意图路由：数据类问题直接模板回答（零 LLM）
        intent = route_intent(request.question)
        quick = await asyncio.to_thread(
            agent.quick_answer, intent, request.question, request.session_id
        )
        if quick is not None:
            fire_persist(
                request.session_id, request.question,
                quick.get("answer", ""), quick.get("intent", "general"),
            )
            return QueryResponse(**quick)

        # 完整 Agent 流程（ahandle_query：向量召回 in 线程池 + LLM ainvoke）
        result = await agent.ahandle_query(
            query=request.question,
            context=request.context,
            session_id=request.session_id,
        )
        fire_persist(
            request.session_id, request.question,
            result.get("answer", ""), result.get("intent", "general"),
        )
        return QueryResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询处理失败: {str(e)}")


@router.post("/queries/stream")
async def create_query_stream(request: QueryRequest):
    """处理自然语言查询 — SSE 流式输出（查询资源的流式变体）

    前端使用 fetch + ReadableStream 逐 token 渲染，实现打字效果。
    意图路由命中的数据类问题直接一次性返回模板回答。
    """
    try:
        agent = await _aget_agent(request.session_id)

        # 意图路由：数据类问题直接模板回答（零 LLM）
        intent = route_intent(request.question)
        quick = await asyncio.to_thread(
            agent.quick_answer, intent, request.question, request.session_id
        )
        if quick is not None:
            async def quick_generator():
                yield f"data: {quick.get('answer', '')}\n\n"
                yield "data: [DONE]\n\n"
                fire_persist(
                    request.session_id, request.question,
                    quick.get("answer", ""), quick.get("intent", "general"),
                )

            return StreamingResponse(
                quick_generator(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def token_generator():
            full_answer = ""
            async for token in agent.handle_query_stream(
                query=request.question,
                session_id=request.session_id,
            ):
                # SSE 格式: data: <token>\n\n
                full_answer += token
                yield f"data: {token}\n\n"
            yield "data: [DONE]\n\n"
            # 持久化（MySQL + 可能的首条 LLM 标题）后台执行，不阻塞流结束
            fire_persist(
                request.session_id, request.question,
                full_answer, "general",
            )

        return StreamingResponse(
            token_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"流式查询失败: {str(e)}")


# ==================== 兼容别名（旧动作路径，deprecated，仅供旧客户端） ====================

@router.post("/query", response_model=QueryResponse, include_in_schema=False)
async def handle_query(request: QueryRequest):
    """兼容别名：POST /api/query（旧路径）→ 等价于 POST /api/queries"""
    return await create_query(request)


@router.post("/query/stream", include_in_schema=False)
async def handle_query_stream(request: QueryRequest):
    """兼容别名：POST /api/query/stream（旧路径）→ 等价于 POST /api/queries/stream"""
    return await create_query_stream(request)
