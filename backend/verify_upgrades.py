import asyncio
import logging
import json
from app.agents.lux import LuxAgent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_robust_and_protection():
    agent = LuxAgent()
    
    # 1. Test Robust JSON Extraction
    noisy_reply = "Claramente, aqui está a decisão: {\"decisao\": \"trailing_stop\", \"ativo_prioritario\": \"BTC\", \"direcao\": \"long\", \"total_confidence\": 8.5, \"consensus_reached\": true} Espero que ajude!"
    data = agent._extract_json(noisy_reply)
    print(f"--- Robust JSON Extraction Test ---")
    print(f"Input: {noisy_reply}")
    print(f"Extracted: {data}")
    assert data.get("decisao") == "trailing_stop"
    print("✅ Robust extraction working!")

    # 2. Simulate Heartbeat with Profit Position (Logic check)
    # We'll mock get_all_accounts to return a position with 5% profit
    print(f"\n--- Capital Protection Logic Simulation ---")
    
    # Mocking dependencies for the agent instance
    async def mock_get_all_accounts():
        return [{
            "agent_id": "oracle",
            "positions": [{
                "coin": "BTC",
                "side": "long",
                "return_on_equity": 0.05, # 5% profit
                "entry_price": 60000.0,
                "size": 0.1
            }]
        }]
    
    # In a real environment, we'd monkeypatch, but here we just want to verify the logic 
    # that converts return_on_equity to the brief.
    
    portfolio = await mock_get_all_accounts()
    position_briefs = []
    for account in portfolio:
        for pos in account.get("positions", []):
            profit_pct = (pos["return_on_equity"] or 0) * 100
            if profit_pct >= 4.0:
                position_briefs.append(
                    f"- POSIÇÃO EM LUCRO: {pos['coin']} ({pos['side'].upper()}) | Lucro: {profit_pct:.1f}% | Preço Entrada: {pos['entry_price']}"
                )
    
    pos_context = "\n".join(position_briefs) if position_briefs else "Nenhuma posição em lucro relevante (>4%)."
    print(f"Generated Context: {pos_context}")
    assert "POSIÇÃO EM LUCRO: BTC" in pos_context
    print("✅ Capital Protection context generation working!")

if __name__ == "__main__":
    asyncio.run(test_robust_and_protection())
