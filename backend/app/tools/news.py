import httpx
import feedparser
from datetime import datetime, timedelta

_cache: dict = {"data": None, "expires": datetime.min}
CACHE_TTL = timedelta(minutes=15)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"


async def _fetch_cryptopanic(limit: int = 8) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                CRYPTOPANIC_URL,
                params={
                    "public": "true",
                    "filter": "hot",
                    "currencies": "BTC,ETH",
                    "kind": "news",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            items = []
            for post in data.get("results", [])[:limit]:
                sentiment = "neutral"
                votes = post.get("votes", {})
                if votes.get("positive", 0) > votes.get("negative", 0) + 2:
                    sentiment = "positive"
                elif votes.get("negative", 0) > votes.get("positive", 0) + 2:
                    sentiment = "negative"
                items.append({
                    "title": post.get("title", ""),
                    "source": post.get("source", {}).get("title", "CryptoPanic"),
                    "sentiment": sentiment,
                    "url": post.get("url", ""),
                    "published_at": post.get("published_at", ""),
                })
            return items
    except Exception:
        return []


async def _fetch_coindesk(limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(COINDESK_RSS, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
            items = []
            for entry in feed.entries[:limit]:
                items.append({
                    "title": entry.get("title", ""),
                    "source": "CoinDesk",
                    "sentiment": "neutral",
                    "url": entry.get("link", ""),
                    "published_at": entry.get("published", ""),
                })
            return items
    except Exception:
        return []


async def get_crypto_news(limit: int = 10) -> list[dict]:
    global _cache
    if _cache["data"] and datetime.utcnow() < _cache["expires"]:
        return _cache["data"]

    cryptopanic = await _fetch_cryptopanic(limit=8)
    coindesk = await _fetch_coindesk(limit=5)

    combined = cryptopanic + coindesk
    combined = combined[:limit]

    _cache["data"] = combined
    _cache["expires"] = datetime.utcnow() + CACHE_TTL
    return combined


def format_news_summary(news: list[dict]) -> str:
    if not news:
        return "Sem notícias disponíveis no momento."
    lines = []
    for item in news[:8]:
        sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(
            item.get("sentiment", "neutral"), "⚪"
        )
        lines.append(f"{sentiment_icon} [{item['source']}] {item['title']}")
    return "\n".join(lines)
