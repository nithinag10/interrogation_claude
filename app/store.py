from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from app.models import Message, MessageRole, SessionRecord, SessionState


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryStore:
    def __init__(self) -> None:
        self.lock = Lock()
        self.sessions: dict[str, SessionRecord] = {}

    def create_session(self, user_id: str, title: str) -> SessionRecord:
        now = utc_now_iso()
        session_id = f"s_{uuid4().hex[:10]}"
        session = SessionRecord(
            id=session_id,
            user_id=user_id,
            title=title,
            state=SessionState.NEW,
            context={},
            messages=[],
            created_at=now,
            updated_at=now,
        )
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def append_message(self, session: SessionRecord, role: MessageRole, content: str, phase: str) -> None:
        session.messages.append(
            Message(
                id=f"m_{uuid4().hex[:10]}",
                role=role,
                content=content.strip(),
                phase=phase,
                created_at=utc_now_iso(),
            )
        )
        session.updated_at = utc_now_iso()
