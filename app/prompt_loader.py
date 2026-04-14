from __future__ import annotations

from pathlib import Path


def load_system_prompt(product_context: str | None = None) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "system_prompt.txt"
    base_prompt = prompt_path.read_text(encoding="utf-8").strip()

    if not product_context:
        return base_prompt

    product_section = (
        "\n\n---\n\n"
        "## PRODUCT CONTEXT (PRE-LOADED)\n\n"
        "You have already reviewed the founder's website. "
        "Here is what you know about their product:\n\n"
        f"{product_context}\n\n"
        "You also have access to the `read_product_docs` tool which contains the full "
        "crawled website content organized by page. Use it to look up specific details "
        "(pricing, features, team, etc.) when needed for hypothesis formation or interview design.\n"
        "\n---\n"
    )

    # Insert product context between the intro paragraph and ## TOOLS AVAILABLE
    marker = "## TOOLS AVAILABLE"
    if marker in base_prompt:
        parts = base_prompt.split(marker, 1)
        return parts[0].rstrip() + product_section + "\n" + marker + parts[1]

    # Fallback: prepend after first ---
    return base_prompt + product_section


def _load_prompt_file(filename: str) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / filename
    return prompt_path.read_text(encoding="utf-8").strip()


def load_interviewer_system_prompt(hypothesis: str) -> str:
    template = _load_prompt_file("interviewer_system.txt")
    return template.replace("__HYPOTHESIS__", hypothesis)


def load_customer_system_prompt(persona: str) -> str:
    template = _load_prompt_file("customer_system.txt")
    return template.replace("__PERSONA__", persona)
