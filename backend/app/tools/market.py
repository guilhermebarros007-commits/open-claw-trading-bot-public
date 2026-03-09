import httpx
from datetime import datetime, timedelta

_cache: dict = {"data": None, "expires": datetime.min}
CACHE_TTL = timedelta(minutes=5)

COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


async def get_market_data() -> dict:
    global _cache
    if _cache["data"] and datetime.utcnow() < _cache["expires"]:
        return _cache["data"]

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            price_resp = await client.get(
                COINGECKO_PRICE_URL,
                params={
                    "ids": "bitcoin,ethereum",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_24hr_vol": "true",
                },
            )
            price_resp.raise_for_status()
            prices = price_resp.json()

            global_resp = await client.get(COINGECKO_GLOBAL_URL)
            global_resp.raise_for_status()
            global_data = global_resp.json()["data"]

            data = {
                "btc_price": prices["bitcoin"]["usd"],
                "btc_change_24h": round(prices["bitcoin"].get("usd_24h_change", 0), 2),
                "btc_volume_24h": prices["bitcoin"].get("usd_24h_vol", 0),
                "eth_price": prices["ethereum"]["usd"],
                "eth_change_24h": round(prices["ethereum"].get("usd_24h_change", 0), 2),
                "eth_volume_24h": prices["ethereum"].get("usd_24h_vol", 0),
                "btc_dominance": round(
                    global_data["market_cap_percentage"].get("btc", 0), 2
                ),
                "total_market_cap_usd": global_data.get("total_market_cap", {}).get("usd", 0),
                "fetched_at": datetime.utcnow().isoformat(),
            }

            _cache["data"] = data
            _cache["expires"] = datetime.utcnow() + CACHE_TTL
            return data

        except Exception as e:
            if _cache["data"]:
                return _cache["data"]
            return {
                "error": str(e),
                "btc_price": 0,
                "eth_price": 0,
                "btc_dominance": 0,
                "btc_change_24h": 0,
                "eth_change_24h": 0,
                "btc_volume_24h": 0,
                "eth_volume_24h": 0,
                "fetched_at": datetime.utcnow().isoformat(),
            }


def format_market_summary(data: dict) -> str:
    return (
        f"BTC: ${data['btc_price']:,.0f} ({data['btc_change_24h']:+.2f}% 24h) | "
        f"Vol: ${data['btc_volume_24h']/1e9:.1f}B\n"
        f"ETH: ${data['eth_price']:,.0f} ({data['eth_change_24h']:+.2f}% 24h)\n"
        f"BTC Dominância: {data['btc_dominance']:.1f}%"
    )
