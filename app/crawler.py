from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from firecrawl import FirecrawlApp

from app.logging_utils import setup_logging

logger = setup_logging()

CRAWL_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "crawls"

FIRECRAWL_MAX_PAGES = int(os.getenv("FIRECRAWL_MAX_PAGES", "20"))
FIRECRAWL_TIMEOUT_SECONDS = int(os.getenv("FIRECRAWL_TIMEOUT_SECONDS", "120"))


@dataclass
class CrawlResult:
    session_id: str
    domain: str
    page_count: int
    crawl_dir: str
    product_summary: str
    page_manifest: list[dict[str, str]] = field(default_factory=list)


def _normalize_url(domain: str) -> str:
    domain = domain.strip().rstrip("/")
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


def _sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return "index"
    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized[:120] or "page"


def _build_page_header(source_url: str, title: str, crawled_at: str) -> str:
    return (
        f"---\n"
        f"source_url: {source_url}\n"
        f"title: {title}\n"
        f"crawled_at: {crawled_at}\n"
        f"---\n\n"
    )


def _generate_product_summary(pages: list[dict[str, Any]], domain: str) -> str:
    home_content = ""
    for page in pages:
        url = (page.get("metadata") or {}).get("sourceURL", "") or page.get("url", "")
        parsed = urlparse(url)
        if parsed.path in ("", "/", "/index.html", "/index"):
            home_content = page.get("markdown", "")
            break

    if not home_content and pages:
        home_content = pages[0].get("markdown", "")

    if not home_content:
        return f"Website: {domain} (no content could be extracted)"

    summary = home_content[:2000]
    if len(home_content) > 2000:
        last_sentence = summary.rfind(".")
        if last_sentence > 500:
            summary = summary[: last_sentence + 1]

    return f"Website: {domain}\n\n{summary}"


def _write_pages(
    crawl_dir: Path,
    pages: list[dict[str, Any]],
    crawled_at: str,
) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []

    for page in pages:
        metadata = page.get("metadata") or {}
        source_url = metadata.get("sourceURL", "") or page.get("url", "")
        title = metadata.get("title", "") or source_url
        markdown = page.get("markdown", "")

        if not markdown:
            continue

        filename = _sanitize_filename(source_url)
        filepath = crawl_dir / f"{filename}.md"

        counter = 1
        while filepath.exists():
            filepath = crawl_dir / f"{filename}_{counter}.md"
            counter += 1

        header = _build_page_header(source_url, title, crawled_at)
        filepath.write_text(header + markdown, encoding="utf-8")

        manifest.append({
            "file": filepath.name,
            "title": title,
            "source_url": source_url,
        })

    return manifest


