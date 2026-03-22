import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from app.agents.base import GeminiBaseAgent
from app.tools.news import format_news_summary
from app.tools.ctrader import get_client

logger = logging.getLogger(__name__)

FOREX_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
MIN_CONFIDENCE = 6.0    # scalper: mais agressivo (era 6.5)
MIN_CONFLUENCE = 2      # scalper: 2 de 4 indicadores (era 4)

# ── Scalper parameters ─────────────────────────────────────────────────────────
LOT_BASE   = 0.05    # lote base → ~8.9%/mês sobre $500
LOT_HIGH   = 0.07    # lote alta confiança (≥8.5) → ~12%/mês
SL_PCT     = 0.0015  # 0.15% ≈ 15 pips EURUSD
TP_PCT     = 0.0025  # 0.25% ≈ 25 pips EURUSD   ← TP1: fecha 70% da posição
TP2_PCT    = 0.0045  # 0.45% ≈ 40 pips EURUSD   ← TP2: fecha 30% restante (SL no BE)

# ── Risk limits ────────────────────────────────────────────────────────────────
MAX_OPEN_POSITIONS = 2       # máx posições simultâneas abertas
MAX_DAILY_TRADES   = 6       # scalper: até 6 trades/dia (era 3)
DAILY_LOSS_LIMIT   = 0.04    # para execução se perda diária > 4% do equity


@dataclass
class HeartbeatReport:
    id: Optional[int]
    decision: str        # COMPRAR / VENDER / HOLD / trailing_stop
    asset: str           # EURUSD / GBPUSD / ... / none
    direction: str       # long / short / none
    reasoning: str
    lux_raw: str
    market_snapshot: dict
    news_count: int
    trade_status: str = "none"
    order_id: Optional[str] = None
    created_at: str = ""

    def to_dict(self):
        return asdict(self)


