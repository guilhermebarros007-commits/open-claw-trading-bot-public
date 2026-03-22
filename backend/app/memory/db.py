import aiosqlite
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_data TEXT NOT NULL,
                news TEXT NOT NULL,
                hype_analysis TEXT,
                oracle_analysis TEXT,
                vitalik_analysis TEXT,
                lux_decision TEXT,
                lux_raw TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL,
                sl_price REAL,
                tp_price REAL,
                status TEXT NOT NULL DEFAULT 'executed',
                confidence REAL,
                decision_json TEXT,
                order_result TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def save_message(agent_id: str, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (agent_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, role, content, datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_chat_history(agent_id: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, created_at FROM messages WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            (agent_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in reversed(rows)]


async def save_report(
    market_data: dict,
    news: list,
    hype_analysis: str,
    oracle_analysis: str,
    vitalik_analysis: str,
    lux_decision: str,
    lux_raw: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO reports
               (market_data, news, hype_analysis, oracle_analysis, vitalik_analysis, lux_decision, lux_raw, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                json.dumps(market_data),
                json.dumps(news),
                hype_analysis,
                oracle_analysis,
                vitalik_analysis,
                lux_decision,
                lux_raw,
                datetime.utcnow().isoformat(),
            )
        )
        await db.commit()
        return cursor.lastrowid


async def get_reports(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["market_data"] = json.loads(d["market_data"])
        d["news"] = json.loads(d["news"])
        result.append(d)
    return result


async def save_trade(
    agent_id: str,
    coin: str,
    side: str,
    size: float,
    entry_price: float = 0,
    sl_price: float = 0,
    tp_price: float = 0,
    status: str = "executed",
    confidence: float = 0,
    decision_json: str = "",
    order_result: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO trades
               (agent_id, coin, side, size, entry_price, sl_price, tp_price, status, confidence, decision_json, order_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id, coin, side, size, entry_price, sl_price, tp_price,
                status, confidence, decision_json, order_result,
                datetime.utcnow().isoformat(),
            )
        )
        await db.commit()
        return cursor.lastrowid


async def get_trades_today(agent_id: str = None) -> list[dict]:
    """Trades executados hoje (UTC) — usado pelo Risk Audit."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if agent_id:
            async with db.execute(
                "SELECT * FROM trades WHERE agent_id = ? AND created_at LIKE ? ORDER BY id DESC",
                (agent_id, f"{today}%")
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM trades WHERE created_at LIKE ? ORDER BY id DESC",
                (f"{today}%",)
            ) as cursor:
                rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_trades(agent_id: str = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if agent_id:
            async with db.execute(
                "SELECT * FROM trades WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
    return [dict(r) for r in rows]
