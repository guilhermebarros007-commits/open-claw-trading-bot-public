from app.agents.base import BaseAgent

# Populated at startup in main.py
agents: dict[str, BaseAgent] = {}


async def call_agent(agent_id: str, message: str, extra_context: str = "") -> str:
    agent = agents.get(agent_id)
    if not agent:
        raise ValueError(f"Agent '{agent_id}' not found in registry")
    return await agent.chat(message, extra_context=extra_context)


def get_agent(agent_id: str) -> BaseAgent:
    agent = agents.get(agent_id)
    if not agent:
        raise ValueError(f"Agent '{agent_id}' not found in registry")
    return agent
