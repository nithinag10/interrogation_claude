from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, get_current_user, router as auth_router
from app.agent_worker import run_session_worker
from app.events import RunnerEvent, to_sse
from app.logging_utils import setup_logging
from app.models import (
    ChatSendRequest,
    ChatSendResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    FeedbackSubmitRequest,
    FeedbackSubmitResponse,
    InterruptRequest,
    InterruptResponse,
    SessionResponse,
)
from app.runtime import RuntimeInput, RuntimeManager
from app.store import InMemoryStore, utc_now_iso
from app.webhooks import WebhookNotifier

logger = setup_logging()


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _load_cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if configured:
        origins = [_normalize_origin(origin) for origin in configured.split(",") if origin.strip()]
        if origins:
            return origins
    return [
        _normalize_origin("http://localhost:5173"),
        _normalize_origin("http://127.0.0.1:5173"),
        _normalize_origin("http://127.0.0.1:8080"),
        _normalize_origin("http://localhost:8080"),
        _normalize_origin("https://idea-sharpen.vercel.app"),
    ]


def create_app() -> FastAPI:
    app = FastAPI(title="Business Research Agent API", version="0.1.0")
    cors_origins = _load_cors_origins()
    cors_allow_origin_regex = os.getenv(
        "CORS_ALLOW_ORIGIN_REGEX",
        r"^https://idea-sharpen(-[a-zA-Z0-9-]+)?\.vercel\.app$",
    )
    logger.info(
        "CORS configured allow_origins=%s allow_origin_regex=%s",
        cors_origins,
        cors_allow_origin_regex,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router)

    store = InMemoryStore()
    runtime_manager = RuntimeManager()
    webhook_notifier = WebhookNotifier()

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": "business-research-agent", "version": "0.1.0", "docs": "/docs"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions", response_model=CreateSessionResponse)
    async def create_session(
        payload: CreateSessionRequest,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CreateSessionResponse:
        logger.info("POST /v1/sessions user_id=%s title=%s", current_user.id, payload.title)
        session = store.create_session(user_id=current_user.id, title=payload.title)
        await webhook_notifier.notify_session_created(session)
        logger.info("session created session_id=%s", session.id)
        return CreateSessionResponse(
            session_id=session.id,
            state=session.state,
            created_at=session.created_at,
        )

    @app.get("/v1/sessions/{session_id}", response_model=SessionResponse)
    def get_session(
        session_id: str,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> SessionResponse:
        logger.info("GET /v1/sessions/%s", session_id)
        session = store.get_session(session_id)
        if not session:
            logger.warning("session not found session_id=%s", session_id)
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        return SessionResponse(
            session_id=session.id,
            user_id=session.user_id,
            title=session.title,
            state=session.state,
            context=session.context,
            messages=session.messages,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    @app.post("/v1/chat/send", response_model=ChatSendResponse)
    async def send_chat(
        payload: ChatSendRequest,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> ChatSendResponse:
        logger.info(
            "POST /v1/chat/send session_id=%s message_len=%d stream=%s",
            payload.session_id,
            len(payload.message),
            payload.stream,
        )
        with store.lock:
            session = store.get_session(payload.session_id)
        if not session:
            logger.warning("send_chat session not found session_id=%s", payload.session_id)
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        runtime = runtime_manager.get_or_create(payload.session_id)
        if runtime.worker_task is None or runtime.worker_task.done():
            logger.info("starting worker session_id=%s", payload.session_id)
            runtime.worker_task = asyncio.create_task(run_session_worker(runtime=runtime, store=store))

        await runtime.input_queue.put(RuntimeInput(kind="message", content=payload.message))
        logger.info("queued message session_id=%s queue_size=%d", payload.session_id, runtime.input_queue.qsize())
        with store.lock:
            refreshed = store.get_session(payload.session_id)
            state = refreshed.state if refreshed else session.state
            is_first_query = not bool(session.context.get("first_query_webhook_sent"))
            if is_first_query:
                session.context["first_query_webhook_sent"] = True
        if is_first_query:
            await webhook_notifier.notify_first_query(session, payload.message)
        return ChatSendResponse(
            session_id=payload.session_id,
            state=state,
            assistant_message="Accepted. Subscribe to SSE stream for live updates.",
            clarification_questions=[],
        )

    @app.post("/v1/chat/interrupt", response_model=InterruptResponse)
    async def interrupt_chat(
        payload: InterruptRequest,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> InterruptResponse:
        logger.info("POST /v1/chat/interrupt session_id=%s", payload.session_id)
        with store.lock:
            session = store.get_session(payload.session_id)
        if not session:
            logger.warning("interrupt session not found session_id=%s", payload.session_id)
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        runtime = runtime_manager.get(payload.session_id)
        if not runtime or not runtime.worker_task or runtime.worker_task.done():
            logger.warning("interrupt no active run session_id=%s", payload.session_id)
            raise HTTPException(status_code=409, detail="No active run to interrupt")

        await runtime.input_queue.put(RuntimeInput(kind="interrupt"))
        logger.info("queued interrupt session_id=%s", payload.session_id)
        return InterruptResponse(
            session_id=payload.session_id,
            state=session.state,
            message="Interrupt requested",
        )

    @app.post("/v1/feedback", response_model=FeedbackSubmitResponse)
    async def submit_feedback(
        payload: FeedbackSubmitRequest,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> FeedbackSubmitResponse:
        logger.info(
            "POST /v1/feedback session_id=%s rating=%s comment_len=%d",
            payload.session_id,
            payload.rating,
            len(payload.comment),
        )
        with store.lock:
            session = store.get_session(payload.session_id)
            if not session:
                logger.warning("feedback session not found session_id=%s", payload.session_id)
                raise HTTPException(status_code=404, detail="Session not found")
            if session.user_id != current_user.id:
                raise HTTPException(status_code=403, detail="Forbidden")

            feedback_entry = {
                "rating": payload.rating,
                "comment": payload.comment.strip(),
                "submitted_at": utc_now_iso(),
            }
            session.context["latest_feedback"] = feedback_entry
            feedback_history = session.context.setdefault("feedback_history", [])
            if isinstance(feedback_history, list):
                feedback_history.append(feedback_entry)
            session.updated_at = utc_now_iso()
            state = session.state

        logger.info(
            "feedback stored session_id=%s rating=%s history_count=%d",
            payload.session_id,
            payload.rating,
            len(session.context.get("feedback_history", [])) if isinstance(session.context.get("feedback_history", []), list) else 0,
        )
        await webhook_notifier.notify_feedback_received(session, payload.rating, payload.comment.strip())

        return FeedbackSubmitResponse(
            session_id=payload.session_id,
            state=state,
            rating=payload.rating,
            comment=payload.comment.strip(),
            message="Feedback received",
        )

    @app.get("/v1/chat/stream/{session_id}")
    async def stream_chat(
        session_id: str,
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
    ) -> StreamingResponse:
        logger.info("GET /v1/chat/stream/%s", session_id)
        with store.lock:
            session = store.get_session(session_id)
        if not session:
            logger.warning("stream session not found session_id=%s", session_id)
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        runtime = runtime_manager.get_or_create(session_id)

        async def event_stream():
            logger.info("sse connected session_id=%s", session_id)
            yield to_sse(RunnerEvent(event="connected", data={"session_id": session_id}))
            while True:
                if await request.is_disconnected():
                    logger.info("sse disconnected session_id=%s", session_id)
                    break
                try:
                    event = await asyncio.wait_for(runtime.event_queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                logger.debug("sse event session_id=%s event=%s", session_id, event.event)
                yield to_sse(event)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return app
