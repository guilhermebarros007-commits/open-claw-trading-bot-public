import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime

from app.agents.base import BaseAgent, GeminiBaseAgent
from app.tools.market import format_market_summary
from app.tools.news import format_news_summary
from app.tools.hyperliquid import (
    get_hl_market_data,
    get_technical_data,
    format_hl_market_summary,
    format_technical_summary,
    get_available_usdc,
    execute_market_open,
    execute_limit_order,
    get_sz_decimals,
    round_to_hl_standard,
    get_all_accounts,
    update_sl_trigger,
)
from app import agents as registry_module

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatReport:
    id: int | None
    decision: str       # COMPRAR / VENDER / AGUARDAR
    asset: str          # BTC / ETH / HYPE / none
    direction: str      # long / short / none
    reasoning: str
    hype_analysis: str
    oracle_analysis: str
    vitalik_analysis: str
    lux_raw: str
    market_snapshot: dict
    news_count: int
    trade_status: str = "none"
    order_id: str | None = None
    created_at: str = ""

    def to_dict(self):
        return asdict(self)


def _extract_decision(lux_response: str) -> tuple[str, str, str]:
    """Extract decision, asset, direction from Lux JSON response."""
    try:
        # Try to find JSON block
        match = re.search(r'\{[^{}]*"decisao"[^{}]*\}', lux_response, re.DOTALL)
        if match:
            data = json.loads(match.group())
            decisao = data.get("decisao", "hold").lower()
            ativo = data.get("ativo_prioritario", "none")
            direcao = data.get("direcao", "none")
            decision = "AGUARDAR"
            if "executar" in decisao or "comprar" in decisao or "buy" in decisao:
                decision = "COMPRAR"
            elif "vender" in decisao or "sell" in decisao:
                decision = "VENDER"
            return decision, ativo, direcao
    except Exception:
        pass
    return "AGUARDAR", "none", "none"


def _extract_gold_decision(lux_response: str) -> tuple[str, str, str, dict]:
    """Extract decision and consensus metadata from Lux Gold Standard response."""
    try:
        # Improved JSON extraction to handle nested objects and confidence
        match = re.search(r'(\{.*\})', lux_response, re.DOTALL)
        if match:
            data = json.loads(match.group())
            decisao = data.get("decisao", "hold").lower()
            ativo = data.get("ativo_prioritario", "none")
            direcao = data.get("direcao", "none")
            
            decision = "AGUARDAR"
            if any(k in decisao for k in ["executar", "comprar", "buy"]):
                decision = "COMPRAR"
            elif any(k in decisao for k in ["vender", "sell"]):
                decision = "VENDER"
                
            return decision, ativo, direcao, data
    except Exception:
        pass
    return "AGUARDAR", "none", "none", {}


def _build_market_brief(
    market_data: dict,
    news: list,
    hl_data: dict | None = None,
    tech_data: dict | None = None,
) -> str:
    sections = []

    # Hyperliquid perp prices + funding (primary source)
    if hl_data and not hl_data.get("error"):
        sections.append(f"## Preços Hyperliquid (Perps)\n{format_hl_market_summary(hl_data)}")
    else:
        sections.append(f"## Dados de Mercado (CoinGecko)\n{format_market_summary(market_data)}")

    # BTC dominance from CoinGecko (not available on HL)
    dom = market_data.get("btc_dominance", 0)
    if dom:
        sections.append(f"## Macro\nBTC Dominância: {dom:.1f}%")

    # RSI + EMA technical data
    if tech_data:
        sections.append(f"## Análise Técnica (1h candles)\n{format_technical_summary(tech_data)}")

    sections.append(f"## Headlines Recentes\n{format_news_summary(news)}")
    return "\n\n".join(sections)


def _summarize_for_memory(analysis: str, market_data: dict, agent_id: str) -> str:
    btc = market_data.get("btc_price", 0)
    btc_chg = market_data.get("btc_change_24h", 0)
    dom = market_data.get("btc_dominance", 0)
    # Extract JSON sinal if present
    sinal = "hold"
    try:
        match = re.search(r'"sinal"\s*:\s*"(\w+)"', analysis)
        if match:
            sinal = match.group(1)
    except Exception:
        pass
    conf = "?"
    try:
        match = re.search(r'"confianca"\s*:\s*([\d.]+)', analysis)
        if match:
            conf = match.group(1)
    except Exception:
        pass
    return (
        f"- BTC: ${btc:,.0f} ({btc_chg:+.1f}% 24h), Dominância: {dom:.1f}%\n"
        f"- Sinal: {sinal} | Confiança: {conf}"
    )


