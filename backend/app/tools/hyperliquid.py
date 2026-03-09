"""
Hyperliquid DEX integration.
- Market data: prices, funding rates, open interest (public, no auth)
- Technical analysis: RSI(14), EMA9/21 from 1h candles
- Account state: positions, PnL, margin (requires wallet address)
- Order execution: market open/close, limit orders (requires private key)
"""

import asyncio
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Agent wallet config ───────────────────────────────────────────────────────

AGENT_WALLETS: dict[str, dict] = {
    "hype_beast": {
        "address": os.getenv("HL_HYPE_BEAST_ADDRESS", ""),
        "key": os.getenv("HL_HYPE_BEAST_KEY", ""),
        "primary_coin": "HYPE",
    },
    "oracle": {
        "address": os.getenv("HL_ORACLE_ADDRESS", ""),
        "key": os.getenv("HL_ORACLE_KEY", ""),
        "primary_coin": "BTC",
    },
    "vitalik": {
        "address": os.getenv("HL_VITALIK_ADDRESS", ""),
        "key": os.getenv("HL_VITALIK_KEY", ""),
        "primary_coin": "ETH",
    },
}

WATCHED_COINS = ["BTC", "ETH", "HYPE"]


def _base_url() -> str:
    from hyperliquid.utils import constants

    net = os.getenv("HL_NETWORK", "mainnet")
    return constants.MAINNET_API_URL if net == "mainnet" else constants.TESTNET_API_URL


def _get_info():
    from hyperliquid.info import Info
    return Info(_base_url(), skip_ws=True)


# ── HTTP helper (replaces SDK Info — works on both mainnet and testnet) ────────

async def _hl_post(payload: dict):
    """POST to Hyperliquid /info — direct HTTP, avoids SDK initialisation bugs."""
    import httpx

    url = _base_url() + "/info"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def get_exchange(agent_id: str):
    """Returns an authenticated Exchange instance for the given agent."""
    cfg = AGENT_WALLETS.get(agent_id, {})
    key = cfg.get("key", "")
    if not key:
        raise ValueError(f"No private key configured for agent '{agent_id}'")
    
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    # Pre-fetch meta para evitar bug de inicialização do SDK no Testnet
    import asyncio
    try:
        # Nota: Como get_exchange é síncrono e usado em to_thread em alguns lugares,
        # mas aqui precisamos de dados assíncronos, vamos usar um loop temporário ou rodar no loop atual.
        # Mas para simplificar, já que get_exchange é chamado em contextos síncronos,
        # vamos usar o loop de eventos se ele já existe.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Estamos num thread ou o loop está rodando, usamos run_coroutine_threadsafe ou similar?
                # Na verdade, a maioria dos nossos calls são await execute_market_open -> to_thread(market_open) -> get_exchange.
                # Então estamos em um worker thread.
                import httpx
                net = os.getenv("HL_NETWORK", "mainnet")
                url = ("https://api.hyperliquid.xyz" if net == "mainnet" else "https://api.hyperliquid-testnet.xyz") + "/info"
                with httpx.Client(timeout=10) as client:
                    meta = client.post(url, json={"type": "meta"}).json()
                    spot_meta = client.post(url, json={"type": "spotMeta"}).json()
            else:
                meta = loop.run_until_complete(get_meta())
                spot_meta = None # Spot meta opcional para perps
        except Exception:
            meta, spot_meta = None, None
    except Exception:
        meta, spot_meta = None, None

    # Monkeypatch de segurança
    original_init = Info.__init__
    def robust_init(self, base_url, skip_ws=False, meta=None, spot_meta=None, perp_dexs=None, timeout=None):
        try:
            original_init(self, base_url, skip_ws, meta, spot_meta, perp_dexs, timeout)
        except IndexError:
            self.base_url = base_url
            self.skip_ws = skip_ws
            if meta and "universe" in meta:
                self.meta = meta
                self.name_to_coin = {coin["name"]: coin["name"] for coin in meta["universe"]}
                self.coin_to_asset = {coin["name"]: i for i, coin in enumerate(meta["universe"])}
            if spot_meta: self.spot_meta = spot_meta

    Info.__init__ = robust_init
    wallet = Account.from_key(key)
    # Passamos meta e spot_meta explicitamente
    return Exchange(wallet, _base_url(), meta=meta, spot_meta=spot_meta)


