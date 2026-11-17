import asyncio
import json
import logging
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.dependencies import current_user
from app.config import SSE_HEARTBEAT_INTERVAL
from app.models.chat import ChatRequest
from app.services.chat_service import generate_response
from app.services.feedback_service import (
    FeedbackBackendError, cancel_feedback_shared, resolve_feedback_shared,
)
from app.services.chat_runtime import (
    ChatRun, RunBusy, CoordinationUnavailable, authorize_active_run, request_stop,
)
from app.dao.user_dao import fire_notify_mng_active

logger = logging.getLogger(__name__)
router = APIRouter()
_SSE_HEARTBEAT = ": ping\n\n"
_DONE = object()
# 客户端断连后仍要在后台跑完并落库的生成任务：必须持强引用，
# 否则任务在挂起期间可能被 GC（asyncio 只对任务持弱引用）。
_DETACHED_TASKS = set()


async def _with_heartbeat(source, cancel_event=None, on_finish=None, on_detach=None):
    """Pump SSE without cancelling an upstream await on heartbeat timeout.

    A separate cancellation watcher can interrupt silent LLM/tool/feedback waits;
    generate_response still owns partial-history persistence and user_abort events.

    When the client goes away on its own (no user stop), the upstream generation is
    left running to completion so the round is still persisted; on_finish then runs
    once it truly finishes, in the background task that took the generation over.

    on_detach receives that background task, letting the caller keep its own
    teardown fallback from firing while the generation is still running.
    """
    if SSE_HEARTBEAT_INTERVAL <= 0 and cancel_event is None and on_finish is None and on_detach is None:
        async with aclosing(source):
            async for ev in source:
                yield ev
        return
    queue = asyncio.Queue()
    # 客户端断连：生成任务不再入队（避免队列无限增长），但继续跑完并落库
    detached = asyncio.Event()
    finish_task = None

    async def finish():
        """收尾（清理会话运行态）必须执行完，不能被调用方取消传播打断。"""
        nonlocal finish_task
        if on_finish is None:
            return
        if finish_task is None:
            finish_task = asyncio.create_task(on_finish())
            _DETACHED_TASKS.add(finish_task)
            finish_task.add_done_callback(_DETACHED_TASKS.discard)
        try:
            await asyncio.shield(finish_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[chat] SSE 收尾失败", exc_info=True)

    async def pump():
        try:
            async with aclosing(source):
                async for ev in source:
                    if detached.is_set():
                        continue
                    await queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if detached.is_set():
                logger.warning("[chat] 客户端断连后生成阶段异常", exc_info=True)
            else:
                await queue.put(exc)
        finally:
            if detached.is_set():
                await finish()
            else:
                await queue.put(_DONE)

    pump_task = asyncio.create_task(pump())

    async def stop_pump():
        await cancel_event.wait()
        pump_task.cancel()

    stopper = asyncio.create_task(stop_pump()) if cancel_event is not None else None

    def pump_done(task):
        if stopper is not None:
            stopper.cancel()
        # Also covers cancellation before pump() first executes, and a source
        # finishing just as the consumer disconnects.
        queue.put_nowait(_DONE)
        if detached.is_set():
            cleanup_task = asyncio.create_task(finish())
            _DETACHED_TASKS.add(cleanup_task)
            cleanup_task.add_done_callback(_DETACHED_TASKS.discard)

    pump_task.add_done_callback(pump_done)
    try:
        while True:
            try:
                if SSE_HEARTBEAT_INTERVAL > 0:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_INTERVAL)
                else:
                    item = await queue.get()
            except asyncio.TimeoutError:
                yield _SSE_HEARTBEAT
                continue
            if item is _DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    except (asyncio.CancelledError, GeneratorExit):
        if cancel_event is None or not cancel_event.is_set():
            # 非用户停止的取消（客户端断连/请求取消）→ 放走生成任务，
            # 让它在后台跑完并落库，收尾由 pump 结束时执行
            detached.set()
            _DETACHED_TASKS.add(pump_task)
            pump_task.add_done_callback(_DETACHED_TASKS.discard)
            if on_detach is not None:
                on_detach(pump_task)
        raise
    finally:
        if not detached.is_set():
            if stopper is not None:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            await finish()
        elif pump_task.done():
            await finish()


class StopRequest(BaseModel):
    session_id: str


class FeedbackRequest(BaseModel):
    session_id: str
    feedback_id: str
    action: str
    payload: dict = {}