class LuxAgent(GeminiBaseAgent):
    def __init__(self):
        super().__init__("lux")

    async def run_heartbeat(self, market_data: dict, news: list) -> HeartbeatReport:
        from app.memory import db as memory_db
        import app.agents.registry as registry

        # Fetch HL-native market + technical data in parallel
        try:
            hl_data, tech_data = await asyncio.gather(
                get_hl_market_data(),
                get_technical_data(),
                return_exceptions=True,
            )
            if isinstance(hl_data, Exception):
                logger.warning(f"HL market data failed: {hl_data}")
                hl_data = None
            if isinstance(tech_data, Exception):
                logger.warning(f"HL technical data failed: {tech_data}")
                tech_data = None
        except Exception as e:
            logger.warning(f"HL data fetch error: {e}")
            hl_data, tech_data = None, None

        # ── Capital Protection Check ─────────────────────────────────────────
        portfolio = await get_all_accounts()
        position_briefs = []
        for account in portfolio:
            for pos in account.get("positions", []):
                profit_pct = (pos["return_on_equity"] or 0) * 100
                if profit_pct >= 4.0:
                    position_briefs.append(
                        f"- POSIÇÃO EM LUCRO: {pos['coin']} ({pos['side'].upper()}) | Lucro: {profit_pct:.1f}% | Preço Entrada: {pos['entry_price']}"
                    )
        
        pos_context = "\n".join(position_briefs) if position_briefs else "Nenhuma posição em lucro relevante (>4%)."

        market_brief = _build_market_brief(market_data, news, hl_data, tech_data)
        market_brief += f"\n\n### Status de Posições Ativas\n{pos_context}"

        trader_prompt = (
            f"{market_brief}\n\n"
            "Analise esses dados de acordo com sua estratégia e responda em JSON no formato do seu SOUL.md."
        )

        # Call each trader
        hype_analysis = await registry.call_agent("hype_beast", trader_prompt)
        oracle_analysis = await registry.call_agent("oracle", trader_prompt)
        vitalik_analysis = await registry.call_agent("vitalik", trader_prompt)

        # Lux aggregates
        aggregate_prompt = (
            f"{market_brief}\n\n"
            f"## Análises dos Traders\n\n"
            f"### Hype Beast (HYPE/USDC)\n{hype_analysis}\n\n"
            f"### Oracle (BTC/USDC)\n{oracle_analysis}\n\n"
            f"### Vitalik (ETH/USDC)\n{vitalik_analysis}\n\n"
            "Avalie os sinais com critério SHARP e utilize a NOVA REGRA DE CONSENSO GOLD STANDARD:\n"
            "1. Verifique se há concordância de direção entre pelo menos 2 traders OU se um sinal tem confianca > 8.0.\n"
            "2. Calcule 'total_confidence' como a média das confianças dos traders que concordam com o sinal.\n"
            "3. Se houver divergência total (um compra outro vende o mesmo ativo), a decisão deve ser HOLD.\n"
        )
        lux_raw = await self.chat(aggregate_prompt)

        # ── Extraction with Gold Standard Consensus ──────────────────────────
        decision, asset, direction, lux_data = _extract_gold_decision(lux_raw)
        # Use robust extraction from base class if raw extraction failed
        if not lux_data:
             lux_data = self._extract_json(lux_raw)
             decision = lux_data.get("decisao", "AGUARDAR").upper()
             asset = lux_data.get("ativo_prioritario", "none").upper()
             direction = lux_data.get("direcao", "none")

        total_confidence = lux_data.get("total_confidence", 0)
        consensus = lux_data.get("consensus_reached", False) or (decision != "AGUARDAR")

        # ── Execution Logic (Gold Standard) ──────────────────────────────────
        trade_info = {"status": "none", "order_id": None}
        if decision in ["COMPRAR", "VENDER"] and asset in ["BTC", "ETH", "HYPE"] and consensus:
            try:
                from app.tools.hyperliquid import execute_trigger_order
                
                agent_mapping = {"BTC": "oracle", "ETH": "vitalik", "HYPE": "hype_beast"}
                target_agent = agent_mapping.get(asset)

                if target_agent:
                    balance = await get_available_usdc(target_agent)
                    # Dynamic Risk Sizing based on confidence
                    confidence_factor = min(float(total_confidence) / 10.0, 1.0)
                    risk_amount = balance * 0.10 * (0.5 + (confidence_factor * 0.5)) # Scaling between 5% and 10%

                    if risk_amount >= 10:
                        is_buy = (decision == "COMPRAR")
                        price = hl_data.get(f"{asset.lower()}_price", 0)
                        
                        if price > 0:
                            raw_sz = risk_amount / price
                            sz_dec = await get_sz_decimals(asset)
                            px_str, sz_str = round_to_hl_standard(price, raw_sz, sz_dec)
                            
                            # Dynamic Slippage
                            target_slippage = 0.005 if asset in ["BTC", "ETH"] else 0.015
                            
                            logger.info(f"🚀 [GOLD STANDARD] Executing {decision} {asset} (Conf: {total_confidence})")
                            exec_res = await execute_market_open(
                                agent_id=target_agent,
                                coin=asset,
                                is_buy=is_buy,
                                size=float(sz_str),
                                slippage=target_slippage
                            )
                            
                            if exec_res.get("success"):
                                trade_info["status"] = "executed"
                                trade_info["order_id"] = exec_res.get("result", {}).get("response", {}).get("data", {}).get("statuses", [{}])[0].get("resting", {}).get("oid")
                                
                                # Move to Native Exchange Triggers
                                sl_price = price * (0.97 if is_buy else 1.03) # 3% SL
                                tp_price = price * (1.10 if is_buy else 0.90) # 10% TP
                                
                                sl_px_str, _ = round_to_hl_standard(sl_price, float(sz_str), sz_dec)
                                tp_px_str, _ = round_to_hl_standard(tp_price, float(sz_str), sz_dec)
                                
                                # Concurrent Trigger Placement
                                await asyncio.gather(
                                    execute_trigger_order(target_agent, asset, not is_buy, float(sz_str), float(sl_px_str), "sl"),
                                    execute_trigger_order(target_agent, asset, not is_buy, float(sz_str), float(tp_px_str), "tp")
                                )
                                logger.info(f"🛡️ [GOLD STANDARD] Native triggers set at {sl_px_str}/{tp_px_str}")
                            else:
                                trade_info["status"] = f"failed: {exec_res.get('error')}"
                        else:
                            trade_info["status"] = "failed: price not found"
                    else:
                        trade_info["status"] = f"skipped: balance too low (${balance:.2f})"
            except Exception as trade_err:
                logger.error(f"Trade execution error: {trade_err}")
                trade_info["status"] = f"error: {str(trade_err)}"

        # ── Capital Protection Execution ─────────────────────────────────────
        if decision == "trailing_stop" or lux_data.get("decisao") == "trailing_stop":
             for account in portfolio:
                for pos in account.get("positions", []):
                    profit_pct = (pos["return_on_equity"] or 0) * 100
                    if profit_pct >= 4.0:
                        # Move SL to breakeven + 1% profit
                        is_long = pos["side"] == "long"
                        entry = pos["entry_price"]
                        new_sl = entry * (1.01 if is_long else 0.99)
                        
                        sz_dec = await get_sz_decimals(pos["coin"])
                        sl_px_str, _ = round_to_hl_standard(new_sl, abs(pos["size"]), sz_dec)
                        
                        logger.info(f"🛡️ [CAPITAL PROTECTION] Moving SL for {pos['coin']} to {sl_px_str} (Profit: {profit_pct:.1f}%)")
                        await update_sl_trigger(
                            agent_id=account["agent_id"],
                            coin=pos["coin"],
                            new_trigger_px=float(sl_px_str),
                            size=abs(pos["size"]),
                            is_buy=not is_long
                        )
                        trade_info["status"] = "capital_protected"

        # Update memories
        mem_entry_hype = _summarize_for_memory(hype_analysis, market_data, "hype_beast")
        mem_entry_oracle = _summarize_for_memory(oracle_analysis, market_data, "oracle")
        mem_entry_vitalik = _summarize_for_memory(vitalik_analysis, market_data, "vitalik")
        mem_entry_lux = (
            f"- BTC: ${market_data.get('btc_price', 0):,.0f} ({market_data.get('btc_change_24h', 0):+.1f}% 24h)\n"
            f"- Decisão: {decision} | Ativo: {asset} | Direção: {direction}"
        )

        await registry.get_agent("hype_beast").append_memory(mem_entry_hype)
        await registry.get_agent("oracle").append_memory(mem_entry_oracle)
        await registry.get_agent("vitalik").append_memory(mem_entry_vitalik)
        await self.append_memory(mem_entry_lux)

        # Save to DB
        report_id = await memory_db.save_report(
            market_data=market_data,
            news=news,
            hype_analysis=hype_analysis,
            oracle_analysis=oracle_analysis,
            vitalik_analysis=vitalik_analysis,
            lux_decision=decision,
            lux_raw=lux_raw,
        )

        return HeartbeatReport(
            id=report_id,
            decision=decision,
            asset=asset,
            direction=direction,
            reasoning=lux_raw[:500],
            hype_analysis=hype_analysis,
            oracle_analysis=oracle_analysis,
            vitalik_analysis=vitalik_analysis,
            lux_raw=lux_raw,
            market_snapshot=market_data,
            news_count=len(news),
            trade_status=trade_info["status"],
            order_id=trade_info["order_id"],
            created_at=datetime.utcnow().isoformat(),
        )
