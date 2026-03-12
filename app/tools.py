from __future__ import annotations

import asyncio
import os
import uuid

import anthropic
from claude_agent_sdk import (
    create_sdk_mcp_server,
    tool,
)

try:
    from app.logging_utils import setup_logging
    from app.prompt_loader import (
        load_customer_system_prompt,
        load_interviewer_system_prompt,
    )
    from app.tool_event_bridge import emit_tool_event
except ModuleNotFoundError:
    from logging_utils import setup_logging
    from prompt_loader import (
        load_customer_system_prompt,
        load_interviewer_system_prompt,
    )
    from tool_event_bridge import emit_tool_event

logger = setup_logging()
INTERVIEW_MAX_TURNS = 8

_anthropic = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


async def get_agent_response(system_prompt: str, prompt: str) -> str:
    logger.debug("get_agent_response request | system=%s | prompt=%s", system_prompt, prompt)
    async with asyncio.timeout(30):
        response = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
    result = response.content[0].text.strip()
    logger.debug("get_agent_response response | stop_reason=%s | text=%s", response.stop_reason, result)
    return result


@tool(
    name="simulate_user_interview",
    description=(
        "Conducts a multi-agent user interview to validate a hypothesis. "
        "Agents pass a transcript back and forth until the root cause is found."
    ),
    input_schema={"hypothesis": str, "persona": str},
)
async def simulate_user_interview(args: dict[str, str]) -> dict[str, object]:
    hypothesis = args["hypothesis"]
    persona = args["persona"]
    interview_id = str(uuid.uuid4())

    logger.info("Starting interview interview_id=%s hypothesis=%s persona=%s", interview_id, hypothesis, persona)

    await emit_tool_event(
        "interview_started",
        {
            "interview_id": interview_id,
            "hypothesis": hypothesis,
            "persona": persona,
            "max_turns": INTERVIEW_MAX_TURNS,
        },
    )

    interviewer_system = load_interviewer_system_prompt(hypothesis=hypothesis)
    customer_system = load_customer_system_prompt(persona=persona)

    transcript = ""
    max_turns = INTERVIEW_MAX_TURNS
    turns_completed = 0

    for turn in range(max_turns):
        current_turn = turn + 1
        remaining_turns = max_turns - current_turn
        turns_completed = current_turn
        logger.debug("Interview turn interview_id=%s turn=%d", interview_id, current_turn)

        interviewer_prompt = (
            (
                "You are in an ongoing interview.\n"
                f"Current turn: {current_turn}\n"
                f"Maximum turns: {max_turns}\n"
                f"Remaining turns after this response: {remaining_turns}\n\n"
                f"Here is the transcript so far:\n{transcript}\n\n"
                "Ask your next best question, or end now with [END_INTERVIEW] if evidence is sufficient."
            )
            if transcript
            else (
                "Start the interview with your very first question.\n"
                f"Current turn: {current_turn}\n"
                f"Maximum turns: {max_turns}\n"
                f"Remaining turns after this response: {remaining_turns}\n\n"
                "You may end early with [END_INTERVIEW] if the evidence is already sufficient."
            )
        )
        interviewer_reply = await get_agent_response(interviewer_system, interviewer_prompt)
        logger.info("Interviewer interview_id=%s turn=%d reply=%s", interview_id, current_turn, interviewer_reply)

        if "[END_INTERVIEW]" in interviewer_reply:
            analysis = interviewer_reply.replace("[END_INTERVIEW]", "").strip()
            transcript += f"\n\n--- INTERVIEWER ANALYSIS ---\n{analysis}"
            await emit_tool_event(
                "interview_turn",
                {
                    "interview_id": interview_id,
                    "turn": current_turn,
                    "role": "interviewer",
                    "content": analysis,
                    "max_turns": max_turns,
                    "remaining_turns": remaining_turns,
                    "is_final": True,
                },
            )
            await emit_tool_event(
                "interview_concluded",
                {
                    "interview_id": interview_id,
                    "turn": current_turn,
                    "analysis": analysis,
                },
            )
            logger.info("Interview concluded early interview_id=%s turn=%d", interview_id, current_turn)
            break

        await emit_tool_event(
            "interview_turn",
            {
                "interview_id": interview_id,
                "turn": current_turn,
                "role": "interviewer",
                "content": interviewer_reply,
                "max_turns": max_turns,
                "remaining_turns": remaining_turns,
                "is_final": False,
            },
        )
        transcript += f"\nInterviewer: {interviewer_reply}\n"

        customer_prompt = (
            "You are answering as the customer persona.\n"
            f"Current turn: {current_turn}\n"
            f"Maximum turns: {max_turns}\n"
            f"Remaining turns after this response: {remaining_turns}\n\n"
            f"Here is the transcript so far:\n{transcript}\n\nHow do you respond to the last question?"
        )
        customer_reply = await get_agent_response(customer_system, customer_prompt)
        transcript += f"Customer: {customer_reply}\n"
        logger.info("Customer interview_id=%s turn=%d reply=%s", interview_id, current_turn, customer_reply)

        await emit_tool_event(
            "interview_turn",
            {
                "interview_id": interview_id,
                "turn": current_turn,
                "role": "customer",
                "content": customer_reply,
                "max_turns": max_turns,
                "remaining_turns": remaining_turns,
                "is_final": False,
            },
        )

    logger.info("Interview complete interview_id=%s turns_completed=%d", interview_id, turns_completed)
    await emit_tool_event(
        "interview_completed",
        {
            "interview_id": interview_id,
            "turns_completed": turns_completed,
            "max_turns": max_turns,
        },
    )
    return {"content": [{"type": "text", "text": transcript}]}


def create_research_server():
    return create_sdk_mcp_server(
        name="research_server",
        version="1.0.0",
        tools=[simulate_user_interview],
    )
