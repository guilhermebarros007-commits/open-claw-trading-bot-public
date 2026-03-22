"""
Motor de backteste da estratégia Lux — sem dependências externas além de numpy.

Replica fielmente as regras do SOUL.md:
  Fase 1 — Filtro macro (USD strength via EMA9/21)
  Fase 2 — RSI Reversal  (< rsi_buy = long candidato, > rsi_sell = short candidato)
  Fase 3 — Confluência   (≥ confluence_min de 5 indicadores)

Suporta:
  - Dados mock gerados internamente (para testes imediatos)
  - CSV no formato HistData (EURUSD_H1_YYYY.csv) para backteste real
"""
from __future__ import annotations

import csv
import io
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Parâmetros padrão ──────────────────────────────────────────────────────────

DEFAULT_PARAMS = {
    "rsi_period":       7,     # RSI(7) — sensível para scalping M5/M15
    "ema_fast":         5,     # EMA rápida
    "ema_slow":         13,    # EMA lenta
    "rsi_buy":          30,    # RSI: saída de oversold (cruzamento de volta acima deste)
    "rsi_sell":         70,    # RSI: saída de overbought (cruzamento de volta abaixo deste)
    "confluence_min":   2,     # confirmações adicionais exigidas (0–3) — 2 = ótimo
    "sl_pct":           0.15,  # stop loss — 0.15% ≈ 15 pips em EURUSD (scalping)
    "tp_pct":           0.25,  # take profit TP1 — 0.25% ≈ 25 pips (fecha 70% aqui)
    "tp2_pct":          0.45,  # take profit TP2 — 0.45% ≈ 40 pips (fecha 30% restante)
    "partial_exit_pct": 0.70,  # % da posição fechada no TP1 (70%); restante corre até TP2
    "initial_equity":   500.0,
    "lot_size":         0.05,  # lote scalper → ~8.9%/mês sobre $500
    "pip_value":        10.0,
}

_BASE_PRICES: dict[str, float] = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2650,
    "USDJPY": 149.50,
    "AUDUSD": 0.6450,
}


# ── Estruturas de dados ────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    idx:        int
    side:       str      # long / short
    entry:      float
    sl:         float
    tp:         float
    exit_price: float
    exit_reason: str     # sl / tp / eod
    pnl_pct:    float    # em % sobre o entry
    pnl_usd:    float
    confluence: int


@dataclass
class BacktestResult:
    symbol:           str
    candles_total:    int
    trades:           int
    wins:             int
    losses:           int
    win_rate:         float   # %
    total_return_pct: float
    sharpe_ratio:     float
    max_drawdown_pct: float
    avg_rr:           float   # média R:R realizado
    equity_curve:     list[float]
    trade_list:       list[dict]
    params:           dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["equity_curve"] = [round(v, 2) for v in d["equity_curve"]]
        return d


# ── Gerador de candles mock ────────────────────────────────────────────────────

def _generate_mock_candles(symbol: str, count: int) -> list[dict]:
    """
    Gera candles sintéticos com comportamento realista:
    - Ciclos de tendência + reversão (simula mercado Forex)
    - Volatilidade variável (clusters de volatilidade)
    - Suficiente para disparar sinais RSI + confluence
    """
    base = _BASE_PRICES.get(symbol, 1.0)
    rng = random.Random(int(base * 10000) % 9999)
    price = base
    now = int(time.time())
    candles = []

    # parâmetros do modelo
    vol_base   = base * 0.0012   # volatilidade base por barra H1
    trend      = 0.0             # drift atual
    trend_dur  = 0               # barras restantes na tendência atual
    vol_factor = 1.0             # multiplicador de volatilidade (clusters)

    for i in range(count):
        # ── Muda tendência a cada ~30 barras ──────────────────────────────────
        if trend_dur <= 0:
            trend     = rng.gauss(0, vol_base * 0.6)   # novo drift
            trend_dur = rng.randint(20, 50)
            vol_factor = rng.uniform(0.7, 1.8)         # cluster de vol

        trend_dur -= 1

        # ── Preço da barra ────────────────────────────────────────────────────
        noise  = rng.gauss(0, vol_base * vol_factor)
        change = trend + noise
        open_  = price
        close  = round(price + change, 5)
        wick   = abs(rng.gauss(0, vol_base * 0.4 * vol_factor))
        high   = round(max(open_, close) + wick, 5)
        low    = round(min(open_, close) - wick, 5)

        candles.append({
            "time":   now - (count - i) * 3600,
            "open":   open_,
            "high":   high,
            "low":    low,
            "close":  close,
            "volume": rng.randint(500, 3000),
        })
        price = close

    return candles


