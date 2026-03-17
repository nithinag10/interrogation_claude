from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    TextBlock,
    UserMessage,
)

from app.events import RunnerEvent
from app.logging_utils import setup_logging
from app.models import MessageRole, SessionState
from app.prompt_loader import load_system_prompt
from app.runtime import RuntimeInput, SessionRuntime
from app.store import InMemoryStore
from app.tool_event_bridge import reset_tool_event_emitter, set_tool_event_emitter
from app.tools import create_research_server
from app.webhooks import WebhookNotifier

logger = setup_logging()
webhook_notifier = WebhookNotifier()


async def _emit(runtime: SessionRuntime, event: str, data: dict[str, Any]) -> None:
    await runtime.event_queue.put(RunnerEvent(event=event, data=data))


def _extract_questions(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    questions = input_data.get("questions", [])
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _extract_transcript_text(content: str | list[dict[str, Any]] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        transcript_parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    transcript_parts.append(item["text"])
        return "\n".join(transcript_parts).strip()
    return ""


def _is_final_report(text: str) -> bool:
    return "# ColdWater Validation Report" in text or "## The Verdict" in text


def _looks_like_interview_transcript(text: str) -> bool:
    if not text:
        return False
    markers = ["Interviewer:", "Customer:", "--- INTERVIEWER ANALYSIS ---"]
    return any(marker in text for marker in markers)


def _interview_start_message(hypothesis: str, persona: str) -> str:
    if hypothesis and persona:
        return (
            f"Starting customer interview with persona '{persona}' "
            f"to test hypothesis: {hypothesis}"
        )
    if hypothesis:
        return f"Starting customer interview to test hypothesis: {hypothesis}"
    if persona:
        return f"Starting customer interview with persona '{persona}'."
    return "Starting customer interview."


async def run_session_worker(runtime: SessionRuntime, store: InMemoryStore) -> None:
    logger.info("Worker started for session=%s", runtime.session_id)
    await _emit(runtime, "session_ready", {"message": "Session worker initialized."})

    async def _tool_emit(event: str, data: dict[str, Any]) -> None:
        await _emit(runtime, event, data)

    emitter_token = set_tool_event_emitter(_tool_emit)
    server = create_research_server()
    system_prompt = load_system_prompt()

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any):
        logger.info("can_use_tool session=%s tool=%s", runtime.session_id, tool_name)

        if tool_name != "AskUserQuestion":
            return PermissionResultAllow(updated_input=input_data)

        questions = _extract_questions(input_data)
        text_questions = [str(q.get("question", "")).strip() for q in questions if q.get("question")]

        with store.lock:
            session = store.get_session(runtime.session_id)
            if session:
                session.state = SessionState.AWAITING_CLARIFICATION

        await _emit(
            runtime,
            "clarification_needed",
            {"questions": text_questions},
        )
        logger.info(
            "awaiting clarification session=%s questions=%d",
            runtime.session_id,
            len(text_questions),
        )

        answered: list[dict[str, Any]] = []
        for q in questions:
            question = str(q.get("question", "")).strip()
            if not question:
                continue
            while True:
                queued = await runtime.input_queue.get()
                logger.info(
                    "clarification input dequeued session=%s kind=%s queue_size=%d",
                    runtime.session_id,
                    queued.kind,
                    runtime.input_queue.qsize(),
                )
                if queued.kind == "interrupt":
                    await _emit(runtime, "session_interrupted", {"message": "Run interrupted while awaiting clarification."})
                    logger.info("clarification interrupted session=%s", runtime.session_id)
                    continue
                if queued.kind != "message":
                    continue
                answer = queued.content.strip()
                if not answer:
                    continue
                with store.lock:
                    session = store.get_session(runtime.session_id)
                    if session:
                        store.append_message(
                            session,
                            MessageRole.USER,
                            answer,
                            phase="clarification",
                        )
                answered.append({"question": question, "answer": answer, "attachments": []})
                logger.info(
                    "clarification answered session=%s answer_len=%d",
                    runtime.session_id,
                    len(answer),
                )
                await _emit(
                    runtime,
                    "clarification_received",
                    {"question": question, "answer_preview": answer[:120]},
                )
                break

        updated = dict(input_data)
        updated["questions"] = answered
        return PermissionResultAllow(updated_input=updated)

    async def pre_tool_use_hook(_input_data: Any, _tool_use_id: str | None, _context: Any):
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

    try:
        async with ClaudeSDKClient(options=options) as client:
            logger.info("claude client connected session=%s", runtime.session_id)
            while True:
                queued = await runtime.input_queue.get()
                logger.info(
                    "worker input dequeued session=%s kind=%s queue_size=%d",
                    runtime.session_id,
                    queued.kind,
                    runtime.input_queue.qsize(),
                )
                if queued.kind == "stop":
                    await _emit(runtime, "session_stopped", {"message": "Worker stopped."})
                    logger.info("worker stopped session=%s", runtime.session_id)
                    return
                if queued.kind == "interrupt":
                    await client.interrupt()
                    await _emit(runtime, "session_interrupted", {"message": "Interrupt signal sent to active run."})
                    logger.info("interrupt sent to sdk session=%s", runtime.session_id)
                    continue
                if queued.kind != "message":
                    continue

                user_message = queued.content.strip()
                if not user_message:
                    continue

                logger.info(
                    "user request session=%s message=%s",
                    runtime.session_id,
                    user_message,
                )

                with store.lock:
                    session = store.get_session(runtime.session_id)
                    if session:
                        if session.state == SessionState.NEW:
                            session.state = SessionState.INTAKE
                        session.state = SessionState.RESEARCH_IN_PROGRESS
                        store.append_message(session, MessageRole.USER, user_message, phase="intake")

                await _emit(runtime, "agent_thinking", {"message": "Agent is analyzing your request."})
                logger.info(
                    "query start session=%s sdk_session_id=%s message_len=%d",
                    runtime.session_id,
                    runtime.sdk_session_id,
                    len(user_message),
                )
                await client.query(user_message, session_id=runtime.sdk_session_id)

                assistant_chunks: list[str] = []
                tool_use_by_id: dict[str, str] = {}
                interview_transcript_emitted = False

                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                text = block.text
                                assistant_chunks.append(text)
                                await _emit(runtime, "agent_delta", {"text": text})
                                logger.debug(
                                    "agent delta session=%s chars=%d",
                                    runtime.session_id,
                                    len(text),
                                )
                            elif isinstance(block, ToolUseBlock):
                                tool_use_by_id[block.id] = block.name
                                tool_payload: dict[str, Any] = {
                                    "tool_name": block.name,
                                    "tool_use_id": block.id,
                                    "input": block.input,
                                }
                                if "simulate_user_interview" in block.name:
                                    hypothesis = ""
                                    persona = ""
                                    if isinstance(block.input, dict):
                                        hypothesis = str(block.input.get("hypothesis", "")).strip()
                                        persona = str(block.input.get("persona", "")).strip()
                                    tool_payload["hypothesis"] = hypothesis
                                    tool_payload["persona"] = persona
                                    await _emit(runtime, "interview_in_progress", {
                                        "tool_use_id": block.id,
                                        "message": _interview_start_message(hypothesis, persona),
                                        "hypothesis": hypothesis,
                                        "persona": persona,
                                    })
                                await _emit(runtime, "tool_started", tool_payload)

                            elif isinstance(block, ToolResultBlock):
                                tool_name = tool_use_by_id.get(block.tool_use_id, "unknown_tool")
                                await _emit(
                                    runtime,
                                    "tool_completed",
                                    {
                                        "tool_name": tool_name,
                                        "tool_use_id": block.tool_use_id,
                                        "is_error": block.is_error,
                                    },
                                )
                                transcript = _extract_transcript_text(block.content)
                                if transcript and (
                                    "simulate_user_interview" in tool_name
                                    or _looks_like_interview_transcript(transcript)
                                ):
                                    interview_transcript_emitted = True
                                    await _emit(
                                        runtime,
                                        "interview_transcript",
                                        {
                                            "tool_use_id": block.tool_use_id,
                                            "transcript": transcript,
                                        },
                                    )

                    elif isinstance(msg, UserMessage):
                        if isinstance(msg.content, list):
                            for block in msg.content:
                                if isinstance(block, ToolResultBlock):
                                    tool_name = tool_use_by_id.get(block.tool_use_id, "unknown_tool")
                                    transcript = _extract_transcript_text(block.content)
                                    await _emit(
                                        runtime,
                                        "tool_completed",
                                        {
                                            "tool_name": tool_name,
                                            "tool_use_id": block.tool_use_id,
                                            "is_error": block.is_error,
                                        },
                                    )
                                    if transcript and (
                                        "simulate_user_interview" in tool_name
                                        or _looks_like_interview_transcript(transcript)
                                    ):
                                        interview_transcript_emitted = True
                                        await _emit(
                                            runtime,
                                            "interview_transcript",
                                            {
                                                "tool_use_id": block.tool_use_id,
                                                "transcript": transcript,
                                            },
                                        )

                    elif isinstance(msg, ResultMessage):
                        runtime.sdk_session_id = msg.session_id or runtime.sdk_session_id
                        assistant_message = "".join(assistant_chunks).strip()
                        if not assistant_message and msg.result:
                            assistant_message = msg.result.strip()
                        if not assistant_message:
                            assistant_message = "Completed, but no text response was generated."

                        # If the interview ran but transcript wasn't emitted via ToolResultBlock,
                        # fall back to checking the assembled assistant message.
                        if (
                            not interview_transcript_emitted
                            and _looks_like_interview_transcript(assistant_message)
                        ):
                            await _emit(
                                runtime,
                                "interview_transcript",
                                {
                                    "tool_use_id": None,
                                    "transcript": assistant_message,
                                },
                            )

                        with store.lock:
                            session = store.get_session(runtime.session_id)
                            if session:
                                session.state = SessionState.COMPLETED
                                session.last_research_summary = assistant_message[:500]
                                store.append_message(
                                    session,
                                    MessageRole.ASSISTANT,
                                    assistant_message,
                                    phase="final",
                                )
                                should_send_final_answer = not bool(session.context.get("final_answer_webhook_sent"))
                                if should_send_final_answer:
                                    session.context["final_answer_webhook_sent"] = True
                            else:
                                should_send_final_answer = False

                        if should_send_final_answer and session:
                            await webhook_notifier.notify_final_answer(session, assistant_message)

                        await _emit(
                            runtime,
                            "agent_response",
                            {
                                "session_id": runtime.session_id,
                                "content": assistant_message,
                            },
                        )
                        if _is_final_report(assistant_message):
                            await _emit(
                                runtime,
                                "final_report",
                                {
                                    "session_id": runtime.session_id,
                                    "content": assistant_message,
                                },
                            )
                        await _emit(
                            runtime,
                            "session_done",
                            {
                                "session_id": runtime.session_id,
                                "sdk_session_id": runtime.sdk_session_id,
                                "is_error": msg.is_error,
                                "cost_usd": msg.total_cost_usd,
                            },
                        )
                        logger.info(
                            "query done session=%s sdk_session_id=%s is_error=%s cost=%s",
                            runtime.session_id,
                            runtime.sdk_session_id,
                            msg.is_error,
                            msg.total_cost_usd,
                        )
                        logger.info(
                            "agent response session=%s response=%s",
                            runtime.session_id,
                            assistant_message,
                        )
                    else:
                        logger.debug(
                            "sdk_unknown_message session=%s type=%s",
                            runtime.session_id,
                            type(msg).__name__,
                        )

    except Exception as exc:
        with store.lock:
            session = store.get_session(runtime.session_id)
            if session:
                session.state = SessionState.FAILED
        logger.exception("Worker failed for session=%s", runtime.session_id)
        await _emit(runtime, "session_error", {"message": str(exc)})
    finally:
        reset_tool_event_emitter(emitter_token)
