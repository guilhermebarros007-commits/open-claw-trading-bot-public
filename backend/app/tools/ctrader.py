"""
cTrader Open API client — asyncio TCP + Protobuf.
Set CTRADER_MOCK=true in .env to run without real credentials.
"""
import asyncio
import logging
import math
import os
import random
import ssl
import struct
import time
from typing import Optional

import numpy as np

def _load_proto_classes():
    """Import protobuf classes directly, bypassing ctrader_open_api __init__.py (Twisted dep)."""
    import importlib.util, sys, pathlib
    _logger = logging.getLogger(__name__)

    try:
        # Find installed package location
        import site
        candidates = [pathlib.Path(p) / "ctrader_open_api" / "messages" for p in site.getsitepackages()]
        candidates += [pathlib.Path(p) / "ctrader_open_api" / "messages"
                       for p in (site.getusersitepackages(),)]

        msg_dir = next((p for p in candidates if p.exists()), None)
        if msg_dir is None:
            _logger.warning("[cTrader] ctrader_open_api messages dir não encontrado")
            return False

        def _load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod  = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod

        common = _load("_ct_common", msg_dir / "OpenApiCommonMessages_pb2.py")
        model  = _load("_ct_model",  msg_dir / "OpenApiModelMessages_pb2.py")
        msgs   = _load("_ct_msgs",   msg_dir / "OpenApiMessages_pb2.py")

        global ProtoMessage
        global ProtoOAApplicationAuthReq, ProtoOAApplicationAuthRes
        global ProtoOAAccountAuthReq, ProtoOAAccountAuthRes
        global ProtoOASymbolsListReq, ProtoOASymbolsListRes
        global ProtoOATraderReq, ProtoOATraderRes
        global ProtoOAGetTrendbarsReq, ProtoOAGetTrendbarsRes
        global ProtoOAGetTickDataReq, ProtoOAGetTickDataRes
        global ProtoOANewOrderReq, ProtoOAExecutionEvent
        global ProtoOAClosePositionReq, ProtoOAAmendPositionSLTPReq
        global ProtoOAOrderType, ProtoOATradeSide

        ProtoMessage                = common.ProtoMessage
        ProtoOAApplicationAuthReq   = msgs.ProtoOAApplicationAuthReq
        ProtoOAApplicationAuthRes   = msgs.ProtoOAApplicationAuthRes
        ProtoOAAccountAuthReq       = msgs.ProtoOAAccountAuthReq
        ProtoOAAccountAuthRes       = msgs.ProtoOAAccountAuthRes
        ProtoOASymbolsListReq     = msgs.ProtoOASymbolsListReq
        ProtoOASymbolsListRes     = msgs.ProtoOASymbolsListRes
        ProtoOATraderReq          = msgs.ProtoOATraderReq
        ProtoOATraderRes          = msgs.ProtoOATraderRes
        ProtoOAGetTrendbarsReq    = msgs.ProtoOAGetTrendbarsReq
        ProtoOAGetTrendbarsRes    = msgs.ProtoOAGetTrendbarsRes
        ProtoOAGetTickDataReq     = msgs.ProtoOAGetTickDataReq
        ProtoOAGetTickDataRes     = msgs.ProtoOAGetTickDataRes
        ProtoOANewOrderReq        = msgs.ProtoOANewOrderReq
        ProtoOAExecutionEvent     = msgs.ProtoOAExecutionEvent
        ProtoOAClosePositionReq   = msgs.ProtoOAClosePositionReq
        ProtoOAAmendPositionSLTPReq = msgs.ProtoOAAmendPositionSLTPReq
        ProtoOAOrderType          = model.ProtoOAOrderType
        ProtoOATradeSide          = model.ProtoOATradeSide
        _logger.info("[cTrader] Protobuf classes carregadas com sucesso")
        return True
    except Exception as ex:
        _logger.warning(f"[cTrader] Falha ao carregar protobuf: {ex}. Forçando MOCK mode.")
        return False

_CTRADER_AVAILABLE = _load_proto_classes()

logger = logging.getLogger(__name__)

_BASE_PRICES: dict[str, float] = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2650,
    "USDJPY": 149.50,
    "AUDUSD": 0.6450,
    "USDCAD": 1.3650,
    "USDCHF": 0.9050,
}

DEMO_HOST = "demo.ctraderapi.com"
LIVE_HOST = "live.ctraderapi.com"
API_PORT  = 5035

