import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
import asyncio
import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.ws_manager import ws_manager
from app.memory import db as memory_db
from app.scheduler import start_scheduler, stop_scheduler, reschedule_heartbeat, current_interval_min
from app.tools.market import get_market_data
from app.tools.news import get_crypto_news
from app.tools.ctrader import get_client as get_ct_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"
VALID_AGENTS = ["lux"]

# ── Logging System ────────────────────────────────────────────────────────────
LOG_QUEUES = []
LOG_BUFFER = deque(maxlen=100)


class LogStreamHandler(logging.Handler):
    def emit(self, record):
        try:
            log_entry = {
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
                "tag": record.name.split(".")[-1].upper(),
                "msg": self.format(record),
                "level": record.levelname,
            }
            LOG_BUFFER.append(log_entry)
            for q in LOG_QUEUES:
                asyncio.run_coroutine_threadsafe(q.put(log_entry), asyncio.get_event_loop())
        except Exception:
            pass


stream_handler = LogStreamHandler()
stream_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger("app").addHandler(stream_handler)
logging.getLogger("app.agents").addHandler(stream_handler)
logging.getLogger("app.scheduler").addHandler(stream_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await memory_db.init_db()
    logger.info("📦 Banco de dados inicializado")

    import app.agents.registry as registry
    from app.agents.lux import LuxAgent

    registry.agents["lux"] = LuxAgent()
    logger.info("🤖 Agente registrado: lux")

    start_scheduler()
    yield

    # Shutdown
    stop_scheduler()


app = FastAPI(title="Trading Agents", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


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
    if not (1 <= req.interval_min <= 120):
        raise HTTPException(status_code=400, detail="interval_min must be between 1 and 120")
    reschedule_heartbeat(req.interval_min)
    return {"interval_min": req.interval_min, "status": "rescheduled"}


@app.get("/api/settings/heartbeat")
async def get_heartbeat_interval():
    import app.scheduler as sched
    return {"interval_min": sched.current_interval_min}


@app.get("/api/scheduler/next_run")
async def get_next_run():
    from app.scheduler import scheduler
    job = scheduler.get_job("heartbeat")
    if not job or not job.next_run_time:
        return {"next_run": None}
    return {"next_run": job.next_run_time.isoformat()}


@app.post("/api/telegram/test")
async def telegram_test():
    from app.tools.telegram import send_message
    ok = await send_message("🤖 <b>Trading Agents</b> — conexão Telegram OK!")
    return {"sent": ok}


@app.post("/api/telegram/report")
async def telegram_report():
    from app.tools.telegram import send_daily_report
    ok = await send_daily_report()
    return {"sent": ok}


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat/{agent_id}/stream")
async def chat_stream(agent_id: str, req: ChatRequest):
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


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/v1/logs/stream")
async def logs_stream():
    async def event_generator():
        queue = asyncio.Queue()
        LOG_QUEUES.append(queue)
        try:
            for entry in list(LOG_BUFFER):
                yield f"data: {json.dumps(entry)}\n\n"
            while True:
                log_entry = await queue.get()
                yield f"data: {json.dumps(log_entry)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            LOG_QUEUES.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── Trades History ────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def trades_list(agent_id: str = None, limit: int = 50):
    return await memory_db.get_trades(agent_id=agent_id, limit=limit)


# ── cTrader: Market & Technical ───────────────────────────────────────────────

@app.get("/api/ct/market")
async def ct_market():
    """Current prices for all Forex pairs."""
    ct = get_ct_client()
    return await ct.get_all_prices()


@app.get("/api/ct/technical")
async def ct_technical():
    """RSI, EMA, MACD, BB, ATR for all tracked pairs."""
    ct = get_ct_client()
    return await ct.get_technical_data()


@app.get("/api/ct/candles")
async def ct_candles(symbol: str = "EURUSD", timeframe: str = "H1", count: int = 100):
    """Raw OHLCV candle data for frontend charts."""
    ct = get_ct_client()
    return await ct.get_market_data(symbol, timeframe, count)


# ── cTrader: Portfolio ────────────────────────────────────────────────────────

@app.get("/api/ct/portfolio")
async def ct_portfolio():
    """Account info + open positions."""
    ct = get_ct_client()
    account, positions = await asyncio.gather(
        ct.get_account_info(),
        ct.list_positions(),
    )
    return [{
        "agent_id": "lux",
        "balance": account.get("balance", 0),
        "equity": account.get("equity", 0),
        "free_margin": account.get("free_margin", 0),
        "positions": positions,
    }]


@app.get("/api/ct/account")
async def ct_account():
    """Account state for Lux."""
    ct = get_ct_client()
    return await ct.get_account_info()


# ── cTrader: Order Execution ──────────────────────────────────────────────────

class CTOrderRequest(BaseModel):
    symbol: str
    is_buy: bool
    volume: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class CTCloseRequest(BaseModel):
    symbol: str
    volume: float
    ticket: int


@app.post("/api/ct/order/open")
async def ct_open_order(req: CTOrderRequest):
    ct = get_ct_client()
    result = await ct.place_order(
        symbol=req.symbol,
        is_buy=req.is_buy,
        volume=req.volume,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
    )
    if result.get("retcode") != 0 and not result.get("success"):
        raise HTTPException(status_code=400, detail=str(result))
    return result


@app.post("/api/ct/order/close")
async def ct_close_order(req: CTCloseRequest):
    ct = get_ct_client()
    ok = await ct.close_position(ticket=req.ticket, symbol=req.symbol, volume=req.volume)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to close position")
    return {"success": True, "ticket": req.ticket}


# ── Legacy aliases (keep /api/hl/ working for frontend compat) ────────────────

@app.get("/api/hl/market")
async def hl_market_alias():
    return await ct_market()


@app.get("/api/hl/technical")
async def hl_technical_alias():
    return await ct_technical()


@app.get("/api/hl/candles")
async def hl_candles_alias(coin: str = "EURUSD", interval: str = "H1", start: int = None, end: int = None):
    return await ct_candles(symbol=coin, timeframe=interval)


@app.get("/api/hl/portfolio")
async def hl_portfolio_alias():
    return await ct_portfolio()


@app.post("/api/hl/order/{agent_id}/close")
async def hl_close_alias(agent_id: str, req: CTCloseRequest):
    return await ct_close_order(req)


# ── Backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:           str   = "EURUSD"
    count:            int   = 500
    use_real_data:    bool  = False
    timeframe:        str   = "M5"
    rsi_buy:          int   = 30
    rsi_sell:         int   = 70
    confluence_min:   int   = 2
    sl_pct:           float = 0.15
    tp_pct:           float = 0.25   # TP1 — fecha 70% da posição
    tp2_pct:          float = 0.45   # TP2 — fecha 30% restante (SL no breakeven)
    partial_exit_pct: float = 0.70   # fração fechada no TP1
    csv_content:      Optional[str] = None


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest):
    from app.tools.backtest import run_backtest as _run
    try:
        result = await _run(
            symbol=req.symbol,
            count=req.count,
            use_real_data=req.use_real_data,
            timeframe=req.timeframe,
            csv_content=req.csv_content,
            rsi_buy=req.rsi_buy,
            rsi_sell=req.rsi_sell,
            confluence_min=req.confluence_min,
            sl_pct=req.sl_pct,
            tp_pct=req.tp_pct,
            tp2_pct=req.tp2_pct,
            partial_exit_pct=req.partial_exit_pct,
        )
        return result.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