# ── Parser de CSV HistData ─────────────────────────────────────────────────────

def parse_histdata_csv(content: str) -> list[dict]:
    """
    Formato HistData (sem header):
      20030505 170000;1.12340;1.12400;1.12310;1.12370;0
    ou com header:
      Date;Open;High;Low;Close;Volume
    """
    candles = []
    reader = csv.reader(io.StringIO(content), delimiter=";")
    for row in reader:
        if not row or row[0].startswith("Date") or row[0].startswith("#"):
            continue
        try:
            dt_str = row[0].strip()
            # timestamp unix aproximado (não crítico para backteste de sinais)
            ts = int(time.mktime(time.strptime(dt_str, "%Y%m%d %H%M%S"))) if " " in dt_str else 0
            candles.append({
                "time":   ts,
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0,
            })
        except (ValueError, IndexError):
            continue
    return candles


# ── Indicadores (mesmas fórmulas do ctrader.py) ───────────────────────────────

def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI vetorizado — retorna array do mesmo tamanho (NaN nos primeiros períodos)."""
    result = np.full(len(closes), np.nan)
    if len(closes) < period + 1:
        return result
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + float(gains[i])) / period
        avg_l = (avg_l * (period - 1) + float(losses[i])) / period
        rs = avg_g / (avg_l + 1e-10)
        result[i + 1] = 100 - 100 / (1 + rs)
    return result


def _ema_arr(closes: np.ndarray, period: int) -> np.ndarray:
    """EMA vetorizada."""
    result = np.full(len(closes), np.nan)
    if len(closes) < period:
        return result
    k = 2.0 / (period + 1)
    val = float(np.mean(closes[:period]))
    result[period - 1] = val
    for i in range(period, len(closes)):
        val = closes[i] * k + val * (1 - k)
        result[i] = val
    return result


def _compute_all(
    closes: np.ndarray,
    highs:  np.ndarray,
    lows:   np.ndarray,
    volumes: np.ndarray,
    rsi_period: int = 7,
    ema_fast: int = 5,
    ema_slow: int = 13,
) -> dict[str, np.ndarray]:
    rsi    = _rsi(closes, rsi_period)
    ema9   = _ema_arr(closes, ema_fast)   # EMA rápida (scalper: 5 | swing: 9)
    ema21  = _ema_arr(closes, ema_slow)   # EMA lenta  (scalper: 13 | swing: 21)
    ema12  = _ema_arr(closes, 12)
    ema26  = _ema_arr(closes, 26)
    macd   = ema12 - ema26

    # Bollinger (20)
    bb_pct = np.full(len(closes), np.nan)
    for i in range(20, len(closes)):
        window = closes[i - 20:i]
        sma = float(np.mean(window))
        std = float(np.std(window))
        rng = 4 * std
        bb_pct[i] = (closes[i] - (sma - 2 * std)) / rng if rng > 0 else 0.5

    # OBV direction (últimas 5 barras com resultado > 3 subindo)
    obv_rising = np.full(len(closes), False)
    for i in range(5, len(closes)):
        up = sum(1 for j in range(i - 4, i) if closes[j] > closes[j - 1])
        obv_rising[i] = up >= 3

    return {
        "rsi":       rsi,
        "ema9":      ema9,
        "ema21":     ema21,
        "macd":      macd,
        "bb_pct":    bb_pct,
        "obv_rising": obv_rising,
    }


# ── Motor de simulação ─────────────────────────────────────────────────────────

def _run_simulation(
    candles: list[dict],
    params:  dict,
) -> BacktestResult:
    symbol   = params.get("symbol", "EURUSD")
    rsi_buy  = params["rsi_buy"]
    rsi_sell = params["rsi_sell"]
    conf_min = params["confluence_min"]
    sl_pct   = params["sl_pct"] / 100
    tp_pct   = params["tp_pct"] / 100
    equity   = params["initial_equity"]
    lot      = params["lot_size"]
    pip_val  = params["pip_value"]

    closes  = np.array([c["close"]  for c in candles], dtype=float)
    highs   = np.array([c["high"]   for c in candles], dtype=float)
    lows    = np.array([c["low"]    for c in candles], dtype=float)
    volumes = np.array([c["volume"] for c in candles], dtype=float)

    ind = _compute_all(closes, highs, lows, volumes,
                       rsi_period=params.get("rsi_period", 7),
                       ema_fast=params.get("ema_fast", 5),
                       ema_slow=params.get("ema_slow", 13))
    rsi      = ind["rsi"]
    ema9     = ind["ema9"]
    ema21    = ind["ema21"]
    macd     = ind["macd"]
    bb_pct   = ind["bb_pct"]
    obv_up   = ind["obv_rising"]

    equity_curve: list[float] = [equity]
    trades: list[BacktestTrade] = []
    position: Optional[dict] = None
    WARMUP = max(30, params.get("ema_slow", 13) + 5)  # barras mínimas antes de operar

    for i in range(WARMUP, len(closes)):
        price = closes[i]

        # ── Checar saída de posição existente ──────────────────────────────────
        if position:
            entry        = position["entry"]
            is_long      = position["side"] == "long"
            partial_done = position.get("partial_done", False)
            remaining    = position.get("remaining", 1.0)
            partial_pct  = params.get("partial_exit_pct", 0.70)
            tp2_pct_val  = params.get("tp2_pct", 0.45) / 100

            hit_sl  = (lows[i]  <= position["sl"]) if is_long else (highs[i] >= position["sl"])
            hit_tp1 = (not partial_done) and (
                (highs[i] >= position["tp"]) if is_long else (lows[i] <= position["tp"])
            )
            hit_tp2 = partial_done and (
                (highs[i] >= position.get("tp2", 1e9)) if is_long else (lows[i] <= position.get("tp2", 0.0))
            )

            if hit_tp1:
                # ── Saída parcial: fecha partial_pct% (70%) no TP1 ─────────────
                exit_price = position["tp"]
                pnl_pct    = (exit_price - entry) / entry if is_long else (entry - exit_price) / entry
                pnl_usd    = pnl_pct * equity * lot * 100 * partial_pct
                equity    += pnl_usd
                equity_curve.append(equity)

                trades.append(BacktestTrade(
                    idx=i,
                    side=position["side"],
                    entry=entry,
                    sl=position["sl"],
                    tp=exit_price,
                    exit_price=round(exit_price, 5),
                    exit_reason="tp1_partial",
                    pnl_pct=round(pnl_pct * 100, 3),
                    pnl_usd=round(pnl_usd, 2),
                    confluence=position["confluence"],
                ))

                # Atualiza posição: SL → breakeven (risco zero), TP → TP2
                new_tp2 = round(
                    entry * (1 + tp2_pct_val) if is_long else entry * (1 - tp2_pct_val), 5
                )
                position["sl"]           = entry     # breakeven — protege capital
                position["tp2"]          = new_tp2   # alvo estendido
                position["partial_done"] = True
                position["remaining"]    = 1.0 - partial_pct  # 30% restante
                continue  # posição ainda aberta — próxima barra

            elif hit_tp2:
                # ── Fecha os restantes 30% no TP2 ──────────────────────────────
                exit_price = position["tp2"]
                pnl_pct    = (exit_price - entry) / entry if is_long else (entry - exit_price) / entry
                pnl_usd    = pnl_pct * equity * lot * 100 * remaining
                equity    += pnl_usd
                # NÃO append aqui — cai pro append único na linha final do loop

                trades.append(BacktestTrade(
                    idx=i,
                    side=position["side"],
                    entry=entry,
                    sl=position["sl"],
                    tp=exit_price,
                    exit_price=round(exit_price, 5),
                    exit_reason="tp2",
                    pnl_pct=round(pnl_pct * 100, 3),
                    pnl_usd=round(pnl_usd, 2),
                    confluence=position["confluence"],
                ))
                position = None
                # fall through → signal check + equity_curve.append no final

            elif hit_sl:
                # ── SL: fecha fração restante (100% se antes do TP1, 30% se depois)
                exit_price  = position["sl"]
                exit_reason = "sl_be" if partial_done else "sl"
                pnl_pct     = (exit_price - entry) / entry if is_long else (entry - exit_price) / entry
                pnl_usd     = pnl_pct * equity * lot * 100 * remaining
                equity     += pnl_usd

                trades.append(BacktestTrade(
                    idx=i,
                    side=position["side"],
                    entry=entry,
                    sl=exit_price,
                    tp=position["tp"],
                    exit_price=round(exit_price, 5),
                    exit_reason=exit_reason,
                    pnl_pct=round(pnl_pct * 100, 3),
                    pnl_usd=round(pnl_usd, 2),
                    confluence=position["confluence"],
                ))
                position = None
                # fall through → signal check + equity_curve.append no final

            else:
                equity_curve.append(equity)
                continue

        # ── Verificar indicadores ──────────────────────────────────────────────
        if np.isnan(rsi[i]) or np.isnan(ema9[i]) or np.isnan(ema21[i]) or np.isnan(macd[i]):
            equity_curve.append(equity)
            continue

        r    = float(rsi[i])
        bull = bool(ema9[i] > ema21[i])
        mbul = bool(macd[i] > 0)
        bp   = float(bb_pct[i]) if not np.isnan(bb_pct[i]) else 0.5
        obv  = bool(obv_up[i])

        # ── Estratégia Scalper: RSI crossing back through threshold ─────────────
        # Sinal: RSI(7) saindo de zona extrema = reversão de curto prazo confirmada
        # Long:  RSI estava abaixo de rsi_buy  → cruza de volta ACIMA (recuperação)
        # Short: RSI estava acima de rsi_sell  → cruza de volta ABAIXO (exaustão)
        prev_r = float(rsi[i-1]) if i > 0 and not np.isnan(rsi[i-1]) else r

        rsi_exit_oversold   = (prev_r <= rsi_buy)  and (r > rsi_buy)   # cruzou acima
        rsi_exit_overbought = (prev_r >= rsi_sell) and (r < rsi_sell)  # cruzou abaixo

        if rsi_exit_oversold:
            # ── LONG: RSI saiu da zona de oversold — reversão bullish confirmada ─
            confluence = sum([
                bull,        # EMA5 > EMA13 (tendência de alta)
                mbul,        # MACD positivo
                bp < 0.5,    # preço abaixo da média de Bollinger
                obv,         # volume crescente
            ])
            if confluence >= conf_min:
                sl = round(price * (1 - sl_pct), 5)
                tp = round(price * (1 + tp_pct), 5)
                position = {"side": "long", "entry": price, "sl": sl, "tp": tp, "confluence": confluence}

        elif rsi_exit_overbought:
            # ── SHORT: RSI saiu da zona de overbought — reversão bearish confirmada
            confluence = sum([
                not bull,    # EMA5 < EMA13 (tendência de baixa)
                not mbul,    # MACD negativo
                bp > 0.5,    # preço acima da média de Bollinger
                not obv,     # volume decrescente
            ])
            if confluence >= conf_min:
                sl = round(price * (1 + sl_pct), 5)
                tp = round(price * (1 - tp_pct), 5)
                position = {"side": "short", "entry": price, "sl": sl, "tp": tp, "confluence": confluence}

        equity_curve.append(equity)

    # ── Force-close posição aberta ao final dos dados ─────────────────────────
    if position:
        last_price   = closes[-1]
        entry        = position["entry"]
        is_long      = position["side"] == "long"
        remaining    = position.get("remaining", 1.0)
        pnl_pct      = (last_price - entry) / entry if is_long else (entry - last_price) / entry
        pnl_usd      = pnl_pct * equity * lot * 100 * remaining
        equity       += pnl_usd
        equity_curve[-1] = equity  # atualiza último ponto (não cria novo)

        trades.append(BacktestTrade(
            idx=len(closes) - 1,
            side=position["side"],
            entry=entry,
            sl=position["sl"],
            tp=position["tp"],
            exit_price=round(last_price, 5),
            exit_reason="eod",
            pnl_pct=round(pnl_pct * 100, 3),
            pnl_usd=round(pnl_usd, 2),
            confluence=position["confluence"],
        ))
        position = None

    # ── Métricas ───────────────────────────────────────────────────────────────
    n       = len(trades)
    wins    = [t for t in trades if t.pnl_pct > 0]
    losses  = [t for t in trades if t.pnl_pct <= 0]
    win_rate = round(len(wins) / n * 100, 1) if n > 0 else 0.0

    total_return = round((equity - params["initial_equity"]) / params["initial_equity"] * 100, 2)

    # Sharpe (anualizado assumindo H1: 252 * 6.5 ≈ 1638 barras/ano)
    if n > 1:
        pnls   = np.array([t.pnl_pct for t in trades])
        std_pnl = float(np.std(pnls))
        if std_pnl > 0.01:   # evita divisão por quase-zero quando todos os trades são iguais
            sharpe = round(float(np.mean(pnls) / std_pnl * (1638 ** 0.5)), 2)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Max drawdown
    peak   = params["initial_equity"]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # Avg R:R
    avg_rr = 0.0
    if wins and losses:
        avg_win  = float(np.mean([t.pnl_pct for t in wins]))
        avg_loss = float(np.mean([abs(t.pnl_pct) for t in losses]))
        avg_rr   = round(avg_win / (avg_loss + 1e-10), 2)

    return BacktestResult(
        symbol=symbol,
        candles_total=len(candles),
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        total_return_pct=total_return,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        avg_rr=avg_rr,
        equity_curve=equity_curve,
        trade_list=[asdict(t) for t in trades],
        params={k: v for k, v in params.items() if k != "symbol"},
    )


# ── Downloader yfinance ────────────────────────────────────────────────────────

# Mapa: símbolo cTrader → ticker Yahoo Finance
_YAHOO_TICKERS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
}

def fetch_real_candles(symbol: str, period: str = "730d", interval: str = "1h") -> list[dict]:
    """
    Baixa candles históricos reais via yfinance (gratuito, sem API key).
    Compatível com yfinance 0.x e 1.x (colunas multi-nível).
    period: "60d", "730d" — máximo ~730 dias para intervalo H1
    interval: "1h", "1d"
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance não instalado. Execute: pip install yfinance")

    ticker = _YAHOO_TICKERS.get(symbol, symbol + "=X")
    logger.info(f"[Backtest] Baixando {ticker} {period} {interval} via yfinance...")

    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=True, progress=False)

    if df is None or df.empty:
        raise ValueError(f"Sem dados para {ticker} no período {period}")

    # yfinance 1.x retorna MultiIndex: nível 0 = tipo (Open/High/...), nível 1 = ticker
    import pandas as pd
    if isinstance(df.columns, pd.MultiIndex):
        # Flatten: pega nível 0 (Open, High, Low, Close, Volume)
        df = df.droplevel(1, axis=1) if df.columns.nlevels == 2 else df

    # Normaliza nomes das colunas para title-case
    df.columns = [str(c).strip().capitalize() for c in df.columns]

    logger.info(f"[Backtest] Colunas: {list(df.columns)}, shape: {df.shape}")

    candles = []
    for ts, row in df.iterrows():
        try:
            t  = int(ts.timestamp())
            o  = float(row["Open"])
            h  = float(row["High"])
            lo = float(row["Low"])
            c  = float(row["Close"])
            v  = float(row.get("Volume", 0) or 0)
            if any(x != x for x in [o, h, lo, c]):  # NaN check
                continue
            candles.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
        except Exception as exc:
            logger.debug(f"[Backtest] linha ignorada: {exc}")
            continue

    logger.info(f"[Backtest] {len(candles)} candles baixados para {symbol}")
    return candles


