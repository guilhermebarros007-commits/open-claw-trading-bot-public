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
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import httpx
import pandas as pd
import pandas_ta_classic as ta

logger = logging.getLogger(__name__)

HL_NETWORK = os.getenv("HL_NETWORK", "mainnet")

# ── Agent wallet config ───────────────────────────────────────────────────────

AGENT_WALLETS: dict[str, dict] = {
    "hype_beast": {
        "address": os.getenv("HL_HYPE_BEAST_ADDRESS", ""),
        "key": os.getenv("HL_HYPE_BEAST_KEY", ""),
        "primary_coin": "SOL",
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

WATCHED_COINS = ["BTC", "ETH", "SOL"]



def _base_url() -> str:
    from hyperliquid.utils import constants

    net = os.getenv("HL_NETWORK", "mainnet")
    return constants.MAINNET_API_URL if net == "mainnet" else constants.TESTNET_API_URL


def _get_info():
    from hyperliquid.info import Info
    return Info(_base_url(), skip_ws=True)


# ── HTTP helper (replaces SDK Info — works on both mainnet and testnet) ────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True
)
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
    meta, spot_meta = None, None
    try:
        net = os.getenv("HL_NETWORK", "mainnet")
        url = ("https://api.hyperliquid.xyz" if net == "mainnet" else "https://api.hyperliquid-testnet.xyz") + "/info"
        import httpx
        with httpx.Client(timeout=10) as client:
            try:
                meta = client.post(url, json={"type": "meta"}).json()
            except Exception as e:
                logger.warning(f"Failed to pre-fetch HL meta: {e}")
            
            try:
                spot_meta = client.post(url, json={"type": "spotMeta"}).json()
            except Exception as e:
                logger.warning(f"Failed to pre-fetch HL spotMeta: {e}")
    except Exception as e:
        logger.error(f"Error in metadata pre-fetch logic: {e}")

    # Monkeypatch de segurança
    original_init = Info.__init__
    def robust_init(self, base_url, skip_ws=False, meta=None, spot_meta=None, perp_dexs=None, timeout=None):
        try:
            original_init(self, base_url, skip_ws, meta, spot_meta, perp_dexs, timeout)
        except Exception as e:
            logger.warning(f"Info.__init__ failed (likely Testnet bug), applying fallback. Error: {e}")
            self.base_url = base_url
            self.skip_ws = skip_ws
            if meta and "universe" in meta:
                self.meta = meta
                self.name_to_coin = {coin["name"]: coin["name"] for coin in meta["universe"]}
                self.coin_to_asset = {coin["name"]: i for i, coin in enumerate(meta["universe"])}
                self.asset_to_sz_decimals = {i: coin["szDecimals"] for i, coin in enumerate(meta["universe"])}
            if spot_meta: 
                self.spot_meta = spot_meta
                # Also populate spot assets in name_to_coin if possible
                if "universe" in spot_meta:
                    for spot_info in spot_meta["universe"]:
                        self.name_to_coin[spot_info["name"]] = spot_info["name"]
    
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


# ── Technical indicators (pandas-ta) ──────────────────────────────────────────


def _compute_indicators(closes: list[float], highs: list[float], lows: list[float], volumes: list[float]) -> dict:
    """Compute full technical indicators using pandas-ta-classic."""
    if len(closes) < 21:
        return {"rsi_14": 50.0, "ema9": None, "ema21": None, "error": "insufficient candles"}

    df = pd.DataFrame({"close": closes, "high": highs, "low": lows, "volume": volumes})

    # RSI
    rsi_series = ta.rsi(df["close"], length=14)
    rsi_val = round(float(rsi_series.iloc[-1]), 2) if rsi_series is not None and not rsi_series.empty else 50.0

    # EMA 9 / 21
    ema9_series = ta.ema(df["close"], length=9)
    ema21_series = ta.ema(df["close"], length=21)
    ema9_val = round(float(ema9_series.iloc[-1]), 4) if ema9_series is not None and not ema9_series.empty else None
    ema21_val = round(float(ema21_series.iloc[-1]), 4) if ema21_series is not None and not ema21_series.empty else None

    # MACD (12, 26, 9)
    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    macd_val, macd_signal, macd_hist = None, None, None
    if macd_df is not None and not macd_df.empty:
        macd_val = round(float(macd_df.iloc[-1, 0]), 4)
        macd_signal = round(float(macd_df.iloc[-1, 1]), 4)
        macd_hist = round(float(macd_df.iloc[-1, 2]), 4)

    # Bollinger Bands (20, 2)
    bbands = ta.bbands(df["close"], length=20, std=2)
    bb_upper, bb_mid, bb_lower = None, None, None
    if bbands is not None and not bbands.empty:
        bb_lower = round(float(bbands.iloc[-1, 0]), 4)
        bb_mid = round(float(bbands.iloc[-1, 1]), 4)
        bb_upper = round(float(bbands.iloc[-1, 2]), 4)

    # ATR (14)
    atr_series = ta.atr(df["high"], df["low"], df["close"], length=14)
    atr_val = round(float(atr_series.iloc[-1]), 4) if atr_series is not None and not atr_series.empty else None

    # OBV
    obv_series = ta.obv(df["close"], df["volume"])
    obv_val = round(float(obv_series.iloc[-1]), 2) if obv_series is not None and not obv_series.empty else None

    trend = "BULL" if (ema9_val and ema21_val and ema9_val > ema21_val) else "BEAR"
    macd_trend = "BULL" if (macd_hist and macd_hist > 0) else "BEAR"

    return {
        "rsi_14": rsi_val,
        "ema9": ema9_val,
        "ema21": ema21_val,
        "trend_ema": trend,
        "macd": macd_val,
        "macd_signal": macd_signal,
        "macd_histogram": macd_hist,
        "macd_trend": macd_trend,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "atr_14": atr_val,
        "obv": obv_val,
        "last_close": closes[-1] if closes else 0,
        "volume_1h": volumes[-1] if volumes else 0,
    }


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """Backward-compatible RSI using pandas-ta."""
    if len(closes) < period + 1:
        return 50.0
    series = pd.Series(closes)
    rsi = ta.rsi(series, length=period)
    if rsi is not None and not rsi.empty:
        return round(float(rsi.iloc[-1]), 2)
    return 50.0


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

        data: dict = {
            "source": "hyperliquid",
            "fetched_at": datetime.utcnow().isoformat(),
            "watched_coins": WATCHED_COINS,
            "network": HL_NETWORK
        }

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
            "sol_price": 0,
        }


