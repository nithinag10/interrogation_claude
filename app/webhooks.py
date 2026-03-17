from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from app.logging_utils import setup_logging
from app.models import SessionRecord

logger = setup_logging()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WebhookSettings:
    enabled: bool
    url: str
    auth_header: str
    auth_token: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "WebhookSettings":
        url = os.getenv("WEBHOOK_URL", "").strip()
        return cls(
            enabled=_env_flag("WEBHOOK_ENABLED", default=bool(url)),
            url=url,
            auth_header=os.getenv("WEBHOOK_AUTH_HEADER", "Authorization").strip() or "Authorization",
            auth_token=os.getenv("WEBHOOK_AUTH_TOKEN", "").strip(),
            timeout_seconds=float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "5")),
        )


class WebhookNotifier:
    def __init__(self, settings: WebhookSettings | None = None) -> None:
        self.settings = settings or WebhookSettings.from_env()

    def is_enabled(self) -> bool:
        return self.settings.enabled and bool(self.settings.url)

    async def notify_session_created(self, session: SessionRecord) -> None:
        await self._send(
            "session_created",
            session=session,
            extra={"trigger": "POST /v1/sessions"},
        )

    async def notify_first_query(self, session: SessionRecord, message: str) -> None:
        await self._send(
            "first_query_submitted",
            session=session,
            extra={
                "trigger": "POST /v1/chat/send",
                "query": {
                    "length": len(message),
                    "text": message,
                },
            },
        )

    async def notify_final_answer(self, session: SessionRecord, answer: str) -> None:
        await self._send(
            "final_answer_generated",
            session=session,
            extra={
                "trigger": "session completion",
                "answer": {
                    "length": len(answer),
                    "text": answer,
                },
            },
        )

    async def notify_feedback_received(self, session: SessionRecord, rating: int, comment: str) -> None:
        await self._send(
            "feedback_received",
            session=session,
            extra={
                "trigger": "POST /v1/feedback",
                "feedback": {
                    "rating": rating,
                    "comment": comment,
                },
            },
        )

    async def _send(self, event_type: str, session: SessionRecord, extra: dict[str, Any] | None = None) -> None:
        if not self.is_enabled():
            logger.info("webhook skipped event=%s reason=disabled_or_missing_url session_id=%s", event_type, session.id)
            return

        payload: dict[str, Any] = {
            "event_type": event_type,
            "occurred_at": _utc_now_iso(),
            "service": "business-research-agent",
            "session": {
                "id": session.id,
                "user_id": session.user_id,
                "title": session.title,
                "state": session.state.value,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            },
        }
        if extra:
            payload.update(extra)

        logger.info("webhook enqueue event=%s session_id=%s", event_type, session.id)
        await asyncio.to_thread(self._post_json, self._format_payload(payload), event_type)

    def _format_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._is_slack_webhook():
            return {"text": self._format_slack_text(payload)}
        return payload

    def _is_slack_webhook(self) -> bool:
        return "hooks.slack.com/services/" in self.settings.url

    def _format_slack_text(self, payload: dict[str, Any]) -> str:
        session = payload.get("session", {})
        session_id = session.get("id", "")
        user_id = session.get("user_id", "")
        event_type = payload.get("event_type", "")

        if event_type == "session_created":
            return (
                "New session created\n"
                f"session_id: {session_id}\n"
                f"user_id: {user_id}\n"
                f"title: {session.get('title', '')}"
            )

        if event_type == "first_query_submitted":
            query = payload.get("query", {})
            return (
                "First query submitted\n"
                f"session_id: {session_id}\n"
                f"user_id: {user_id}\n"
                f"query: {query.get('text', '')}"
            )

        if event_type == "final_answer_generated":
            answer = payload.get("answer", {})
            return (
                "Final answer generated\n"
                f"session_id: {session_id}\n"
                f"user_id: {user_id}\n"
                f"answer: {answer.get('text', '')}"
            )

        if event_type == "feedback_received":
            feedback = payload.get("feedback", {})
            return (
                "Feedback received\n"
                f"session_id: {session_id}\n"
                f"user_id: {user_id}\n"
                f"rating: {feedback.get('rating', '')}/5\n"
                f"comment: {feedback.get('comment', '')}"
            )

        return json.dumps(payload, ensure_ascii=True)

    def _post_json(self, payload: dict[str, Any], event_type: str) -> None:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.auth_token:
            headers[self.settings.auth_header] = self.settings.auth_token

        req = request.Request(
            self.settings.url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.settings.timeout_seconds) as response:
                status = getattr(response, "status", "unknown")
                logger.info("webhook delivered status=%s event=%s", status, event_type)
        except error.HTTPError as exc:
            logger.warning(
                "webhook http error status=%s event=%s reason=%s",
                exc.code,
                event_type,
                exc.reason,
            )
        except error.URLError as exc:
            logger.warning("webhook url error event=%s reason=%s", event_type, exc.reason)
        except Exception:
            logger.exception("webhook unexpected failure event=%s", event_type)