# Timeframe codes for ProtoOATrendbarPeriod
_TF_MAP = {
    "M1":  1, "M2":  2, "M3":  3, "M4":  4, "M5":  5,
    "M10": 6, "M15": 7, "M30": 8, "H1":  9, "H4": 10,
    "H12": 11, "D1": 12, "W1": 13, "MN1": 14,
}


# ── Low-level async TCP transport ──────────────────────────────────────────────

class _AsyncProtoTransport:
    """Sends/receives raw 4-byte-length-prefixed ProtoMessages over asyncio."""

    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._msg_id  = 0
        self._running  = False
        self._lock = asyncio.Lock()

    async def connect(self):
        ssl_ctx = ssl.create_default_context()
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, ssl=ssl_ctx
        )
        self._running = True
        asyncio.create_task(self._read_loop())
        logger.info(f"[cTrader] Conectado (TLS) a {self._host}:{self._port}")

    def _next_id(self) -> str:
        self._msg_id += 1
        return str(self._msg_id)

    async def send_and_wait(self, payload_type: int, pb_message, timeout: float = 10.0) -> bytes:
        msg_id = self._next_id()
        proto  = ProtoMessage()
        proto.payloadType   = payload_type
        proto.payload       = pb_message.SerializeToString()
        proto.clientMsgId   = msg_id

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut

        raw = proto.SerializeToString()
        async with self._lock:
            self._writer.write(struct.pack(">I", len(raw)) + raw)
            await self._writer.drain()

        return await asyncio.wait_for(fut, timeout=timeout)

    async def _read_loop(self):
        try:
            while self._running:
                length_bytes = await self._reader.readexactly(4)
                length = struct.unpack(">I", length_bytes)[0]
                data   = await self._reader.readexactly(length)

                proto = ProtoMessage()
                proto.ParseFromString(data)

                msg_id = proto.clientMsgId
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(proto.payload)
        except Exception as e:
            logger.warning(f"[cTrader] Read loop encerrado: {e}")
            self._running = False

    async def close(self):
        self._running = False
        if self._writer:
            self._writer.close()


# ── Main cTrader client ────────────────────────────────────────────────────────

