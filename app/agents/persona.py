from claude_agent_sdk import AgentDefinition


class Persona(AgentDefinition):
    def __init__(self, persona: str):
        super().__init__(
            description=f"Persona simulator for: {persona}",
            prompt=(
                "You are an excellent human mimicker. "
                f"Respond consistently as this persona: {persona}"
            ),
            tools=None,
            model="inherit",
        )
        