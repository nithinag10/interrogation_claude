from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ProcessError,
    ResultMessage,
    TextBlock,
)

try:
    from app.events import RunnerEvent, to_sse
    from app.logging_utils import setup_logging
    from app.prompt_loader import load_system_prompt
    from app.tools import create_research_server
except ModuleNotFoundError:
    from events import RunnerEvent, to_sse
    from logging_utils import setup_logging
    from prompt_loader import load_system_prompt
    from tools import create_research_server

logger = setup_logging()


def _extract_questions(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    questions = input_data.get("questions", [])
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _emit(event: str, data: dict[str, Any], on_event: Callable[[RunnerEvent], None] | None) -> None:
    runner_event = RunnerEvent(event=event, data=data)
    if on_event:
        on_event(runner_event)
    logger.debug("event=%s data=%s", event, data)


async def run_orchestrator(
    client: ClaudeSDKClient,
    user_prompt: str,
    session_id: str,
    on_event: Callable[[RunnerEvent], None] | None = None,
) -> str:
    _emit("request_started", {"session_id": session_id, "prompt": user_prompt}, on_event)
    await client.query(user_prompt, session_id=session_id)

    latest_session_id = session_id
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    text = block.text
                    print(text, end="", flush=True)
                    _emit("assistant_delta", {"text": text}, on_event)
        elif isinstance(msg, ResultMessage):
            latest_session_id = msg.session_id or latest_session_id
            _emit(
                "response_completed",
                {
                    "session_id": latest_session_id,
                    "is_error": msg.is_error,
                    "cost_usd": msg.total_cost_usd,
                },
                on_event,
            )
        else:
            _emit("message", {"type": type(msg).__name__}, on_event)
    print()
    return latest_session_id


async def main() -> None:
    server = create_research_server()
    logger.info("Initialized MCP server: research_server")
    system_prompt = load_system_prompt()
    logger.info("Loaded system prompt from app/prompts/system_prompt.txt")

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any):
        logger.info("Tool permission requested: %s", tool_name)
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow(updated_input=input_data)

        answered: list[dict[str, Any]] = []
        for q in _extract_questions(input_data):
            question = str(q.get("question", "")).strip()
            if not question:
                continue
            print(f"\n[Clarification] {question}")
            answer = input("> ").strip()
            answered.append(
                {
                    "question": question,
                    "answer": answer,
                    "attachments": [],
                }
            )

        updated = dict(input_data)
        updated["questions"] = answered
        return PermissionResultAllow(updated_input=updated)

    async def pre_tool_use_hook(_input_data: Any, _tool_use_id: str | None, _context: Any):
        # Keep tool flow deterministic when can_use_tool is active.
        return {"continue_": True}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"research_server": server},
        allowed_tools=["AskUserQuestion", "mcp__research_server__simulate_user_interview"],
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use_hook])]},
        stderr=lambda line: logger.debug("sdk-stderr: %s", line.rstrip()),
        max_turns=10,
    )

    print("Local Claude SDK test runner. Type 'exit' to quit.")
    session_id = "local-research-session"

    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                user_prompt = input("\nPrompt> ").strip()
                if user_prompt.lower() in {"exit", "quit"}:
                    break
                if not user_prompt:
                    continue

                logger.info("Sending prompt (session_id=%s)", session_id)

                # SSE-ready event hook: replace with queue/publisher in FastAPI.
                def on_event(evt: RunnerEvent) -> None:
                    logger.debug("sse: %s", to_sse(evt).rstrip())

                session_id = await run_orchestrator(
                    client=client,
                    user_prompt=user_prompt,
                    session_id=session_id,
                    on_event=on_event,
                )
    except ProcessError as exc:
        logger.error("Claude SDK process failed: %s", exc)
        logger.error("Tip: ensure Claude CLI can write under ~/.claude and auth is valid.")


if __name__ == "__main__":
    anyio.run(main)
