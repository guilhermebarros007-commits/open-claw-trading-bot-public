import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.memory import db as memory_db
from app.scheduler import start_scheduler, stop_scheduler, reschedule_heartbeat, current_interval_min
from app.tools.market import get_market_data
from app.tools.news import get_crypto_news
from app.tools.hyperliquid import (
    get_hl_market_data,
    get_technical_data,
    get_account_state,
    get_all_accounts,
    execute_market_open,
    execute_market_close,
    execute_limit_order,
    cancel_all_orders,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"
VALID_AGENTS = ["lux", "hype_beast", "oracle", "vitalik"]
TRADER_AGENTS = ["hype_beast", "oracle", "vitalik"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await memory_db.init_db()
    logger.info("📦 Banco de dados inicializado")

    # Register agents
    import app.agents.registry as registry
    from app.agents.lux import LuxAgent
    from app.agents.traders import HypeBeastAgent, OracleAgent, VitalikAgent

    registry.agents["lux"] = LuxAgent()
    registry.agents["hype_beast"] = HypeBeastAgent()
    registry.agents["oracle"] = OracleAgent()
    registry.agents["vitalik"] = VitalikAgent()
    logger.info("🤖 Agentes registrados: " + ", ".join(registry.agents.keys()))

    start_scheduler()
    yield

    # Shutdown
    stop_scheduler()


app = FastAPI(title="Trading Agents", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Market & News ─────────────────────────────────────────────────────────────

@app.get("/api/market")
async def api_market():
    return await get_market_data()


@app.get("/api/news")
async def api_news():
    return await get_crypto_news()


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@app.get("/api/heartbeat/history")
async def heartbeat_history(limit: int = 20):
    return await memory_db.get_reports(limit=limit)


@app.post("/api/heartbeat/trigger")
async def heartbeat_trigger():
    import app.agents.registry as registry

    market_data = await get_market_data()
    news = await get_crypto_news()
    lux = registry.get_agent("lux")
    report = await lux.run_heartbeat(market_data, news)
    return report.to_dict()


class HeartbeatSettings(BaseModel):
    interval_min: int


@app.post("/api/settings/heartbeat")
async def update_heartbeat_interval(req: HeartbeatSettings):
    """Dynamically change heartbeat interval (1–120 min)."""
    if not (1 <= req.interval_min <= 120):
        raise HTTPException(status_code=400, detail="interval_min must be between 1 and 120")
    reschedule_heartbeat(req.interval_min)
    return {"interval_min": req.interval_min, "status": "rescheduled"}


@app.get("/api/settings/heartbeat")
async def get_heartbeat_interval():
    import app.scheduler as sched
    return {"interval_min": sched.current_interval_min}


@app.post("/api/telegram/test")
async def telegram_test():
    """Send a test message to Telegram."""
    from app.tools.telegram import send_message
    ok = await send_message("🤖 <b>Trading Agents</b> — conexão Telegram OK!")
    return {"sent": ok}


@app.post("/api/telegram/report")
async def telegram_report():
    """Trigger the daily Telegram report manually."""
    from app.tools.telegram import send_daily_report
    ok = await send_daily_report()
    return {"sent": ok}


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat/{agent_id}/stream")
async def chat_stream(agent_id: str, req: ChatRequest):
    """SSE streaming endpoint — yields text/event-stream chunks."""
    if agent_id not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    import app.agents.registry as registry
    agent = registry.get_agent(agent_id)

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for chunk in agent.stream_chat(req.message):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/{agent_id}")
async def chat(agent_id: str, req: ChatRequest):
    """Non-streaming fallback."""
    if agent_id not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    import app.agents.registry as registry
    agent = registry.get_agent(agent_id)
    reply = await agent.chat(req.message)
    return {"agent_id": agent_id, "reply": reply}


@app.get("/api/chat/{agent_id}/history")
async def chat_history(agent_id: str, limit: int = 20):
    if agent_id not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return await memory_db.get_chat_history(agent_id, limit=limit)


# ── Agent Memory ──────────────────────────────────────────────────────────────

@app.get("/api/agents/{agent_id}/memory")
async def agent_memory(agent_id: str):
    if agent_id not in VALID_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    import app.agents.registry as registry
    agent = registry.get_agent(agent_id)
    memory_path = agent.workspace / "MEMORY.md"
    content = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    return {"agent_id": agent_id, "memory": content}


# ── Hyperliquid: Market & Technical ──────────────────────────────────────────

@app.get("/api/hl/market")
async def hl_market():
    """Real-time prices + funding rates + OI from Hyperliquid."""
    return await get_hl_market_data()


@app.get("/api/hl/technical")
async def hl_technical():
    """RSI(14) + EMA9/21 for BTC, ETH, HYPE from 1h candles."""
    return await get_technical_data()


# ── Hyperliquid: Portfolio ────────────────────────────────────────────────────

@app.get("/api/hl/portfolio")
async def hl_portfolio():
    """Account state (positions, PnL, margin) for all trader agents."""
    return await get_all_accounts()


@app.get("/api/hl/account/{agent_id}")
async def hl_account(agent_id: str):
    """Account state for a specific agent."""
    if agent_id not in TRADER_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not a trader")
    return await get_account_state(agent_id)


# ── Hyperliquid: Order Execution ──────────────────────────────────────────────

class MarketOrderRequest(BaseModel):
    coin: str
    is_buy: bool
    size: float
    slippage: float = 0.01


class LimitOrderRequest(BaseModel):
    coin: str
    is_buy: bool
    size: float
    price: float
    reduce_only: bool = False


class CloseRequest(BaseModel):
    coin: str
    size: Optional[float] = None


@app.post("/api/hl/order/{agent_id}/market")
async def hl_market_order(agent_id: str, req: MarketOrderRequest):
    """Open a market position for an agent."""
    if agent_id not in TRADER_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not a trader")
    result = await execute_market_open(agent_id, req.coin, req.is_buy, req.size, req.slippage)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Order failed"))
    return result


@app.post("/api/hl/order/{agent_id}/limit")
async def hl_limit_order(agent_id: str, req: LimitOrderRequest):
    """Place a GTC limit order for an agent."""
    if agent_id not in TRADER_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not a trader")
    result = await execute_limit_order(
        agent_id, req.coin, req.is_buy, req.size, req.price, req.reduce_only
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Order failed"))
    return result


@app.post("/api/hl/order/{agent_id}/close")
async def hl_close_position(agent_id: str, req: CloseRequest):
    """Close a position for an agent."""
    if agent_id not in TRADER_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not a trader")
    result = await execute_market_close(agent_id, req.coin, req.size)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Close failed"))
    return result


@app.delete("/api/hl/orders/{agent_id}/{coin}")
async def hl_cancel_orders(agent_id: str, coin: str):
    """Cancel all open orders for a coin/agent."""
    if agent_id not in TRADER_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not a trader")
    return await cancel_all_orders(agent_id, coin)
