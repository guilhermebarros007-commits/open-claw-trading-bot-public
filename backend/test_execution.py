import asyncio
import os
import sys
from dotenv import load_dotenv

# Define base path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

from app.tools.hyperliquid import execute_market_open, get_account_state, get_hl_market_data, get_meta, get_exchange

async def force_trade_test():
    print("🚀 Iniciando Teste de Execução Forçado...")
    
    agent_id = "hype_beast"
    # 1. Check balance
    state = await get_account_state(agent_id)
    if "error" in state:
        print(f"Erro ao buscar saldo: {state['error']}")
        return
        
    balance = state.get("total_value", 0)
    print(f"Saldo Hype Beast: {balance} USDC")
    
    if balance < 10:
        print("Saldo insuficiente para o teste (mínimo 10 USDC no Testnet).")
        return

    # 2. Simulate Lux decision
    asset = "ETH"
    is_buy = True
    
    print(f"Simulando ordem: BUY {asset}...")
    
    # 3. Execute with real logic
    # Calculate size (10% of balance)
    account_value = state.get("total_value", 0)
    market_data = await get_hl_market_data()
    price = market_data.get(f"{asset.lower()}_price", 0)
    
    if price == 0:
        print("Erro ao buscar preço do ativo.")
        return
        
    usd_size = account_value * 0.10 # 10%
    size = usd_size / price
    
    # Round size (helper handles this but let's be clean)
    from app.tools.hyperliquid import get_sz_decimals
    sz_decimals = await get_sz_decimals(asset)
    size = round(size, sz_decimals)

    print(f"Calculado: 10% de {account_value} = ${usd_size} -> {size} {asset}")

    exchange = get_exchange(agent_id)
    # Manually fix the exchange if it's broken (Testnet SDK bug)
    if not hasattr(exchange.info, "name_to_coin") or not exchange.info.name_to_coin:
        print("Injetando metadados manualmente para contornar bug do SDK...")
        meta = await get_meta()
        exchange.info.name_to_coin = {coin["name"]: i for i, coin in enumerate(meta["universe"])}

    # We need to use exchange.market_open but we'll call it via to_thread since it's synchronous
    import asyncio
    result_raw = await asyncio.to_thread(
        exchange.market_open, asset, is_buy, size, None, 0.01
    )
    
    result = {"success": True, "result": result_raw} if result_raw.get("status") == "ok" else {"success": False, "error": result_raw}
    
    if result.get("success"):
        print(f"✅ ORDEM EXECUTADA COM SUCESSO! Resultado: {result.get('result')}")
        print("Verifique seu painel na Hyperliquid e as notificações no Telegram.")
    else:
        print(f"❌ FALHA NA EXECUÇÃO DA ORDEM: {result.get('error')}")

if __name__ == "__main__":
    asyncio.run(force_trade_test())