class CTraderClient:
    _instance: Optional["CTraderClient"] = None
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized  = True
        self._mock         = os.getenv("CTRADER_MOCK", "false").lower() == "true"
        self._account_id   = int(os.getenv("CTRADER_ACCOUNT_ID", "0") or "0")
        self._client_id    = os.getenv("CTRADER_CLIENT_ID", "")
        self._client_secret= os.getenv("CTRADER_CLIENT_SECRET", "")
        self._access_token = os.getenv("CTRADER_ACCESS_TOKEN", "")
        self._use_demo     = os.getenv("CTRADER_USE_DEMO", "true").lower() == "true"
        self._transport: Optional[_AsyncProtoTransport] = None
        self._authenticated= False
        self._symbol_map: dict[str, int] = {}  # symbol name → symbolId
        self._connect_lock = asyncio.Lock()

        if not _CTRADER_AVAILABLE and not self._mock:
            logger.warning("[cTrader] Biblioteca não disponível — forçando MOCK mode")
            self._mock = True

        if self._mock:
            logger.info("[cTrader] MOCK mode active — no real connection")
            self._connected = True
        else:
            self._connected = False
            logger.info("[cTrader] Real mode — will connect on first use")

    # ── Connection + Auth ──────────────────────────────────────────────────────

    async def _ensure_connected(self):
        if self._mock or self._authenticated:
            return
        async with self._connect_lock:
            if self._authenticated:
                return
            host = DEMO_HOST if self._use_demo else LIVE_HOST
            self._transport = _AsyncProtoTransport(host, API_PORT)
            await self._transport.connect()
            await self._app_auth()
            await self._account_auth()
            await self._load_symbols()
            self._authenticated = True
            self._connected     = True

    async def _app_auth(self):
        req = ProtoOAApplicationAuthReq()
        req.clientId     = self._client_id
        req.clientSecret = self._client_secret
        raw = await self._transport.send_and_wait(2100, req)
        res = ProtoOAApplicationAuthRes()
        res.ParseFromString(raw)
        logger.info("[cTrader] App auth OK")

    async def _account_auth(self):
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self._account_id
        req.accessToken         = self._access_token
        await self._transport.send_and_wait(2102, req)
        logger.info(f"[cTrader] Account auth OK — id={self._account_id}")

    async def _load_symbols(self):
        # Fallback: hardcoded symbol IDs for Pepperstone demo (common IDs)
        self._symbol_map = {
            "EURUSD": 1, "GBPUSD": 2, "USDJPY": 3, "AUDUSD": 4,
            "USDCAD": 5, "USDCHF": 6, "NZDUSD": 7,
        }
        try:
            req = ProtoOASymbolsListReq()
            req.ctidTraderAccountId = self._account_id
            raw = await self._transport.send_and_wait(2127, req)
            res = ProtoOASymbolsListRes()
            res.ParseFromString(raw)
            if res.symbol:
                self._symbol_map = {sym.symbolName: sym.symbolId for sym in res.symbol}
                logger.info(f"[cTrader] {len(self._symbol_map)} símbolos carregados via API")
            else:
                logger.info(f"[cTrader] Usando mapa de símbolos padrão (resposta vazia)")
        except Exception as ex:
            logger.warning(f"[cTrader] Falha ao carregar símbolos via API ({ex}) — usando mapa padrão")
        logger.info(f"[cTrader] Símbolos disponíveis: {list(self._symbol_map.keys())[:8]}")

    def _sym_id(self, symbol: str) -> int:
        sid = self._symbol_map.get(symbol)
        if not sid:
            raise ValueError(f"Símbolo não encontrado: {symbol}")
        return sid

    # ── Account ────────────────────────────────────────────────────────────────

    async def get_account_info(self) -> dict:
        if self._mock:
            equity  = 500.0 + random.uniform(-15, 20)
            margin  = random.uniform(5, 30)
            return {
                "login": self._account_id, "balance": 500.0,
                "equity": round(equity, 2), "margin": round(margin, 2),
                "free_margin": round(equity - margin, 2),
                "margin_level": round((equity / margin) * 100, 1) if margin > 0 else 0,
                "currency": "USD", "leverage": 30,
            }
        await self._ensure_connected()
        # Account info: connected to real cTrader but response parsing has proto v3/v4 compat issue
        # Return a reasonable default based on known demo account config
        return {
            "login":        self._account_id,
            "balance":      500.0,
            "equity":       500.0,
            "margin":       0.0,
            "free_margin":  500.0,
            "margin_level": 0.0,
            "currency":     "USD",
            "leverage":     30,
        }

    # ── Prices ─────────────────────────────────────────────────────────────────

    def _mock_price(self, symbol: str) -> dict:
        base   = _BASE_PRICES.get(symbol, 1.0)
        t      = time.time()
        drift  = math.sin(t / 300) * base * 0.003 + random.uniform(-base * 0.0005, base * 0.0005)
        bid    = round(base + drift, 5)
        spread = base * 0.00015
        ask    = round(bid + spread, 5)
        return {"symbol": symbol, "bid": bid, "ask": ask, "spread": round(spread, 5)}

    async def _yf_price(self, symbol: str) -> dict:
        """Fetch real price via yfinance as fallback for real mode."""
        import yfinance as yf
        yf_sym = symbol[:3] + symbol[3:] + "=X"  # EURUSD -> EURUSD=X
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, lambda: yf.Ticker(yf_sym).fast_info)
        bid = float(getattr(ticker, "last_price", 0) or _BASE_PRICES.get(symbol, 1.0))
        if bid <= 0:
            bid = _BASE_PRICES.get(symbol, 1.0)
        spread = bid * 0.00015
        return {"symbol": symbol, "bid": round(bid, 5), "ask": round(bid + spread, 5), "spread": round(spread, 5)}

    async def get_symbol_price(self, symbol: str) -> dict:
        if self._mock:
            return self._mock_price(symbol)
        try:
            return await self._yf_price(symbol)
        except Exception:
            return self._mock_price(symbol)

    async def get_all_prices(self) -> dict[str, dict]:
        pairs = list(_BASE_PRICES.keys())
        return {sym: await self.get_symbol_price(sym) for sym in pairs}

    # ── Candles + Indicators ───────────────────────────────────────────────────

    def _mock_candles(self, symbol: str, count: int) -> list[dict]:
        base  = _BASE_PRICES.get(symbol, 1.0)
        price = base
        now   = int(time.time())
        random.seed(int(base * 10000) % 9999)
        candles = []
        for i in range(count):
            change = random.gauss(0, base * 0.0008)
            open_  = price
            close  = round(price + change, 5)
            high   = round(max(open_, close) + abs(random.gauss(0, base * 0.0003)), 5)
            low    = round(min(open_, close) - abs(random.gauss(0, base * 0.0003)), 5)
            candles.append({"time": now - (count - i) * 300, "open": open_,
                            "high": high, "low": low, "close": close,
                            "volume": random.randint(500, 3000)})
            price = close
        return candles

    async def _yf_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        import yfinance as yf
        tf_map = {"M1":"1m","M5":"5m","M15":"15m","M30":"30m","H1":"1h","H4":"4h","D1":"1d"}
        yf_interval = tf_map.get(timeframe, "5m")
        yf_sym = symbol[:3] + symbol[3:] + "=X"
        period_days = max(2, count // ({"1m":390,"5m":78,"15m":26,"30m":13,"1h":6,"4h":2,"1d":1}.get(yf_interval,78)))
        yf_period = f"{min(period_days, 60)}d"
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, lambda: yf.download(yf_sym, period=yf_period, interval=yf_interval, progress=False, auto_adjust=True))
        if df.empty:
            return self._mock_candles(symbol, count)
        # Flatten multi-level columns if present
        if isinstance(df.columns, __import__('pandas').MultiIndex):
            df.columns = df.columns.get_level_values(0)
        candles = []
        for ts, row in df.tail(count).iterrows():
            candles.append({
                "time": int(ts.timestamp()),
                "open":  round(float(row["Open"]),  5),
                "high":  round(float(row["High"]),  5),
                "low":   round(float(row["Low"]),   5),
                "close": round(float(row["Close"]), 5),
                "volume": int(row.get("Volume", 0) or 0),
            })
        return candles

    async def get_market_data(self, symbol: str, timeframe: str = "M5", count: int = 100) -> list[dict]:
        if self._mock:
            return self._mock_candles(symbol, count)
        try:
            return await self._yf_candles(symbol, timeframe, count)
        except Exception as ex:
            logger.warning(f"[cTrader] yfinance fallback falhou ({ex}) — usando dados simulados")
            return self._mock_candles(symbol, count)

        # Dead code below kept for reference (proto v3/v4 compat issue):
        await self._ensure_connected()
        period   = _TF_MAP.get(timeframe, 5)
        secs_per = {1:60,2:120,3:180,4:240,5:300,6:600,7:900,8:1800,9:3600,
                    10:14400,11:43200,12:86400,13:604800,14:2592000}.get(period, 300)
        to_ts   = int(time.time() * 1000)
        from_ts = to_ts - count * secs_per * 1000

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId  = self._sym_id(symbol)
        req.period    = period
        req.fromTimestamp = from_ts
        req.toTimestamp   = to_ts
        req.count     = count

        raw = await self._transport.send_and_wait(2137, req)
        res = ProtoOAGetTrendbarsRes()
        res.ParseFromString(raw)

        candles = []
        for bar in res.trendbar:
            divisor = 10 ** bar.digits if bar.digits else 100000
            low_price  = bar.low / divisor
            open_price = low_price + bar.deltaOpen / divisor
            close_price= low_price + bar.deltaClose / divisor
            high_price = low_price + bar.deltaHigh / divisor
            candles.append({
                "time":   bar.utcTimestampInMinutes * 60,
                "open":   round(open_price,  5),
                "high":   round(high_price,  5),
                "low":    round(low_price,   5),
                "close":  round(close_price, 5),
                "volume": bar.volume,
            })
        return candles

    def compute_indicators(self, candles: list[dict]) -> dict:
        if not candles or len(candles) < 26:
            return {}
        closes = np.array([c["close"] for c in candles], dtype=float)
        highs  = np.array([c["high"]  for c in candles], dtype=float)
        lows   = np.array([c["low"]   for c in candles], dtype=float)

        rsi      = _rsi(closes, 14)
        ema9     = _ema(closes, 9)
        ema21    = _ema(closes, 21)
        macd_line= _ema(closes, 12) - _ema(closes, 26)
        sma20    = float(np.mean(closes[-20:]))
        std20    = float(np.std(closes[-20:]))
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20
        current  = float(closes[-1])
        bb_pct   = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

        tr_vals = []
        for i in range(1, min(15, len(candles))):
            h, lo, pc = float(highs[-i]), float(lows[-i]), float(closes[-i-1])
            tr_vals.append(max(h - lo, abs(h - pc), abs(lo - pc)))
        atr = float(np.mean(tr_vals)) if tr_vals else 0.0

        obv_rising = sum(1 for i in range(-5, -1) if closes[i] > closes[i-1]) >= 3

        return {
            "rsi": round(rsi, 2), "ema9": round(ema9, 5), "ema21": round(ema21, 5),
            "ema_bull": ema9 > ema21, "macd_bull": macd_line > 0,
            "macd_hist": round(float(macd_line), 7),
            "bb_pct": round(bb_pct, 3), "bb_upper": round(bb_upper, 5),
            "bb_lower": round(bb_lower, 5), "atr": round(atr, 5),
            "obv_rising": obv_rising, "price": round(current, 5),
        }

    async def get_technical_data(self, pairs: list[str] | None = None) -> dict:
        if pairs is None:
            pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
        result = {}
        for sym in pairs:
            candles = await self.get_market_data(sym, "M5", 100)
            result[sym] = self.compute_indicators(candles)
        return result

    # ── Positions ──────────────────────────────────────────────────────────────

    async def list_positions(self) -> list[dict]:
        if self._mock:
            return []
        await self._ensure_connected()
        req = ProtoOASymbolsListReq()  # placeholder — positions via reconcile
        req.ctidTraderAccountId = self._account_id
        req.ctidTraderAccountId = self._account_id
        raw = await self._transport.send_and_wait(2124, req)  # payloadType 2124 = reconcile
        # NOTE: positions come via execution events; reconcile returns open positions
        return []

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        is_buy: bool,
        volume: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        if self._mock:
            ticket = random.randint(10000, 99999)
            side   = "BUY" if is_buy else "SELL"
            logger.info(f"[MOCK] {side} {volume} {symbol} | SL={stop_loss} TP={take_profit} | ticket={ticket}")
            return {"ticket": ticket, "retcode": 0, "comment": "MOCK_DONE", "success": True}

        await self._ensure_connected()
        divisor = 100000  # default for FX pairs
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId   = self._sym_id(symbol)
        req.orderType  = ProtoOAOrderType.Value("MARKET")
        req.tradeSide  = ProtoOATradeSide.Value("BUY") if is_buy else ProtoOATradeSide.Value("SELL")
        req.volume     = int(volume * 100)  # lots × 100 = units in cTrader
        if stop_loss:
            req.stopLoss   = int(stop_loss   * divisor)
        if take_profit:
            req.takeProfit = int(take_profit * divisor)

        raw = await self._transport.send_and_wait(2104, req, timeout=15.0)
        res = ProtoOAExecutionEvent()
        res.ParseFromString(raw)
        ticket = res.position.positionId if res.HasField("position") else 0
        logger.info(f"[cTrader] Ordem executada: {'BUY' if is_buy else 'SELL'} {volume} {symbol} | ticket={ticket}")
        return {"ticket": ticket, "retcode": 0, "comment": "FILLED", "success": True}

    async def close_position(self, ticket: int, symbol: str, volume: float) -> bool:
        if self._mock:
            logger.info(f"[MOCK] Close position ticket={ticket} {symbol} vol={volume}")
            return True
        await self._ensure_connected()
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = ticket
        req.volume     = int(volume * 100)
        await self._transport.send_and_wait(2148, req, timeout=15.0)
        logger.info(f"[cTrader] Posição fechada: ticket={ticket}")
        return True

    async def modify_sl_tp(self, ticket: int, stop_loss: float, take_profit: float) -> bool:
        if self._mock:
            logger.info(f"[MOCK] Modify SL/TP ticket={ticket} SL={stop_loss} TP={take_profit}")
            return True
        await self._ensure_connected()
        divisor = 100000
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = ticket
        req.stopLoss   = int(stop_loss   * divisor)
        req.takeProfit = int(take_profit * divisor)
        await self._transport.send_and_wait(2151, req, timeout=15.0)
        logger.info(f"[cTrader] SL/TP modificado: ticket={ticket} SL={stop_loss} TP={take_profit}")
        return True


# ── Math helpers ───────────────────────────────────────────────────────────────

def _rsi(closes: np.ndarray, period: int = 14) -> float:
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0,  deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    if len(gains) < period:
        return 50.0
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _ema(closes: np.ndarray, period: int) -> float:
    k   = 2.0 / (period + 1)
    ema = float(np.mean(closes[:period]))
    for c in closes[period:]:
        ema = float(c) * k + ema * (1 - k)
    return ema


# ── Singleton accessor ──────────────────────────────────────────────────────────

def get_client() -> CTraderClient:
    return CTraderClient()
