from app.agents.base import GeminiBaseAgent


class HypeBeastAgent(GeminiBaseAgent):
    def __init__(self):
        super().__init__("hype_beast")


class OracleAgent(GeminiBaseAgent):
    def __init__(self):
        super().__init__("oracle")


class VitalikAgent(GeminiBaseAgent):
    def __init__(self):
        super().__init__("vitalik")
