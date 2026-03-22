# TRADING.md — Regras de Execução Forex

## Pares Monitorados

| Par     | Pip Value | Volatilidade | Prioridade |
|---------|-----------|--------------|------------|
| EURUSD  | 0.0001    | Média        | Alta       |
| GBPUSD  | 0.0001    | Alta         | Alta       |
| USDJPY  | 0.01      | Média        | Média      |
| AUDUSD  | 0.0001    | Média        | Média      |

## Critério de Execução

Execute apenas quando **TODOS** os critérios abaixo forem satisfeitos:

1. `total_confidence` ≥ 6.5
2. `confluencia_count` ≥ 4
3. `fase_macro` não é "indefinido" (ou, se indefinido, `confluencia_count` ≥ 5)
4. Nenhuma posição aberta no mesmo par
5. Sem headlines de alto impacto nas últimas 2h

## Sizing de Posição

Conta demo: ~$500 USD
- **Volume padrão**: 0.01 lotes (micro lot) = $0.10/pip
- **Máximo**: 0.02 lotes com `total_confidence` ≥ 8.5
- **Nunca arriscar > 5% do equity por operação**

## Stop Loss / Take Profit Dinâmico

Baseado no ATR do par:
- SL = `entrada × 0.04` (4% fixo, conservador para Forex)
- TP = `entrada × 0.12` (12% fixo, R:R 3:1)

## Filtros de Sessão

- **Melhor horário**: 08:00–17:00 UTC (sobreposição Londres/NY)
- **Evitar**: 22:00–07:00 UTC (baixa liquidez asiática exceto USDJPY)
- **Evitar**: 30min antes/depois de NFP, CPI, decisão do Fed

## Regras de Saída Antecipada

- RSI cruza 50 contra a posição → avaliar saída
- EMA9 cruza EMA21 contra a posição → saída obrigatória
- Confluência cai para < 2 → fechar posição

## Proteção de Capital

- 3 perdas seguidas → pausar operações por 1 ciclo
- Drawdown > 15% → pausar e notificar via Telegram
- Lucro ≥ 8% na posição → trailing stop automático

## Notícias de Alto Impacto (HOLD obrigatório)

- NFP (Non-Farm Payrolls) — 1ª sexta do mês
- CPI / Core CPI
- Decisão de taxa de juros (Fed, BCE, BoE, RBA)
- PIB trimestral
- Guerra, crise financeira, default soberano