# ── Metadata Helpers ──────────────────────────────────────────────────────────

async def get_meta() -> dict:
    """Fetch exchange metadata (universe, szDecimals, etc.)"""
    return await _hl_post({"type": "meta"})


async def get_sz_decimals(coin: str) -> int:
    """Get the number of decimals allowed for the size of a specific coin."""
    meta = await get_meta()
    for asset in meta.get("universe", []):
        if asset["name"] == coin:
            return asset["szDecimals"]
    return 0


def round_to_hl_standard(px: float, sz: float, sz_decimals: int) -> tuple[str, str]:
    """
    Format price and size to Hyperliquid standards:
    - Price: 5 significant figures or 6 decimals (whichever is more restrictive).
    - Size: Based on szDecimals, no trailing zeros.
    Returns (px_str, sz_str) as strings.
    """
    # Price rounding: max 5 sig figs, max 6 decimals
    px_str = f"{px:.6g}"
    if "." in px_str:
        parts = px_str.split(".")
        if len(parts[1]) > 6:
            px_str = f"{px:.6f}"
    
    # Size rounding: fixed decimals based on szDecimals
    sz_str = f"{round(sz, sz_decimals):.{sz_decimals}f}".rstrip("0").rstrip(".")
    if not sz_str: sz_str = "0"
    
    return px_str, sz_str


# ── Technical indicators ──────────────────────────────────────────────────────


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


# ── Market data ───────────────────────────────────────────────────────────────

_hl_cache: dict = {"data": None, "expires": 0.0}
_MARKET_TTL = 60  # seconds


async def get_hl_market_data() -> dict:
    """Real-time prices + funding rates + open interest from Hyperliquid."""
    global _hl_cache
    now = time.time()
    if _hl_cache["data"] and now < _hl_cache["expires"]:
        return _hl_cache["data"]

    try:
        all_mids_raw, meta_ctxs = await asyncio.gather(
            _hl_post({"type": "allMids"}),
            _hl_post({"type": "metaAndAssetCtxs"}),
        )
        meta, ctxs = meta_ctxs
        coin_idx = {m["name"]: i for i, m in enumerate(meta["universe"])}

        def _mid(coin: str) -> float:
            try:
                return float(all_mids_raw.get(coin, 0))
            except Exception:
                return 0.0

        def _ctx(coin: str) -> dict:
            idx = coin_idx.get(coin)
            return ctxs[idx] if idx is not None and idx < len(ctxs) else {}

        data: dict = {"source": "hyperliquid", "fetched_at": datetime.utcnow().isoformat()}
        for coin in WATCHED_COINS:
            ctx = _ctx(coin)
            data[f"{coin.lower()}_price"] = _mid(coin)
            data[f"{coin.lower()}_mark_price"] = float(ctx.get("markPx", 0) or 0)
            data[f"{coin.lower()}_funding"] = float(ctx.get("funding", 0) or 0)
            data[f"{coin.lower()}_oi"] = float(ctx.get("openInterest", 0) or 0)
            data[f"{coin.lower()}_volume_24h"] = float(ctx.get("dayNtlVlm", 0) or 0)

        _hl_cache["data"] = data
        _hl_cache["expires"] = now + _MARKET_TTL
        logger.info("HL market data refreshed")
        return data

    except Exception as e:
        logger.warning(f"HL market data error: {e}")
        return _hl_cache["data"] or {
            "source": "hyperliquid",
            "error": str(e),
            "btc_price": 0,
            "eth_price": 0,
            "hype_price": 0,
        }


# ── Technical data (candles → RSI + EMA) ─────────────────────────────────────

_tech_cache: dict = {"data": None, "expires": 0.0}
_TECH_TTL = 300  # 5 min


