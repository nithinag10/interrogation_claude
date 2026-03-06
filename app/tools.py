from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    create_sdk_mcp_server,
    query,
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


async def get_agent_response(system_prompt: str, prompt: str) -> str:
    options = ClaudeAgentOptions(system_prompt=system_prompt, max_turns=1, allowed_tools=[])
    response_text = ""
    async with asyncio.timeout(30):
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
    return response_text.strip()


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
    start_message = (
        f"Starting customer interview with persona '{persona}' to test hypothesis: {hypothesis}"
    )
    logger.info("Starting interview simulation for hypothesis=%s", hypothesis)
    logger.info("System Persona: %s", persona)
    await emit_tool_event(
        "interview_status",
        {
            "status": "started",
            "message": start_message,
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
        logger.debug("Interview turn=%d", current_turn)
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
        logger.info("Interviewer Reply: %s", interviewer_reply)
        await emit_tool_event(
            "interview_message",
            {
                "turn": current_turn,
                "role": "interviewer",
                "content": interviewer_reply,
                "max_turns": max_turns,
                "remaining_turns": remaining_turns,
            },
        )

        if "[END_INTERVIEW]" in interviewer_reply:
            analysis = interviewer_reply.replace("[END_INTERVIEW]", "").strip()
            transcript += f"\n\n--- INTERVIEWER ANALYSIS ---\n{analysis}"
            logger.info("Interviewer concluded at turn=%d", current_turn)
            await emit_tool_event(
                "interview_status",
                {
                    "status": "concluded",
                    "message": "Interviewer concluded the interview.",
                    "turn": current_turn,
                    "max_turns": max_turns,
                    "remaining_turns": remaining_turns,
                },
            )
            break

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
        logger.info("Customer Reply: %s", customer_reply)
        await emit_tool_event(
            "interview_message",
            {
                "turn": current_turn,
                "role": "customer",
                "content": customer_reply,
                "max_turns": max_turns,
                "remaining_turns": remaining_turns,
            },
        )

    logger.info("Interview simulation complete")
    await emit_tool_event(
        "interview_transcript",
        {
            "tool_name": "mcp__research_server__simulate_user_interview",
            "transcript": transcript,
            "message": "Interview transcript generated.",
        },
    )
    await emit_tool_event(
        "interview_status",
        {
            "status": "completed",
            "message": "Interview simulation completed.",
            "max_turns": max_turns,
            "turns_completed": turns_completed,
            "remaining_turns": max_turns - turns_completed,
        },
    )
    return {"content": [{"type": "text", "text": transcript}]}


def create_research_server():
    return create_sdk_mcp_server(
        name="research_server",
        version="1.0.0",
        tools=[simulate_user_interview],
    )
