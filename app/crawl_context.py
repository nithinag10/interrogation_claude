from __future__ import annotations

from contextvars import ContextVar, Token


_crawl_dir_var: ContextVar[str | None] = ContextVar("crawl_dir", default=None)


def set_crawl_dir(path: str | None) -> Token[str | None]:
    return _crawl_dir_var.set(path)


def reset_crawl_dir(token: Token[str | None]) -> None:
    _crawl_dir_var.reset(token)


def get_crawl_dir() -> str | None:
    return _crawl_dir_var.get()
