import asyncio
from datetime import datetime
import logging
import os
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
# Usar fuso horário de São Paulo como padrão
DEFAULT_TZ = pytz.timezone("America/Sao_Paulo")
scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)

# Current interval (minutes) — exposed so the API can read it
current_interval_min: int = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "30"))


async def _run_heartbeat():
    from app.tools.market import get_market_data
    from app.tools.news import get_crypto_news
    import app.agents.registry as registry

    # ── Weekend guard: Forex fecha sexta 22h UTC → domingo 22h UTC ────────
    now_utc = datetime.now(pytz.utc)
    weekday = now_utc.weekday()  # 0=seg, 4=sex, 5=sáb, 6=dom
    hour_utc = now_utc.hour

    if weekday == 5:  # Sábado — mercado 100% fechado
        logger.info("💤 Sábado — mercado Forex fechado, heartbeat ignorado")
        return
    if weekday == 6 and hour_utc < 22:  # Domingo antes das 22h UTC
        logger.info("💤 Domingo pré-abertura — mercado Forex fechado, heartbeat ignorado")
        return
    if weekday == 4 and hour_utc >= 22:  # Sexta após 22h UTC
        logger.info("💤 Sexta pós-fechamento — mercado Forex fechado, heartbeat ignorado")
        return

    # ── Session guard: só opera entre 07–17 UTC (Londres + NY overlap) ────
    if hour_utc < 7 or hour_utc >= 17:
        logger.info(f"💤 Fora da sessão ({hour_utc:02d}:00 UTC) — HOLD automático, LLM não chamado")
        return

    logger.info("🔔 Heartbeat iniciado")
    try:
        from app.ws_manager import ws_manager
        await ws_manager.broadcast({"type": "heartbeat_start", "ts": datetime.now(DEFAULT_TZ).isoformat()})

        market_data = await get_market_data()
        news = await get_crypto_news()
        lux = registry.get_agent("lux")
        report = await lux.run_heartbeat(market_data, news)
        
        await ws_manager.broadcast({
            "type": "heartbeat_completed",
            "decision": report.decision,
            "asset": report.asset,
            "ts": datetime.now(DEFAULT_TZ).isoformat()
        })
        logger.info(f"✅ Heartbeat concluído — Decisão: {report.decision} | Ativo: {report.asset}")
    except Exception as e:
        logger.error(f"❌ Heartbeat falhou: {e}", exc_info=True)
        from app.tools.telegram import send_alert
        await send_alert("Falha no Heartbeat", f"Erro crítico na execução do ciclo de análise: {str(e)}", level="ERROR")


async def _run_daily_telegram():
    from app.tools.telegram import send_daily_report
    await send_daily_report()


def reschedule_heartbeat(minutes: int):
    """Dynamically change the heartbeat interval without restarting."""
    global current_interval_min
    current_interval_min = minutes
    scheduler.reschedule_job(
        "heartbeat",
        trigger=IntervalTrigger(minutes=minutes),
    )
    logger.info(f"⏱️ Heartbeat reescalonado para {minutes} minutos")


def start_scheduler():
    global current_interval_min
    current_interval_min = int(os.getenv("HEARTBEAT_INTERVAL_MIN", "30"))

    scheduler.add_job(
        _run_heartbeat,
        trigger=IntervalTrigger(minutes=current_interval_min),
        id="heartbeat",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Daily Telegram report — 9:00 AM UTC
    report_hour = int(os.getenv("TELEGRAM_REPORT_HOUR", "9"))
    scheduler.add_job(
        _run_daily_telegram,
        trigger=CronTrigger(hour=report_hour, minute=0),
        id="daily_telegram",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.start()
    logger.info(f"📅 Scheduler iniciado — heartbeat a cada {current_interval_min} minutos")
    logger.info(f"📨 Relatório Telegram diário às {report_hour:02d}:00 horário local")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