# ── Technical data (candles → RSI + EMA) ─────────────────────────────────────

# ── Candle data for frontend charts ───────────────────────────────────────────


async def get_candle_data(coin: str, interval: str = "1h", start_ms: int = None, end_ms: int = None) -> list[dict]:
    """Fetch raw candle data from Hyperliquid for frontend charts."""
    now_ms = int(time.time() * 1000)
    if not end_ms:
        end_ms = now_ms
    if not start_ms:
        start_ms = end_ms - (60 * 60 * 1000 * 60)  # 60 hours back

    try:
        candles = await _hl_post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })
        return [
            {"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
            for c in candles
        ]
    except Exception as e:
        logger.warning(f"HL candles fetch error for {coin}: {e}")
        return []


_tech_cache: dict = {"data": None, "expires": 0.0}
_TECH_TTL = 300  # 5 min


async def get_technical_data() -> dict:
    """RSI(14) + EMA9/EMA21 for BTC, ETH, SOL from 1h Hyperliquid candles."""
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
            highs = [float(c["h"]) for c in candles]
            lows = [float(c["l"]) for c in candles]
            volumes = [float(c["v"]) for c in candles]

            indicators = _compute_indicators(closes, highs, lows, volumes)
            indicators["candles"] = len(candles)
            return coin, indicators
        except Exception as e:
            logger.warning(f"HL candles error for {coin}: {e}")
            return coin, {"rsi_14": 50.0, "ema9": None, "ema21": None, "error": str(e)}

    results_list = await asyncio.gather(*[_fetch_coin(c) for c in WATCHED_COINS])
    results = dict(results_list)
    results["metadata"] = {"watched_coins": WATCHED_COINS}

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


async def transfer_to_perp(agent_id: str, amount: float) -> dict:
    """Transfer USDC from Spot wallet to Perp margin account."""
    try:
        exchange = get_exchange(agent_id)
        # Use to_thread as SDK sign/post is blocking
        result = await asyncio.to_thread(
            exchange.usd_class_transfer, 
            amount, 
            True # to_perp=True
        )
        logger.info(f"🔄 [{agent_id}] Transferred ${amount} from Spot to Perp → {result}")
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"❌ [{agent_id}] Transfer failure: {e}")
        return {"success": False, "error": str(e)}


