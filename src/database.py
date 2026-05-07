from __future__ import annotations

from pathlib import Path

import aiosqlite


MODEL_PRICING_SEED = (
    ("gemini", "google", "Gemini Flash", 1, 1, 1),
    ("deepseek", "deepseek", "DeepSeek", 1, 0, 2),
    ("qwen", "alibaba", "通义千问", 1, 0, 3),
    ("glm4", "zhipu", "GLM-4", 2, 0, 4),
    ("gpt4", "openai", "GPT-4", 3, 0, 5),
    ("claude", "anthropic", "Claude", 3, 0, 6),
)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        preferred_model TEXT NOT NULL DEFAULT 'gemini',
        points_balance INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT 1,
        password_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        stock_code TEXT NOT NULL,
        stock_name TEXT NOT NULL DEFAULT '',
        cost_price REAL NOT NULL DEFAULT 0,
        model_id TEXT NOT NULL DEFAULT 'gemini',
        is_active BOOLEAN NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, stock_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint TEXT UNIQUE NOT NULL,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        user_agent TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code TEXT NOT NULL,
        stock_name TEXT NOT NULL DEFAULT '',
        trade_date TEXT NOT NULL,
        model_id TEXT NOT NULL,
        analysis_version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT NOT NULL DEFAULT '',
        market_data_json TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        news_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(stock_code, trade_date, model_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_context (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        stock_code TEXT NOT NULL,
        context_type TEXT NOT NULL,
        content_json TEXT NOT NULL,
        trade_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS personalized_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        stock_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        model_id TEXT NOT NULL,
        result_json TEXT NOT NULL,
        context_snapshot_json TEXT NOT NULL DEFAULT '',
        points_consumed INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, stock_code, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_pricing (
        model_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        display_name TEXT NOT NULL,
        points_per_call INTEGER NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        stock_code TEXT,
        model_id TEXT,
        trade_date TEXT,
        points_consumed INTEGER NOT NULL DEFAULT 0,
        is_personalized BOOLEAN NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        type TEXT NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        points_delta INTEGER NOT NULL,
        channel TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL,
        token_type TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, is_active, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_stock ON subscriptions(stock_code, model_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_cache_lookup ON analysis_cache(stock_code, trade_date, model_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_context_lookup ON user_context(user_id, stock_code, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_email_tokens_hash ON email_tokens(token_hash)",
)


async def connect(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _column_exists(conn: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    cursor = await conn.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    await cursor.close()
    return any(str(row["name"]) == column_name for row in rows)


async def _ensure_schema_migrations(conn: aiosqlite.Connection) -> None:
    if not await _column_exists(conn, "users", "daily_push_time"):
        await conn.execute("ALTER TABLE users ADD COLUMN daily_push_time TEXT NOT NULL DEFAULT '08:30'")
    if not await _column_exists(conn, "users", "push_timezone"):
        await conn.execute("ALTER TABLE users ADD COLUMN push_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
    if not await _column_exists(conn, "users", "last_daily_push_trade_date"):
        await conn.execute("ALTER TABLE users ADD COLUMN last_daily_push_trade_date TEXT NOT NULL DEFAULT ''")
    if not await _column_exists(conn, "users", "last_daily_push_sent_at"):
        await conn.execute("ALTER TABLE users ADD COLUMN last_daily_push_sent_at TEXT NOT NULL DEFAULT ''")
    if not await _column_exists(conn, "users", "email_verified_at"):
        await conn.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT NOT NULL DEFAULT ''")


async def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await connect(db_path)
    try:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)
        await _ensure_schema_migrations(conn)
        await conn.executemany(
            """
            INSERT INTO model_pricing
                (model_id, provider, display_name, points_per_call, is_active, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id) DO UPDATE SET
                provider=excluded.provider,
                display_name=excluded.display_name,
                points_per_call=excluded.points_per_call,
                is_active=excluded.is_active,
                sort_order=excluded.sort_order
            """,
            MODEL_PRICING_SEED,
        )
        await conn.commit()
    finally:
        await conn.close()


async def fetch_one(db_path: str, query: str, params: tuple[object, ...] = ()) -> aiosqlite.Row | None:
    conn = await connect(db_path)
    try:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row
    finally:
        await conn.close()


async def fetch_all(db_path: str, query: str, params: tuple[object, ...] = ()) -> list[aiosqlite.Row]:
    conn = await connect(db_path)
    try:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows
    finally:
        await conn.close()


async def execute(db_path: str, query: str, params: tuple[object, ...] = ()) -> int:
    conn = await connect(db_path)
    try:
        cursor = await conn.execute(query, params)
        await conn.commit()
        lastrowid = cursor.lastrowid
        await cursor.close()
        return int(lastrowid or 0)
    finally:
        await conn.close()
