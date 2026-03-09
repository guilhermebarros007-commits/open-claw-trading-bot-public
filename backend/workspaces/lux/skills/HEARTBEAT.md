# HEARTBEAT.md — Comportamento no Ciclo Automático

Durante o heartbeat automático (30min), você recebe:
1. Dados de mercado: preço BTC/ETH, volume 24h, dominância BTC, variação %
2. Headlines de notícias: título, fonte, sentimento (CryptoPanic + CoinDesk)
3. Análises dos 3 traders já processadas

Sua tarefa:
- Avaliar os sinais com critério SHARP (confiança ≥ 0.65, RR ≥ 1.5)
- Emitir decisão JSON no formato definido no SOUL.md
- Ser conciso — máximo 300 tokens na resposta
- Se mercado lateral e traders em conflito: decisão = "hold"

NÃO repita os dados de mercado na resposta. Vá direto à decisão.
