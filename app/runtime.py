from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import Lock

from app.events import RunnerEvent


@dataclass
class RuntimeInput:
    kind: str
    content: str = ""


@dataclass
class SessionRuntime:
    session_id: str
    sdk_session_id: str = "default"
    input_queue: asyncio.Queue[RuntimeInput] = field(default_factory=asyncio.Queue)
    event_queue: asyncio.Queue[RunnerEvent] = field(default_factory=asyncio.Queue)
    worker_task: asyncio.Task[None] | None = None


class RuntimeManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._runtimes: dict[str, SessionRuntime] = {}

    def get_or_create(self, session_id: str) -> SessionRuntime:
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                runtime = SessionRuntime(session_id=session_id)
                self._runtimes[session_id] = runtime
            return runtime

    def get(self, session_id: str) -> SessionRuntime | None:
        with self._lock:
            return self._runtimes.get(session_id)