async def get_technical_data() -> dict:
    """RSI(14) + EMA9/EMA21 for BTC, ETH, HYPE from 1h Hyperliquid candles."""
    global _tech_cache
    now = time.time()
    if _tech_cache["data"] and now < _tech_cache["expires"]:
        return _tech_cache["data"]

    end_ms = int(now * 1000)
    start_ms = end_ms - (60 * 60 * 1000 * 60)  # 60 hours back → 60 candles

    async def _fetch_coin(coin: str) -> tuple[str, dict]:
        try:
            candles = await _hl_post({
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms},
            })
            closes = [float(c["c"]) for c in candles]
            volumes = [float(c["v"]) for c in candles]
            rsi = calculate_rsi(closes)
            ema9 = _ema(closes, 9)
            ema21 = _ema(closes, 21)
            trend = "BULL" if (ema9 and ema21 and ema9 > ema21) else "BEAR"
            return coin, {
                "rsi_14": rsi,
                "ema9": round(ema9, 4) if ema9 else None,
                "ema21": round(ema21, 4) if ema21 else None,
                "trend_ema": trend,
                "last_close": closes[-1] if closes else 0,
                "volume_1h": volumes[-1] if volumes else 0,
                "candles": len(candles),
            }
        except Exception as e:
            logger.warning(f"HL candles error for {coin}: {e}")
            return coin, {"rsi_14": 50.0, "ema9": None, "ema21": None, "error": str(e)}

    results_list = await asyncio.gather(*[_fetch_coin(c) for c in WATCHED_COINS])
    results = dict(results_list)

    _tech_cache["data"] = results
    _tech_cache["expires"] = now + _TECH_TTL
    logger.info("HL technical data refreshed")
    return results


# ── Account state ─────────────────────────────────────────────────────────────


async def get_account_state(agent_id: str) -> dict:
    """
    Full account state for an agent wallet:
    - Perp account: margin, positions, PnL (crossMarginSummary)
    - Spot wallet: USDC and any token balances (spot_user_state)
    - Total value = perp account value + spot USDC
    """
    cfg = AGENT_WALLETS.get(agent_id, {})
    address = cfg.get("address", "")
    if not address:
        return {"agent_id": agent_id, "error": "No address configured"}

    try:
        user_state, spot_state, open_orders = await asyncio.gather(
            _hl_post({"type": "clearinghouseState", "user": address}),
            _hl_post({"type": "spotClearinghouseState", "user": address}),
            _hl_post({"type": "openOrders", "user": address}),
        )

        # ── Perp positions ─────────────────────────────────────────────────────
        positions = []
        for pos_entry in user_state.get("assetPositions", []):
            p = pos_entry.get("position", {})
            size = float(p.get("szi", 0))
            if size == 0:
                continue
            positions.append(
                {
                    "coin": p.get("coin", "?"),
                    "size": size,
                    "entry_price": float(p.get("entryPx", 0) or 0),
                    "mark_price": float(p.get("positionValue", 0) or 0) / abs(size) if size else 0,
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                    "return_on_equity": float(p.get("returnOnEquity", 0) or 0),
                    "liquidation_px": float(p.get("liquidationPx", 0) or 0),
                    "leverage": p.get("leverage", {}).get("value", 1),
                    "side": "long" if size > 0 else "short",
                }
            )

        # ── Spot balances ──────────────────────────────────────────────────────
        spot_balances = []
        spot_usdc = 0.0
        for bal in spot_state.get("balances", []):
            coin = bal.get("coin", "")
            total = float(bal.get("total", 0) or 0)
            hold = float(bal.get("hold", 0) or 0)
            if total > 0:
                spot_balances.append({"coin": coin, "total": total, "hold": hold, "available": total - hold})
            if coin == "USDC":
                spot_usdc = total

        # ── Summaries ──────────────────────────────────────────────────────────
        margin = user_state.get("crossMarginSummary", {})
        perp_value = float(margin.get("accountValue", 0) or 0)
        total_pnl = sum(p["unrealized_pnl"] for p in positions)
        total_value = perp_value + spot_usdc  # combined perp + spot USDC

        return {
            "agent_id": agent_id,
            "address": address,
            "primary_coin": cfg.get("primary_coin", "?"),
            # Perp account
            "account_value": perp_value,
            "total_margin_used": float(margin.get("totalMarginUsed", 0) or 0),
            "total_pnl": round(total_pnl, 4),
            "withdrawable": float(user_state.get("withdrawable", 0) or 0),
            # Spot wallet
            "spot_usdc": round(spot_usdc, 4),
            "spot_balances": spot_balances,
            # Combined
            "total_value": round(total_value, 4),
            # Positions & orders
            "positions": positions,
            "open_orders": open_orders,
            "open_orders_count": len(open_orders),
        }
    except Exception as e:
        logger.error(f"HL account state error for {agent_id}: {e}")
        return {"agent_id": agent_id, "address": address, "error": str(e)}


