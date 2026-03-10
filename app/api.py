from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.agent_worker import run_session_worker
from app.events import RunnerEvent, to_sse
from app.logging_utils import setup_logging
from app.models import (
    ChatSendRequest,
    ChatSendResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    InterruptRequest,
    InterruptResponse,
    SessionResponse,
)
from app.runtime import RuntimeInput, RuntimeManager
from app.store import InMemoryStore

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
    store = InMemoryStore()
    runtime_manager = RuntimeManager()

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": "business-research-agent", "version": "0.1.0", "docs": "/docs"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions", response_model=CreateSessionResponse)
    def create_session(payload: CreateSessionRequest) -> CreateSessionResponse:
        logger.info("POST /v1/sessions user_id=%s title=%s", payload.user_id, payload.title)
        session = store.create_session(user_id=payload.user_id, title=payload.title)
        logger.info("session created session_id=%s", session.id)
        return CreateSessionResponse(
            session_id=session.id,
            state=session.state,
            created_at=session.created_at,
        )

    @app.get("/v1/sessions/{session_id}", response_model=SessionResponse)
    def get_session(session_id: str) -> SessionResponse:
        logger.info("GET /v1/sessions/%s", session_id)
        session = store.get_session(session_id)
        if not session:
            logger.warning("session not found session_id=%s", session_id)
            raise HTTPException(status_code=404, detail="Session not found")

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
    async def send_chat(payload: ChatSendRequest) -> ChatSendResponse:
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

        runtime = runtime_manager.get_or_create(payload.session_id)
        if runtime.worker_task is None or runtime.worker_task.done():
            logger.info("starting worker session_id=%s", payload.session_id)
            runtime.worker_task = asyncio.create_task(run_session_worker(runtime=runtime, store=store))

        await runtime.input_queue.put(RuntimeInput(kind="message", content=payload.message))
        logger.info("queued message session_id=%s queue_size=%d", payload.session_id, runtime.input_queue.qsize())
        with store.lock:
            refreshed = store.get_session(payload.session_id)
            state = refreshed.state if refreshed else session.state
        return ChatSendResponse(
            session_id=payload.session_id,
            state=state,
            assistant_message="Accepted. Subscribe to SSE stream for live updates.",
            clarification_questions=[],
        )

    @app.post("/v1/chat/interrupt", response_model=InterruptResponse)
    async def interrupt_chat(payload: InterruptRequest) -> InterruptResponse:
        logger.info("POST /v1/chat/interrupt session_id=%s", payload.session_id)
        with store.lock:
            session = store.get_session(payload.session_id)
        if not session:
            logger.warning("interrupt session not found session_id=%s", payload.session_id)
            raise HTTPException(status_code=404, detail="Session not found")

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

    @app.get("/v1/chat/stream/{session_id}")
    async def stream_chat(session_id: str, request: Request) -> StreamingResponse:
        logger.info("GET /v1/chat/stream/%s", session_id)
        with store.lock:
            session = store.get_session(session_id)
        if not session:
            logger.warning("stream session not found session_id=%s", session_id)
            raise HTTPException(status_code=404, detail="Session not found")

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
