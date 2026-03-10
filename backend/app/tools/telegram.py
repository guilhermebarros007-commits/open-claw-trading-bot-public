"""
Telegram reporter — daily performance report with ZERO AI token calls.
Reads portfolio data directly from Hyperliquid API and formats a static message.
"""
import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "")


async def send_message(text: str) -> bool:
    """Send a message to the configured Telegram chat."""
    token = _token()
    chat_id = _chat_id()
    if not token or not chat_id:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TELEGRAM_API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


async def send_alert(title: str, message: str, level: str = "ERROR") -> bool:
    """Send a critical alert to Telegram with clear visual marking."""
    icons = {"ERROR": "🚨", "WARNING": "⚠️", "CRITICAL": "🧱"}
    icon = icons.get(level, "🚨")
    
    text = (
        f"{icon} <b>SISTEMA - {level}</b> {icon}\n"
        f"<b>{title}</b>\n\n"
        f"<i>{message}</i>\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S UTC')}"
    )
    return await send_message(text)


async def send_daily_report() -> bool:
    """
    Build and send the daily performance report.
    Zero AI tokens — reads portfolio data directly from Hyperliquid API.
    """
    from app.tools.hyperliquid import get_all_accounts, get_hl_market_data

    try:
        accounts, market = await __import__("asyncio").gather(
            get_all_accounts(),
            get_hl_market_data(),
        )
    except Exception as e:
        logger.error(f"Daily report data fetch failed: {e}")
        return False

    now = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    btc = market.get("btc_price", 0)
    eth = market.get("eth_price", 0)
    hype = market.get("hype_price", 0)

    lines = [
        f"📊 <b>Relatório Diário — Trading Agents</b>",
        f"🕐 {now}",
        f"",
        f"<b>Mercado (Testnet HL)</b>",
        f"  BTC  ${btc:,.0f}  |  ETH  ${eth:,.0f}  |  HYPE  ${hype:,.2f}",
        f"",
        f"<b>Performance dos Agentes</b>",
    ]

    emojis = {"hype_beast": "🐗", "oracle": "🔮", "vitalik": "💎"}
    names  = {"hype_beast": "Hype Beast", "oracle": "Oracle", "vitalik": "Vitalik"}
    total_portfolio = 0.0

    for a in accounts:
        if a.get("error"):
            lines.append(f"  {emojis.get(a['agent_id'],'?')} {names.get(a['agent_id'], a['agent_id'])}: ❌ erro")
            continue
        total = a.get("total_value", 0)
        perp  = a.get("account_value", 0)
        spot  = a.get("spot_usdc", 0)
        pnl   = a.get("total_pnl", 0)
        positions = a.get("positions", [])
        total_portfolio += total

        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pos_str = f"{len(positions)} pos" if positions else "sem posição"
        name = names.get(a["agent_id"], a["agent_id"])
        emoji = emojis.get(a["agent_id"], "🤖")

        lines.append(f"  {emoji} <b>{name}</b>: ${total:.2f}  (PnL {pnl_str}  |  {pos_str})")
        if perp > 0:
            lines.append(f"       Perp: ${perp:.2f}  |  Spot: ${spot:.2f}")
        for p in positions:
            pnl_p = p.get("unrealized_pnl", 0)
            side = "🟢 L" if p["side"] == "long" else "🔴 S"
            lines.append(f"       {side} {p['coin']} sz={abs(p['size'])} PnL=${pnl_p:.2f}")

    lines += [
        f"",
        f"💼 <b>Total portfólio: ${total_portfolio:.2f}</b>",
        f"",
        f"🔗 Dashboard: http://72.60.146.212:8001",
    ]

    message = "\n".join(lines)
    ok = await send_message(message)
    if ok:
        logger.info("📨 Daily Telegram report sent")
    return ok