class LuxAgent(GeminiBaseAgent):
    def __init__(self):
        super().__init__("lux")

    async def run_heartbeat(self, market_data: dict, news: list) -> HeartbeatReport:
        from app.memory import db as memory_db

        ct = get_client()

        # ── Fetch cTrader data in parallel ────────────────────────────────────
        await self.log_event("Buscando dados de mercado cTrader...")
        try:
            account, positions, tech_data = await asyncio.gather(
                ct.get_account_info(),
                ct.list_positions(),
                ct.get_technical_data(FOREX_PAIRS),
                return_exceptions=True,
            )
            if isinstance(account, Exception):
                logger.warning(f"Account fetch failed: {account}")
                account = {}
            if isinstance(positions, Exception):
                logger.warning(f"Positions fetch failed: {positions}")
                positions = []
            if isinstance(tech_data, Exception):
                logger.warning(f"Technical data failed: {tech_data}")
                tech_data = {}
        except Exception as e:
            logger.error(f"cTrader data fetch error: {e}")
            account, positions, tech_data = {}, [], {}

        # ── Build market brief ─────────────────────────────────────────────────
        market_brief = _build_market_brief(account, positions, tech_data, news)

        # ── Single LLM call ────────────────────────────────────────────────────
        await self.log_event("Analisando mercado e tomando decisão...")
        lux_raw = await self.chat(market_brief)

        # ── Parse decision ─────────────────────────────────────────────────────
        lux_data = self._extract_json(lux_raw)
        decision, asset, direction = _parse_decision(lux_data)
        total_confidence = float(lux_data.get("total_confidence", 0))
        confluencia = int(lux_data.get("confluencia_count", 0))

        await self.log_event(
            f"Decisão: {decision} {asset} ({direction}) | "
            f"Confiança: {total_confidence} | Confluência: {confluencia}/4"
        )

        # ── Execution ─────────────────────────────────────────────────────────
        trade_info = {"status": "none", "order_id": None}

        if (
            decision in ["COMPRAR", "VENDER"]
            and asset != "none"
            and total_confidence >= MIN_CONFIDENCE
            and confluencia >= MIN_CONFLUENCE
        ):
            # ── Risk Audit ────────────────────────────────────────────────────
            audit_ok, audit_reason = await _risk_audit(positions, account, memory_db)
            if not audit_ok:
                await self.log_event(f"🛡️ Risk Audit bloqueou execução: {audit_reason}", "warn")
                trade_info["status"] = f"blocked: {audit_reason}"

            # Check for existing open position on same asset
            elif asset in [p.get("symbol", "") for p in positions]:
                await self.log_event(f"⚠️ Posição já aberta em {asset} — ignorando sinal", "warn")
                trade_info["status"] = "skipped: position already open"
            else:
                is_buy = (direction == "long")
                try:
                    price_info = await ct.get_symbol_price(asset)
                    price = price_info.get("ask" if is_buy else "bid", 0)

                    # Scalper: SL/TP em pips apertados (validado por backteste)
                    sl_pct = float(lux_data.get("stop_loss_pct", 0.15)) / 100
                    tp_pct = float(lux_data.get("take_profit_pct", 0.25)) / 100
                    # Sanitiza: scalper nunca usa > 0.5% SL ou > 1% TP
                    if sl_pct > 0.005 or sl_pct <= 0: sl_pct = SL_PCT
                    if tp_pct > 0.01  or tp_pct <= 0: tp_pct = TP_PCT
                    sl_price = round(price * (1 - sl_pct if is_buy else 1 + sl_pct), 5)
                    tp_price = round(price * (1 + tp_pct if is_buy else 1 - tp_pct), 5)

                    # Volume dinâmico: 0.05 base → ~8.9%/mês | 0.07 alta conf → ~12%/mês
                    volume = LOT_HIGH if total_confidence >= 8.5 else LOT_BASE

                    await self.log_event(
                        f"🚀 Executando {'BUY' if is_buy else 'SELL'} {volume} {asset} "
                        f"@ {price} | SL={sl_price} TP={tp_price}"
                    )

                    result = await ct.place_order(
                        symbol=asset,
                        is_buy=is_buy,
                        volume=volume,
                        stop_loss=sl_price,
                        take_profit=tp_price,
                    )

                    if result.get("success") or result.get("retcode") == 0:
                        ticket = result.get("ticket")
                        trade_info["status"] = "executed"
                        trade_info["order_id"] = str(ticket)

                        await memory_db.save_trade(
                            agent_id="lux",
                            coin=asset,
                            side="long" if is_buy else "short",
                            size=volume,
                            entry_price=price,
                            sl_price=sl_price,
                            tp_price=tp_price,
                            status="executed",
                            confidence=total_confidence,
                            decision_json=json.dumps(lux_data),
                            order_result=json.dumps(result),
                        )
                        logger.info(f"📝 Trade logged: lux {asset} {'long' if is_buy else 'short'} ticket={ticket}")
                    else:
                        trade_info["status"] = f"failed: {result}"
                except Exception as trade_err:
                    logger.error(f"Trade execution error: {trade_err}")
                    trade_info["status"] = f"error: {str(trade_err)}"

        # ── Trailing stop / saída parcial ─────────────────────────────────────
        # RODA SEMPRE quando há posições — NÃO depende do LLM decidir "trailing_stop"
        # Isso alinha o comportamento live com o backtest (que checa a cada barra)
        if positions:
            for pos in positions:
                profit_pct = float(pos.get("profit_pct", 0))
                ticket     = pos.get("ticket")
                symbol_pos = pos.get("symbol", "")
                entry      = float(pos.get("open_price", 0))
                is_long    = int(pos.get("type", 0)) == 0

                if entry <= 0 or not ticket:
                    continue

                try:
                    if profit_pct >= TP_PCT * 100:
                        # ── TP1 atingido (+0.25%): SL → breakeven, TP → TP2 (+0.45%)
                        new_sl = round(entry * ((1 + SL_PCT * 0.1) if is_long else (1 - SL_PCT * 0.1)), 5)
                        new_tp = round(entry * ((1 + TP2_PCT) if is_long else (1 - TP2_PCT)), 5)
                        await ct.modify_sl_tp(ticket, new_sl, new_tp)
                        await self.log_event(
                            f"🛡️ TP1 atingido em {symbol_pos} (+{profit_pct:.2f}%) | "
                            f"SL → breakeven={new_sl} | TP → TP2={new_tp}"
                        )
                        trade_info["status"] = "tp1_trailing_to_tp2"

                    elif profit_pct > 0.05:
                        # ── Lucro >0.05%: trail apertado — garante 50% do ganho
                        trail_sl = round(entry * (
                            (1 + (profit_pct / 100) * 0.5) if is_long
                            else (1 - (profit_pct / 100) * 0.5)
                        ), 5)
                        await ct.modify_sl_tp(ticket, trail_sl, None)
                        await self.log_event(
                            f"📈 Trailing parcial em {symbol_pos} (+{profit_pct:.2f}%) | SL={trail_sl}"
                        )
                        trade_info["status"] = "trailing_partial"
                except Exception as trail_err:
                    logger.warning(f"Trailing stop error for {symbol_pos}: {trail_err}")

        # ── Memory ────────────────────────────────────────────────────────────
        mem_entry = (
            f"- Equity: ${account.get('equity', 0):.2f} | Posições: {len(positions)}\n"
            f"- Decisão: {decision} | Par: {asset} | Direção: {direction} | Conf: {total_confidence}"
        )
        await self.append_memory(mem_entry)

        # ── Save report ───────────────────────────────────────────────────────
        report_id = await memory_db.save_report(
            market_data=market_data,
            news=news,
            hype_analysis="",
            oracle_analysis="",
            vitalik_analysis="",
            lux_decision=decision,
            lux_raw=lux_raw,
        )

        return HeartbeatReport(
            id=report_id,
            decision=decision,
            asset=asset,
            direction=direction,
            reasoning=lux_raw[:500],
            lux_raw=lux_raw,
            market_snapshot=market_data,
            news_count=len(news),
            trade_status=trade_info["status"],
            order_id=trade_info["order_id"],
            created_at=datetime.utcnow().isoformat(),
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _risk_audit(positions: list, account: dict, memory_db) -> tuple[bool, str]:
    """
    Verifica limites de risco antes de executar qualquer trade.
    Retorna (pode_operar, motivo).
    """
    equity = float(account.get("equity", 500))

    # 1. Máximo de posições simultâneas
    if len(positions) >= MAX_OPEN_POSITIONS:
        return False, f"{len(positions)} posições abertas (máx {MAX_OPEN_POSITIONS})"

    # 2. Máximo de trades por dia
    today_trades = await memory_db.get_trades_today("lux")
    if len(today_trades) >= MAX_DAILY_TRADES:
        return False, f"{len(today_trades)} trades hoje (máx {MAX_DAILY_TRADES})"

    # 3. Limite de perda diária (soma do risco máximo dos trades de hoje)
    #    Cada trade arrisca sl_pct * volume * entry_price → estimamos como (entry - sl) * volume
    daily_risk = 0.0
    for t in today_trades:
        entry = float(t.get("entry_price") or 0)
        sl = float(t.get("sl_price") or 0)
        size = float(t.get("size") or 0.01)
        if entry > 0 and sl > 0:
            daily_risk += abs(entry - sl) * size * 100_000  # pip value approx
    if equity > 0 and daily_risk / equity >= DAILY_LOSS_LIMIT:
        return False, f"risco diário acumulado ${daily_risk:.2f} ≥ {DAILY_LOSS_LIMIT*100:.0f}% do equity"

    return True, "ok"


def _parse_decision(data: dict) -> tuple[str, str, str]:
    decisao_raw = str(data.get("decisao", "hold")).lower()
    if any(k in decisao_raw for k in ["comprar", "buy", "executar"]):
        decision = "COMPRAR"
    elif any(k in decisao_raw for k in ["vender", "sell"]):
        decision = "VENDER"
    elif "trailing" in decisao_raw or "stop" in decisao_raw:
        decision = "trailing_stop"
    else:
        decision = "HOLD"

    asset = str(data.get("par", "none")).upper()
    if asset not in ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]:
        asset = "none"

    direction = str(data.get("direcao", "none")).lower()
    if direction not in ["long", "short"]:
        direction = "none"

    return decision, asset, direction


