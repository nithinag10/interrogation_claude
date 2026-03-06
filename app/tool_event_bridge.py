from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

_emitter_var: ContextVar[Callable[[str, dict[str, Any]], Awaitable[None]] | None] = (
    ContextVar("tool_event_emitter", default=None)
)


def set_tool_event_emitter(
    emitter: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> Token[Callable[[str, dict[str, Any]], Awaitable[None]] | None]:
    return _emitter_var.set(emitter)


def reset_tool_event_emitter(
    token: Token[Callable[[str, dict[str, Any]], Awaitable[None]] | None],
) -> None:
    _emitter_var.reset(token)


async def emit_tool_event(event: str, data: dict[str, Any]) -> None:
    emitter = _emitter_var.get()
    if emitter is None:
        return
    await emitter(event, data)