@router.post("/chat")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(current_user)):
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        jwt_token = authorization.split(" ", 1)[1].strip()
        if jwt_token:
            fire_notify_mng_active(jwt_token)
    session_service = request.app.state.session_service
    user_id = user.get("user_id")
    session_id = await session_service.get_or_create_session(body.session_id, user_id)
    tasks = request.app.state.chat_tasks
    if session_id in tasks:
        raise HTTPException(status_code=409, detail="该会话正在生成，请等待当前轮结束")
    run = ChatRun(getattr(request.app.state, "redis_client", None), session_id, user_id)
    try:
        await run.open()
    except RunBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CoordinationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    tasks[session_id] = run

    # 客户端断连后接管生成的后台任务：生成仍在跑时收尾由它负责，
    # 这里只记录任务对象供 background 兜底判断（见 cleanup）
    detached_task = None
    watcher = None
    cleanup_task = None

    def note_detached(task):
        nonlocal detached_task
        detached_task = task

    async def cleanup(background: bool = False):
        nonlocal cleanup_task
        # 断连后生成转入后台继续跑：此时（ASGI 后台任务）收尾会提前释放租约并把
        # 会话移出运行表，同一会话就能被并发开启新一轮（single-flight 失效），
        # 同时还会提前取消正在等待的用户反馈。生成真结束时由它自己收尾。
        if background and detached_task is not None and not detached_task.done():
            return
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(cleanup_run())
        await asyncio.shield(cleanup_task)

    async def cleanup_run():
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        try:
            await cancel_feedback_shared(session_id)
        except Exception:
            logger.warning("[chat] 清理反馈失败", exc_info=True)
        finally:
            if tasks.get(session_id) is run:
                tasks.pop(session_id, None)
            await run.close()

    async def source():
        # session_ready 也放进事件流：保证 _with_heartbeat 已在驱动本轮生成，
        # 客户端极早断连时也不会留下无人收尾的运行态。
        yield f"data: {json.dumps({'type': 'session_ready', 'session_id': session_id})}\n\n"
        async with aclosing(generate_response(
            orchestrator_service=request.app.state.orchestrator_service,
            messages=body.messages, session_id=session_id, user_id=user_id,
            session_service=session_service,
            langfuse_service=request.app.state.langfuse_service,
            agent_id=body.agent_id, request=request,
            search_enabled=body.search_enabled, skills=body.skills,
            knowledge_bases=body.knowledge_bases,
            cancel_event=run.cancel_event, run_id=run.run_id,
            workspace_manager=getattr(request.app.state, "workspace_manager", None),
        )) as generation:
            async for ev in generation:
                yield ev

    async def stream():
        nonlocal watcher
        watcher = asyncio.create_task(run.watch_stop())
        try:
            # 收尾（session_service / 租约释放）由 _with_heartbeat 统一负责：
            # 正常结束、用户停止、客户端断连后后台跑完，都会走到 on_finish
            async with aclosing(_with_heartbeat(
                source(), cancel_event=run.cancel_event,
                on_finish=cleanup, on_detach=note_detached,
            )) as heartbeat:
                async for event in heartbeat:
                    yield event
        finally:
            if detached_task is None or detached_task.done():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    try:
        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            # 兜底：body 从未被消费（极早断连/服务关闭）时无人收尾，由它补上；
            # 断连后生成还在后台跑时它会主动让位，不提前释放运行态
            background=BackgroundTask(cleanup, background=True),
        )
    except Exception:
        if tasks.get(session_id) is run:
            tasks.pop(session_id, None)
        await run.close()
        raise


@router.post("/chat/stop")
async def stop_chat(request: Request, body: StopRequest, user: dict = Depends(current_user)):
    """Stop only the caller's currently active round, including on another Pod."""
    try:
        stopped = await request_stop(
            getattr(request.app.state, "redis_client", None), body.session_id,
            user.get("user_id"), getattr(request.app.state, "chat_tasks", {}).get(body.session_id),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CoordinationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not stopped:
        return {"ok": False, "msg": "会话不在运行中或已结束"}
    return {"ok": True, "msg": "已发送停止信号"}


@router.post("/chat/feedback")
async def submit_feedback(request: Request, body: FeedbackRequest, user: dict = Depends(current_user)):
    try:
        active = await authorize_active_run(
            getattr(request.app.state, "redis_client", None), body.session_id,
            user.get("user_id"), getattr(request.app.state, "chat_tasks", {}).get(body.session_id),
        )
        ok = active is not None and await resolve_feedback_shared(
            body.session_id, body.feedback_id, {"action": body.action, "payload": body.payload},
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (CoordinationUnavailable, FeedbackBackendError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="反馈已失效（超时、已处理或会话已结束）")
    return {"ok": True}