async def get_all_accounts() -> list[dict]:
    """Fetch account state for all configured agents in parallel."""
    results = await asyncio.gather(
        *[get_account_state(aid) for aid in AGENT_WALLETS],
        return_exceptions=False,
    )
    return list(results)


async def get_available_usdc(agent_id: str) -> float:
    """Directly fetch available USDC balance for an agent."""
    state = await get_account_state(agent_id)
    if "error" in state:
        return 0.0
    return state.get("spot_usdc", 0.0) + state.get("withdrawable", 0.0)


# ── Order execution ───────────────────────────────────────────────────────────


async def execute_market_open(
    agent_id: str,
    coin: str,
    is_buy: bool,
    size: float,
    slippage: float = 0.01,
) -> dict:
    """Open a market position."""
    try:
        exchange = get_exchange(agent_id)
        result = await asyncio.to_thread(
            exchange.market_open, coin, is_buy, size, None, slippage
        )
        logger.info(f"[{agent_id}] market_open {coin} {'BUY' if is_buy else 'SELL'} {size} → {result}")
        return {"success": True, "agent_id": agent_id, "coin": coin, "result": result}
    except Exception as e:
        import traceback
        error_msg = f"{e}\n{traceback.format_exc()}"
        logger.error(f"[{agent_id}] market_open error: {error_msg}")
        return {"success": False, "agent_id": agent_id, "error": str(e), "traceback": error_msg}


async def execute_market_close(
    agent_id: str,
    coin: str,
    size: float | None = None,
    slippage: float = 0.01,
) -> dict:
    """Close a market position (fully or partially)."""
    try:
        exchange = get_exchange(agent_id)
        result = await asyncio.to_thread(
            exchange.market_close, coin, size, None, slippage
        )
        logger.info(f"[{agent_id}] market_close {coin} size={size} → {result}")
        return {"success": True, "agent_id": agent_id, "coin": coin, "result": result}
    except Exception as e:
        logger.error(f"[{agent_id}] market_close error: {e}")
        return {"success": False, "agent_id": agent_id, "error": str(e)}


async def execute_limit_order(
    agent_id: str,
    coin: str,
    is_buy: bool,
    size: float,
    price: float,
    reduce_only: bool = False,
) -> dict:
    """Place a GTC limit order."""
    try:
        exchange = get_exchange(agent_id)
        result = await asyncio.to_thread(
            exchange.order,
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": "Gtc"}},
            reduce_only,
        )
        logger.info(f"[{agent_id}] limit_order {coin} {'BUY' if is_buy else 'SELL'} {size}@{price}")
        return {"success": True, "agent_id": agent_id, "coin": coin, "result": result}
    except Exception as e:
        logger.error(f"[{agent_id}] limit_order error: {e}")
        return {"success": False, "agent_id": agent_id, "error": str(e)}