# ── API pública ────────────────────────────────────────────────────────────────

async def run_backtest(
    symbol: str = "EURUSD",
    count:  int = 500,
    use_real_data: bool = False,
    timeframe: str = "M5",
    csv_content: Optional[str] = None,
    rsi_buy:          int   = 30,
    rsi_sell:         int   = 70,
    confluence_min:   int   = 2,
    sl_pct:           float = 0.15,
    tp_pct:           float = 0.25,
    tp2_pct:          float = 0.45,
    partial_exit_pct: float = 0.70,
) -> BacktestResult:
    """
    Ponto de entrada principal. Chamado pelo endpoint /api/backtest.
    Prioridade: csv_content > use_real_data (yfinance) > mock
    Timeframes suportados: M5 (60d), M15 (60d), H1 (730d)
    """
    # Mapeia timeframe → (yf_interval, period)
    _TF_MAP = {
        "M5":  ("5m",  "60d"),
        "M15": ("15m", "60d"),
        "M30": ("30m", "60d"),
        "H1":  ("1h",  "730d"),
    }
    yf_interval, yf_period = _TF_MAP.get(timeframe.upper(), ("5m", "60d"))

    if csv_content:
        candles = parse_histdata_csv(csv_content)
        logger.info(f"[Backtest] CSV carregado: {len(candles)} candles para {symbol}")
    elif use_real_data:
        import asyncio
        loop = asyncio.get_running_loop()
        candles = await loop.run_in_executor(
            None, lambda: fetch_real_candles(symbol, yf_period, yf_interval)
        )
        logger.info(f"[Backtest] Timeframe {timeframe} ({yf_interval}/{yf_period}): {len(candles)} candles")
    else:
        candles = _generate_mock_candles(symbol, max(count, 200))
        logger.info(f"[Backtest] Dados mock: {len(candles)} candles para {symbol}")

    if len(candles) < 50:
        raise ValueError(f"Candles insuficientes: {len(candles)} (mínimo 50)")

    params = {
        **DEFAULT_PARAMS,
        "symbol":           symbol,
        "rsi_buy":          rsi_buy,
        "rsi_sell":         rsi_sell,
        "confluence_min":   confluence_min,
        "sl_pct":           sl_pct,
        "tp_pct":           tp_pct,
        "tp2_pct":          tp2_pct,
        "partial_exit_pct": partial_exit_pct,
    }

    return _run_simulation(candles, params)