def _build_market_brief(
    account: dict,
    positions: list,
    tech_data: dict,
    news: list,
) -> str:
    sections = []

    # Account
    sections.append(
        f"## Conta cTrader\n"
        f"- Balance: ${account.get('balance', 0):.2f}\n"
        f"- Equity: ${account.get('equity', 0):.2f}\n"
        f"- Margem livre: ${account.get('free_margin', 0):.2f}\n"
        f"- Posições abertas: {len(positions)}"
    )

    # Open positions
    if positions:
        pos_lines = []
        for p in positions:
            side = "BUY" if int(p.get("type", 0)) == 0 else "SELL"
            pos_lines.append(
                f"  - {p.get('symbol')} {side} {p.get('volume')} lotes "
                f"| Entrada: {p.get('open_price')} | P&L: ${p.get('profit', 0):.2f}"
            )
        sections.append("## Posições Abertas\n" + "\n".join(pos_lines))
    else:
        sections.append("## Posições Abertas\nNenhuma posição aberta.")

    # Technical data
    tech_lines = ["## Análise Técnica (H1 candles)"]
    for sym, ind in tech_data.items():
        if not ind:
            continue
        ema_signal = "BULL" if ind.get("ema_bull") else "BEAR"
        macd_signal = "BULL" if ind.get("macd_bull") else "BEAR"
        tech_lines.append(
            f"\n### {sym}\n"
            f"- Preço: {ind.get('price')}\n"
            f"- RSI(14): {ind.get('rsi')}\n"
            f"- EMA9/21: {ind.get('ema9')}/{ind.get('ema21')} → {ema_signal}\n"
            f"- MACD: {macd_signal} (hist: {ind.get('macd_hist')})\n"
            f"- BB%: {ind.get('bb_pct')} (0=banda inf, 1=banda sup)\n"
            f"- ATR: {ind.get('atr')}\n"
            f"- OBV: {'↑ crescente' if ind.get('obv_rising') else '↓ decrescente'}"
        )
    sections.append("\n".join(tech_lines))

    # News
    sections.append(f"## Headlines Recentes\n{format_news_summary(news)}")

    sections.append(
        "## Instrução\n"
        "Execute as 3 fases do protocolo (Macro → RSI → Confluência) para cada par e "
        "emita sua decisão final exclusivamente em JSON conforme SOUL.md."
    )

    return "\n\n".join(sections)
