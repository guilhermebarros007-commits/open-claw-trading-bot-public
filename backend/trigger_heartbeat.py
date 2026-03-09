import asyncio
import logging
import os
import sys

# Define base path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

# Configure logging to console
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ManualTrigger")

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

async def manual_heartbeat():
    from app.tools.market import get_market_data
    from app.tools.news import get_crypto_news
    import app.agents.registry as registry
    from app.agents.lux import LuxAgent
    from app.agents.traders import HypeBeastAgent, OracleAgent, VitalikAgent
    from app.memory import db as memory_db

    # Init DB
    await memory_db.init_db()

    # Register agents manually
    registry.agents["lux"] = LuxAgent()
    registry.agents["hype_beast"] = HypeBeastAgent()
    registry.agents["oracle"] = OracleAgent()
    registry.agents["vitalik"] = VitalikAgent()

    logger.info("🔔 Iniciando Heartbeat Manual...")
    
    try:
        # Fetch data
        market_data = await get_market_data()
        news = await get_crypto_news()
        
        lux = registry.get_agent("lux")
        
        # Run heartbeat - this will now trigger real trades!
        report = await lux.run_heartbeat(market_data, news)
        
        print("\n" + "="*50)
        print(f"RESULTADO DO HEARTBEAT")
        print("="*50)
        print(f"Decisão: {report.decision}")
        print(f"Ativo: {report.asset}")
        print(f"Status Trade: {report.trade_status}")
        print(f"Order ID: {report.order_id}")
        print(f"Justificativa (resumo): {report.reasoning[:200]}...")
        print("="*50 + "\n")
        
    except Exception as e:
        logger.error(f"Erro no gatilho manual: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(manual_heartbeat())