def _write_index(crawl_dir: Path, domain: str, manifest: list[dict[str, str]], crawled_at: str) -> None:
    lines = [
        f"# Crawled Pages for {domain}\n",
        f"Crawled at: {crawled_at}",
        f"Total pages: {len(manifest)}\n",
        "| File | Title | URL |",
        "|------|-------|-----|",
    ]
    for entry in manifest:
        lines.append(f"| {entry['file']} | {entry['title']} | {entry['source_url']} |")

    (crawl_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _doc_to_dict(doc: Any) -> dict[str, Any]:
    """Normalize a firecrawl Document (Pydantic model or dict) to a plain dict
    with a consistent `metadata.sourceURL` key for downstream code."""
    if isinstance(doc, dict):
        raw = doc
    elif hasattr(doc, "model_dump"):
        raw = doc.model_dump()
    elif hasattr(doc, "__dict__"):
        raw = dict(doc.__dict__)
    else:
        return {}

    # v2 SDK uses snake_case metadata fields (source_url); normalise to sourceURL
    meta = raw.get("metadata") or {}
    if isinstance(meta, dict):
        if "source_url" in meta and "sourceURL" not in meta:
            meta["sourceURL"] = meta["source_url"]
        raw["metadata"] = meta

    return raw


def _do_crawl(url: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY environment variable is not set")

    fc = FirecrawlApp(api_key=api_key)
    logger.info("firecrawl starting url=%s max_pages=%d", url, FIRECRAWL_MAX_PAGES)

    # ── firecrawl-py v2: fc.crawl(url, limit=..., scrape_options=...) ──────────
    if hasattr(fc, "crawl") and not hasattr(fc, "crawl_url"):
        logger.info("using firecrawl-py v2 crawl API")
        try:
            from firecrawl.v2.types import ScrapeOptions
            scrape_options = ScrapeOptions(formats=["markdown"])
        except ImportError:
            scrape_options = None  # type: ignore[assignment]

        kwargs: dict[str, Any] = {"limit": FIRECRAWL_MAX_PAGES}
        if scrape_options is not None:
            kwargs["scrape_options"] = scrape_options

        job = fc.crawl(url, **kwargs)
        logger.info("v2 crawl done status=%s total=%s", getattr(job, "status", "?"), getattr(job, "total", "?"))
        docs = getattr(job, "data", []) or []
        pages = [_doc_to_dict(d) for d in docs]
        logger.info("v2 crawl pages=%d", len(pages))
        return pages

    # ── firecrawl-py v1: fc.crawl_url(url, params=dict) ───────────────────────
    if hasattr(fc, "crawl_url"):
        logger.info("using firecrawl-py v1 crawl_url API")
        result = fc.crawl_url(
            url,
            params={
                "limit": FIRECRAWL_MAX_PAGES,
                "scrapeOptions": {"formats": ["markdown"]},
            },
        )
        logger.info("v1 crawl_url returned type=%s", type(result).__name__)
        if hasattr(result, "data") and result.data is not None:
            pages = [_doc_to_dict(d) for d in result.data]
        elif isinstance(result, dict):
            pages = [_doc_to_dict(d) for d in result.get("data", [])]
        else:
            pages = []
        logger.info("v1 crawl pages=%d", len(pages))
        return pages

    # ── last resort: scrape home page only ─────────────────────────────────────
    logger.warning("no crawl method found — falling back to single-page scrape")
    scrape_fn = getattr(fc, "scrape", None) or getattr(fc, "scrape_url", None)
    if scrape_fn is None:
        raise RuntimeError("firecrawl SDK has no usable scrape or crawl method")
    doc_raw = scrape_fn(url, formats=["markdown"])
    page = _doc_to_dict(doc_raw)
    if not page.get("markdown"):
        return []
    meta = page.setdefault("metadata", {})
    if isinstance(meta, dict) and not meta.get("sourceURL"):
        meta["sourceURL"] = url
    return [page]


async def crawl_domain(
    session_id: str,
    domain: str,
    event_emitter: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> CrawlResult:
    url = _normalize_url(domain)
    crawl_dir = CRAWL_DATA_ROOT / session_id
    crawl_dir.mkdir(parents=True, exist_ok=True)
    crawled_at = datetime.now(timezone.utc).isoformat()

    logger.info("crawl starting session=%s domain=%s", session_id, url)
    await event_emitter("crawl_started", {"domain": url, "message": f"Crawling {url}..."})

    pages = await asyncio.wait_for(
        asyncio.to_thread(_do_crawl, url),
        timeout=FIRECRAWL_TIMEOUT_SECONDS,
    )

    logger.info("crawl fetched session=%s pages=%d", session_id, len(pages))

    manifest: list[dict[str, str]] = []
    for i, page in enumerate(pages):
        source_url = page.get("metadata") or {}.get("sourceURL", "") or page.get("url", "")
        await event_emitter(
            "crawl_progress",
            {
                "domain": url,
                "pages_crawled": i + 1,
                "total_pages": len(pages),
                "current_page": source_url,
                "message": f"Processing page {i + 1}/{len(pages)}: {source_url}",
            },
        )

    manifest = _write_pages(crawl_dir, pages, crawled_at)
    _write_index(crawl_dir, url, manifest, crawled_at)

    product_summary = _generate_product_summary(pages, url)

    logger.info(
        "crawl completed session=%s pages_written=%d summary_len=%d",
        session_id,
        len(manifest),
        len(product_summary),
    )

    await event_emitter(
        "crawl_completed",
        {
            "domain": url,
            "page_count": len(manifest),
            "message": f"Website crawl complete. {len(manifest)} pages indexed.",
        },
    )

    return CrawlResult(
        session_id=session_id,
        domain=url,
        page_count=len(manifest),
        crawl_dir=str(crawl_dir),
        product_summary=product_summary,
        page_manifest=manifest,
    )


def cleanup_crawl_data() -> None:
    if CRAWL_DATA_ROOT.exists():
        import shutil

        for child in CRAWL_DATA_ROOT.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        logger.info("cleaned up stale crawl data at %s", CRAWL_DATA_ROOT)
