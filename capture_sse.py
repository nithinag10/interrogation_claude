"""
Run the FastAPI server, drive a full multi-turn conversation, capture all SSE events.
Usage: .venv/bin/python capture_sse.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx

PORT = 8765
BASE_URL = f"http://localhost:{PORT}"
OUTPUT_FILE = "sse_events.json"

# Conversation turns to drive the agent through the full flow
TURNS = [
    "I want to build a Notion template marketplace where indie hackers can buy and sell Notion templates.",
    # Answer the expected clarifying question about what's broken with existing options
    "The main problem is for sellers: existing platforms like Gumroad have no discovery for Notion templates "
    "specifically. Buyers struggle to find quality templates filtered by use-case. "
    "Both sides are underserved — sellers can't build an audience and buyers can't trust quality.",
]


async def run_capture() -> None:
    events: list[dict] = []
    turn_index = 0

    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{BASE_URL}/v1/sessions", json={"user_id": "test", "title": "SSE Full Capture"})
        session_id = r.json()["session_id"]
        print(f"Session: {session_id}\n")

        async def read_stream() -> None:
            nonlocal turn_index
            event_name = "unknown"
            session_dones = 0

            async with client.stream("GET", f"{BASE_URL}/v1/chat/stream/{session_id}") as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        raw = line[len("data:"):].strip()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {"raw": raw}

                        events.append({"event": event_name, "data": data})
                        preview = json.dumps(data)[:140]
                        print(f"  [{event_name}] {preview}")

                        if event_name == "session_done":
                            session_dones += 1
                            turn_index += 1
                            if turn_index < len(TURNS):
                                # Send next turn after a brief pause
                                await asyncio.sleep(1)
                                await client.post(
                                    f"{BASE_URL}/v1/chat/send",
                                    json={"session_id": session_id, "message": TURNS[turn_index], "stream": True},
                                )
                                print(f"\n--- Sent turn {turn_index + 1} ---\n")
                            else:
                                # No more turns to send — stop after next session_done
                                # (the agent may still be running interviews on its own)
                                pass

                        if event_name == "session_error":
                            print("  [!] session_error — stopping.")
                            break

                        # Stop when we've seen the final session_done after all turns sent
                        # and the final_report content contains the ColdWater report markers
                        if event_name == "final_report":
                            content = data.get("content", "")
                            if "## The Verdict" in content or "## Confidence Calibration" in content:
                                print("\n  [✓] Final report detected — done.")
                                break

                        # Hard stop: too many session_dones means agent finished multiple rounds
                        if session_dones >= len(TURNS) + 3:
                            break

        stream_task = asyncio.create_task(read_stream())
        await asyncio.sleep(1)

        # Send first turn
        await client.post(
            f"{BASE_URL}/v1/chat/send",
            json={"session_id": session_id, "message": TURNS[0], "stream": True},
        )
        print(f"--- Sent turn 1 ---\n")

        await stream_task

    with open(OUTPUT_FILE, "w") as f:
        json.dump(events, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Captured {len(events)} events → {OUTPUT_FILE}")
    print(f"{'='*60}")

    from collections import Counter
    counts = Counter(e["event"] for e in events)
    for name, count in counts.most_common():
        print(f"  {name:35s} {count}")


def main() -> None:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    print("Starting server...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd="/Users/nithinag/codebase/interrogation_claude",
        env=env,
    )

    for _ in range(20):
        time.sleep(0.5)
        try:
            import urllib.request
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=1)
            print("Server ready.\n")
            break
        except Exception:
            pass
    else:
        print("Server failed to start.")
        proc.terminate()
        return

    try:
        asyncio.run(run_capture())
    finally:
        proc.terminate()
        proc.wait()
        print("Server stopped.")


if __name__ == "__main__":
    main()