async def ensure_perp_liquidity(agent_id: str, required_amount: float) -> bool:
    """Checks if Perp account has money; if not, tries to move from Spot."""
    state = await get_account_state(agent_id)
    if not state or "error" in state:
        return False
        
    perp_value = state.get("account_value", 0.0)
    if perp_value >= required_amount:
        return True # Already has enough
        
    # Not enough in Perps, check Spot
    spot_usdc = state.get("spot_usdc", 0.0)
    if spot_usdc > 5.0: # Minimum to transfer
        amount_to_move = min(spot_usdc, required_amount * 2) # move a bit extra for convenience
        await transfer_to_perp(agent_id, amount_to_move)
        return True
        
    return perp_value > 0 # Return True if any money exists


# ── Order execution ───────────────────────────────────────────────────────────


async def execute_market_open(
    agent_id: str,
    coin: str,
    is_buy: bool,
    size: float,
    slippage: float = 0.01,
) -> dict:
    """Open a market position."""
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    def _do_open():
        exchange = get_exchange(agent_id)
        return exchange.market_open(coin, is_buy, size, None, slippage)

    try:
        result = await asyncio.to_thread(_do_open)
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
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    def _do_close():
        exchange = get_exchange(agent_id)
        return exchange.market_close(coin, size, None, slippage)

    try:
        result = await asyncio.to_thread(_do_close)
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
        
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            reraise=True
        )
        def _do_trigger():
            exchange = get_exchange(agent_id)
            return exchange.order(
                coin,
                is_buy,
                size,
                trigger_px,
                trigger_params,
                True, # reduce_only MUST be true for automated SL/TP
            )

        result = await asyncio.to_thread(_do_trigger)
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
        macd_h = d.get("macd_histogram")
        macd_trend = d.get("macd_trend", "?")
        bb_upper = d.get("bb_upper")
        bb_lower = d.get("bb_lower")
        atr = d.get("atr_14")
        last_close = d.get("last_close", 0)

        # Confidence logic: multi-indicator scoring
        confidence = 5.0
        if trend == "BULL" and rsi < 70: confidence += 1.0
        if trend == "BEAR" and rsi > 30: confidence += 1.0
        if rsi > 80 or rsi < 20: confidence += 2.0  # Extreme RSI
        if macd_trend == trend: confidence += 1.0  # MACD confirms EMA trend
        if bb_upper and bb_lower and last_close:
            if last_close <= bb_lower: confidence += 1.0  # Near lower band (oversold)
            elif last_close >= bb_upper: confidence += 0.5  # Near upper band (overbought)

        flag = ""
        if rsi >= 70: flag = " ⚠ SOBRECOMPRA"
        elif rsi <= 30: flag = " ⚠ SOBREVENDA"

        # Bollinger position
        bb_str = ""
        if bb_upper and bb_lower and last_close:
            bb_pos = (last_close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
            bb_str = f" | BB%={bb_pos:.0%}"

        macd_str = f" | MACD={macd_trend}" if macd_trend else ""
        atr_str = f" | ATR={atr:.2f}" if atr else ""

        lines.append(
            f"{coin}: RSI(14)={rsi:.1f}{flag} | EMA9/21={trend}{macd_str}{bb_str}{atr_str} | Conf: {confidence:.1f}"
        )
    return "\n".join(lines)
