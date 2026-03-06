from __future__ import annotations

from pathlib import Path


def load_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _load_prompt_file(filename: str) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / filename
    return prompt_path.read_text(encoding="utf-8").strip()


def load_interviewer_system_prompt(hypothesis: str) -> str:
    template = _load_prompt_file("interviewer_system.txt")
    return template.replace("__HYPOTHESIS__", hypothesis)


def load_customer_system_prompt(persona: str) -> str:
    template = _load_prompt_file("customer_system.txt")
    return template.replace("__PERSONA__", persona)