async def execute_trigger_order(
    agent_id: str,
    coin: str,
    is_buy: bool,
    size: float,
    trigger_px: float,
    order_type: str = "sl",  # 'sl' or 'tp'
) -> dict:
    """
    Place a native exchange-side trigger order (Stop Loss or Take Profit).
    This runs server-side on Hyperliquid.
    """
    try:
        exchange = get_exchange(agent_id)
        
        # Hyperliquid uses 'tp' or 'sl' for trigger orders.
        # We use limit orders that trigger when price hits a threshold.
        # For simplicity, we use market triggers (tpMarket or slMarket) to ensure closure.
        is_tp = (order_type.lower() == "tp")
        
        # Building trigger parameters for Exchange SDK
        # Official SDK requirement: specify isMarket to ensure immediate fill on trigger
        trigger_params = {
            "trigger": {
                "triggerPx": trigger_px,
                "isMarket": True,
                "tpsl": "tp" if is_tp else "sl"
            }
        }
        
        result = await asyncio.to_thread(
            exchange.order,
            coin,
            is_buy,
            size,
            trigger_px,
            trigger_params,
            True, # reduce_only MUST be true for automated SL/TP
        )
        logger.info(f"🛡️ [{agent_id}] Native {order_type.upper()} set for {coin} @ {trigger_px}")
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"[{agent_id}] trigger_order error ({order_type}): {e}")
        return {"success": False, "error": str(e)}


async def get_active_trigger_id(agent_id: str, coin: str, order_type: str = "sl") -> str | None:
    """Find the OID of an active trigger order of a certain type (sl/tp)."""
    try:
        state = await get_account_state(agent_id)
        orders = state.get("open_orders", [])
        for o in orders:
            # Trigger orders have 'isTrigger': True
            if o.get("coin") == coin and o.get("isTrigger"):
                # Identifying SL/TP based on price logic or label if available
                # Fallback: if it's a reduceOnly trigger, it's likely our SL/TP
                return o.get("oid")
    except Exception:
        pass
    return None


async def update_sl_trigger(
    agent_id: str,
    coin: str,
    new_trigger_px: float,
    size: float,
    is_buy: bool,
) -> dict:
    """Cancels old SL and places a new one at a better price."""
    try:
        # 1. Try to find and cancel old SL
        old_oid = await get_active_trigger_id(agent_id, coin, "sl")
        if old_oid:
             exchange = get_exchange(agent_id)
             await asyncio.to_thread(exchange.cancel, coin, old_oid)
             logger.info(f"🔄 [{agent_id}] Cancelled old SL trigger {old_oid}")

        # 2. Place NEW SL
        return await execute_trigger_order(
            agent_id=agent_id,
            coin=coin,
            is_buy=is_buy,
            size=size,
            trigger_px=new_trigger_px,
            order_type="sl"
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


async def cancel_all_orders(agent_id: str, coin: str) -> dict:
    """Cancel all open orders for a coin."""
    try:
        cfg = AGENT_WALLETS.get(agent_id, {})
        address = cfg.get("address", "")
        info = _get_info()
        exchange = get_exchange(agent_id)

        open_orders = await asyncio.to_thread(info.open_orders, address)
        coin_orders = [o for o in open_orders if o.get("coin") == coin]

        results = []
        for o in coin_orders:
            r = await asyncio.to_thread(exchange.cancel, coin, o["oid"])
            results.append(r)

        return {"success": True, "cancelled": len(results), "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Formatting helpers ────────────────────────────────────────────────────────


def format_hl_market_summary(data: dict) -> str:
    lines = []
    for coin in WATCHED_COINS:
        price = data.get(f"{coin.lower()}_price", 0)
        funding = data.get(f"{coin.lower()}_funding", 0)
        oi = data.get(f"{coin.lower()}_oi", 0)
        funding_pct = funding * 100
        funding_sign = "+" if funding >= 0 else ""
        oi_str = f"${oi/1e6:.1f}M OI" if oi > 0 else ""
        lines.append(
            f"{coin}: ${price:,.2f} | Funding: {funding_sign}{funding_pct:.4f}%/h {oi_str}"
        )
    return "\n".join(lines)


def format_technical_summary(tech: dict) -> str:
    lines = []
    for coin in WATCHED_COINS:
        d = tech.get(coin, {})
        if "error" in d:
            lines.append(f"{coin}: RSI=N/A (falha na coleta)")
            continue
        rsi = d.get("rsi_14", 50)
        trend = d.get("trend_ema", "?")
        flag = ""
        if rsi >= 70:
            flag = " ⚠ SOBRECOMPRA"
        elif rsi <= 30:
            flag = " ⚠ SOBREVENDA"
        lines.append(f"{coin}: RSI(14)={rsi:.1f}{flag} | EMA9/21={trend}")
    return "\n".join(lines)
