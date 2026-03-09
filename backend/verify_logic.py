import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from app.tools.hyperliquid import round_to_hl_standard

def test_rounding():
    print("--- Testing Rounding Helper ---")
    test_cases = [
        # (price, size, sz_decimals)
        (63456.78, 0.00031234, 5),
        (3452.1, 0.555555, 3),
        (1.234567, 100.123456, 1),
        (123.456789, 0.123, 2),
    ]
    
    for px, sz, dec in test_cases:
        px_str, sz_str = round_to_hl_standard(px, sz, dec)
        print(f"Input: Px={px}, Sz={sz}, Dec={dec} -> Result: Px={px_str}, Sz={sz_str}")

def test_risk_calculation():
    print("\n--- Testing Risk Calculation Logic (10%) ---")
    balance = 1000.0
    risk_pct = 0.10
    risk_amount = balance * risk_pct
    
    prices = {"BTC": 60000.0, "ETH": 3500.0, "HYPE": 15.0}
    sz_decimals = {"BTC": 5, "ETH": 4, "HYPE": 1}
    
    for asset, px in prices.items():
        raw_sz = risk_amount / px
        dec = sz_decimals[asset]
        px_str, sz_str = round_to_hl_standard(px, raw_sz, dec)
        print(f"{asset}: Balance=${balance} -> Risk=${risk_amount} -> RawSz={raw_sz:.8f} -> FinalSz={sz_str}")

if __name__ == "__main__":
    test_rounding()
    test_risk_calculation()
