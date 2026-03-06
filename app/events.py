from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class RunnerEvent:
    event: str
    data: dict[str, Any]


def to_sse(event: RunnerEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=True)}\n\n"
