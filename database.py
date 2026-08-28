import os
import re
import time
from pathlib import Path

import aiosqlite

from config import Config
from logging_utils import log_event


DB_LOGS = os.getenv("DB_LOGS", "0") == "1"


def _query_summary(query):
    clean = re.sub(r"\s+", " ", query or "").strip()
    verb = clean.split(" ", 1)[0].upper() if clean else "UNKNOWN"
    table = "unknown"
    patterns = [
        r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        r"\bINTO\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        r"\bUPDATE\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        r"\bTABLE\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        r"\bTABLE\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            table = match.group(1)
            break
    return verb, table, clean[:220]


class Database:
    @staticmethod
    async def init_schema():
        Path(Config.DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(Config.DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA journal_mode=WAL;")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    session_name TEXT PRIMARY KEY,
                    session_string TEXT,
                    api_id INTEGER,
                    api_hash TEXT,
                    device_model TEXT,
                    system_version TEXT,
                    app_version TEXT,
                    status TEXT DEFAULT 'Active',
                    cooldown_until INTEGER DEFAULT 0
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT UNIQUE,
                    used BOOLEAN DEFAULT 0
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS target_chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE,
                    title TEXT,
                    username TEXT,
                    status TEXT DEFAULT 'New',
                    last_read_msg_id INTEGER DEFAULT 0,
                    quality_score REAL DEFAULT 0,
                    rejection_reason TEXT,
                    last_checked_at TIMESTAMP
                )
            """)
            for column_name, column_type in [
                ("last_read_msg_id", "INTEGER DEFAULT 0"),
                ("quality_score", "REAL DEFAULT 0"),
                ("rejection_reason", "TEXT"),
                ("last_checked_at", "TIMESTAMP"),
                ("next_check_at", "INTEGER DEFAULT 0"),
                ("empty_reads", "INTEGER DEFAULT 0"),
                ("last_new_messages", "INTEGER DEFAULT 0"),
                ("description", "TEXT"),
                ("estimated_topic", "TEXT"),
                ("activity", "REAL DEFAULT 0"),
                ("message_count_collected", "INTEGER DEFAULT 0"),
                ("last_activity", "TIMESTAMP"),
                ("audience_segment", "TEXT"),
                ("quality_details", "TEXT"),
                ("member_count", "INTEGER DEFAULT 0"),
            ]:
                await Database._add_column(db, "target_chats", column_name, column_type)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT,
                    chat_username TEXT,
                    message_link TEXT,
                    author_id INTEGER,
                    author_username TEXT,
                    author_name TEXT,
                    text TEXT,
                    raw_type TEXT,
                    msg_at TIMESTAMP,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    has_text INTEGER DEFAULT 0,
                    is_bot INTEGER DEFAULT 0,
                    UNIQUE(chat_id, message_id)
                )
            """)

            await db.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS pains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    message_id INTEGER,
                    trace_id TEXT,
                    problem_title TEXT,
                    category TEXT,
                    loss_type TEXT,
                    wtp_signal TEXT,
                    wtp_justification TEXT,
                    context_summary TEXT,
                    raw_quotes TEXT DEFAULT '[]',
                    raw_llm_json TEXT,
                    token_usage INTEGER DEFAULT 0,
                    msg_at TIMESTAMP,
                    processing_metadata TEXT,
                    butcher_status TEXT DEFAULT 'Pending',
                    insight_id INTEGER DEFAULT NULL,
                    evidence_quote TEXT,
                    evidence_context TEXT,
                    confidence REAL DEFAULT 0,
                    quality_score REAL DEFAULT 0,
                    source_title TEXT,
                    source_username TEXT,
                    rejection_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for column_name, column_type in [
                ("evidence_quote", "TEXT"),
                ("evidence_context", "TEXT"),
                ("confidence", "REAL DEFAULT 0"),
                ("quality_score", "REAL DEFAULT 0"),
                ("source_title", "TEXT"),
                ("source_username", "TEXT"),
                ("rejection_reason", "TEXT"),
                ("author_id", "INTEGER"),
                ("author_username", "TEXT"),
                ("author_name", "TEXT"),
                ("message_link", "TEXT"),
                ("source_link", "TEXT"),
                ("author_anonymized_id", "TEXT"),
                ("profession_segment", "TEXT"),
                ("pain_summary", "TEXT"),
                ("desired_outcome", "TEXT"),
                ("current_workaround", "TEXT"),
                ("frequency", "TEXT"),
                ("estimated_time_cost", "TEXT"),
                ("estimated_money_cost", "TEXT"),
                ("urgency", "TEXT"),
                ("emotional_intensity", "TEXT"),
                ("repeatability", "TEXT"),
                ("manual_work_indicator", "INTEGER DEFAULT 0"),
                ("existing_tool_failure", "INTEGER DEFAULT 0"),
                ("solution_seeking_indicator", "INTEGER DEFAULT 0"),
                ("explicit_wtp_indicator", "INTEGER DEFAULT 0"),
                ("potential_wtp", "TEXT DEFAULT 'LOW'"),
                ("pain_score", "INTEGER DEFAULT 0"),
                ("evidence_quotes", "TEXT DEFAULT '[]'"),
                ("personal_case_indicator", "INTEGER DEFAULT 0"),
                ("is_advertisement", "INTEGER DEFAULT 0"),
            ]:
                await Database._add_column(db, "pains", column_name, column_type)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS verified_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    core_problem TEXT,
                    category TEXT,
                    weight INTEGER DEFAULT 0,
                    wtp_score TEXT,
                    evidence_quotes TEXT,
                    business_impact TEXT,
                    quality_grade TEXT DEFAULT 'Silver',
                    source_count INTEGER DEFAULT 0,
                    noise_score REAL DEFAULT 0,
                    source_chats TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for column_name, column_type in [
                ("quality_grade", "TEXT DEFAULT 'Silver'"),
                ("source_count", "INTEGER DEFAULT 0"),
                ("noise_score", "REAL DEFAULT 0"),
                ("source_chats", "TEXT"),
                ("description", "TEXT"),
                ("icp", "TEXT"),
                ("unique_authors", "INTEGER DEFAULT 0"),
                ("unique_chats", "INTEGER DEFAULT 0"),
                ("total_messages", "INTEGER DEFAULT 0"),
                ("date_from", "TIMESTAMP"),
                ("date_to", "TIMESTAMP"),
                ("example_messages", "TEXT DEFAULT '[]'"),
                ("current_workaround", "TEXT"),
                ("frequency", "TEXT"),
                ("pain_score", "INTEGER DEFAULT 0"),
                ("reachability_score", "INTEGER DEFAULT 0"),
                ("confidence", "REAL DEFAULT 0"),
                ("why_business_pain", "TEXT"),
                ("why_not_business_pain", "TEXT"),
                ("manual_mvp", "TEXT"),
                ("manual_48h", "INTEGER DEFAULT 0"),
                ("price_hypothesis", "TEXT"),
                ("next_50_source", "TEXT"),
                ("next_experiment", "TEXT"),
                ("offer", "TEXT"),
                ("first_customer_experiment", "TEXT"),
                ("acquisition_source", "TEXT"),
                ("trend", "TEXT DEFAULT 'UNKNOWN'"),
                ("recent_signals", "INTEGER DEFAULT 0"),
                ("first_money_score", "INTEGER DEFAULT 0"),
                ("main_risk", "TEXT"),
                ("cluster_json", "TEXT"),
            ]:
                await Database._add_column(db, "verified_insights", column_name, column_type)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS trash_bin (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_pain_id INTEGER,
                    category TEXT,
                    problem_title TEXT,
                    reason TEXT,
                    raw_llm_dump TEXT,
                    trace_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pains_unique_source
                ON pains(chat_id, message_id, problem_title)
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pains_status ON pains(butcher_status, id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_target_chats_status ON target_chats(status, id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_target_chats_next_check ON target_chats(status, next_check_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, message_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pains_message_link ON pains(chat_id, message_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_verified_insights_quality ON verified_insights(quality_grade, wtp_score, weight)")

            for i, session_str in enumerate(getattr(Config, "SESSIONS", [])):
                session_name = f"account_{i + 1}"
                await db.execute("""
                    INSERT INTO accounts
                    (session_name, session_string, api_id, api_hash, device_model, system_version, app_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        session_string = excluded.session_string,
                        api_id = excluded.api_id,
                        api_hash = excluded.api_hash,
                        device_model = excluded.device_model,
                        system_version = excluded.system_version,
                        app_version = excluded.app_version
                """, (
                    session_name,
                    session_str,
                    Config.API_ID,
                    Config.API_HASH,
                    "Desktop",
                    "Windows 11",
                    "4.16.8 x64",
                ))

            await db.commit()
            if DB_LOGS:
                async with db.execute("SELECT COUNT(*) FROM accounts") as cursor:
                    accounts_count = (await cursor.fetchone())[0]
                async with db.execute("SELECT COUNT(*) FROM target_chats") as cursor:
                    chats_count = (await cursor.fetchone())[0]
                log_event(
                    "DB",
                    "SCHEMA_READY",
                    "Database schema initialized",
                    db_path=Config.DB_PATH,
                    accounts=accounts_count,
                    target_chats=chats_count,
                )

    @staticmethod
    async def _add_column(db, table_name, column_name, column_type):
        try:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        except Exception as exc:
            if "duplicate column name" in str(exc).lower():
                return
            log_event(
                "DB",
                "MIGRATION_ERROR",
                "Could not add schema column",
                table=table_name,
                column=column_name,
                error=exc,
            )
            raise

    @staticmethod
    async def get_available_account():
        current_ts = int(time.time())
        query = """
            SELECT session_name, session_string, api_id, api_hash,
                   device_model, system_version, app_version
            FROM accounts
            WHERE status != 'Banned' AND cooldown_until <= ?
            LIMIT 1
        """
        return await Database.fetchone(query, (current_ts,))

    @staticmethod
    async def update_account_cooldown(session_name: str, cooldown_until: int, status: str = "Cooldown"):
        await Database.execute(
            "UPDATE accounts SET cooldown_until = ?, status = ? WHERE session_name = ?",
            (cooldown_until, status, session_name),
        )

    @staticmethod
    async def get_chat_last_read_id(username: str) -> int:
        row = await Database.fetchone("SELECT last_read_msg_id FROM target_chats WHERE username = ?", (username,))
        return int(row[0] or 0) if row else 0

    @staticmethod
    async def update_chat_last_read_id(username: str, last_read_id: int):
        await Database.execute(
            "UPDATE target_chats SET last_read_msg_id = ? WHERE username = ?",
            (last_read_id, username),
        )

    @staticmethod
    async def schedule_chat_recheck(username: str, delay_seconds: int, new_messages: int, empty_increment: bool = False):
        next_check_at = int(time.time()) + int(delay_seconds)
        await Database.execute(
            """
            UPDATE target_chats
            SET next_check_at = ?,
                last_new_messages = ?,
                empty_reads = CASE WHEN ? THEN COALESCE(empty_reads, 0) + 1 ELSE 0 END
            WHERE username = ?
            """,
            (next_check_at, int(new_messages or 0), 1 if empty_increment else 0, username),
        )

    @staticmethod
    async def save_chat_messages(messages):
        if not messages:
            return 0
        started_at = time.time()
        rows = [
            (
                message.get("chat_id"),
                message.get("message_id"),
                message.get("chat_title"),
                message.get("chat_username"),
                message.get("message_link"),
                message.get("author_id"),
                message.get("author_username"),
                message.get("author_name"),
                message.get("text"),
                message.get("raw_type"),
                message.get("msg_at"),
                1 if message.get("has_text") else 0,
                1 if message.get("is_bot") else 0,
            )
            for message in messages
        ]
        async with aiosqlite.connect(Config.DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.executemany(
                """
                INSERT INTO chat_messages
                (chat_id, message_id, chat_title, chat_username, message_link,
                 author_id, author_username, author_name, text, raw_type, msg_at,
                 has_text, is_bot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    chat_title = excluded.chat_title,
                    chat_username = excluded.chat_username,
                    message_link = excluded.message_link,
                    author_id = excluded.author_id,
                    author_username = excluded.author_username,
                    author_name = excluded.author_name,
                    text = excluded.text,
                    raw_type = excluded.raw_type,
                    msg_at = excluded.msg_at,
                    captured_at = CURRENT_TIMESTAMP,
                    has_text = excluded.has_text,
                    is_bot = excluded.is_bot
                """,
                rows,
            )
            await db.commit()
        if DB_LOGS:
            log_event(
                "DB",
                "MESSAGES_SAVED",
                "Chat messages saved",
                rows=len(rows),
                duration_ms=int((time.time() - started_at) * 1000),
            )
        return len(rows)

    @staticmethod
    async def set_state(key: str, value: str):
        await Database.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    async def get_state(key: str):
        row = await Database.fetchone("SELECT value FROM system_state WHERE key = ?", (key,))
        return row[0] if row else None

    @staticmethod
    async def execute(query: str, params: tuple = ()):
        started_at = time.time()
        verb, table, summary = _query_summary(query)
        async with aiosqlite.connect(Config.DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA journal_mode=WAL;")
            cursor = await db.execute(query, params)
            last_id = cursor.lastrowid
            rowcount = cursor.rowcount
            await db.commit()
            if DB_LOGS:
                log_event(
                    "DB",
                    "EXECUTE",
                    "SQL write executed",
                    verb=verb,
                    table=table,
                    params=len(params or ()),
                    rowcount=rowcount,
                    last_id=last_id,
                    duration_ms=int((time.time() - started_at) * 1000),
                    query=summary,
                )
            return last_id

    @staticmethod
    async def fetchone(query: str, params: tuple = ()):
        started_at = time.time()
        verb, table, summary = _query_summary(query)
        async with aiosqlite.connect(Config.DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA journal_mode=WAL;")
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                if DB_LOGS:
                    log_event(
                        "DB",
                        "FETCHONE",
                        "SQL fetchone executed",
                        verb=verb,
                        table=table,
                        params=len(params or ()),
                        found=row is not None,
                        duration_ms=int((time.time() - started_at) * 1000),
                        query=summary,
                    )
                return row

    @staticmethod
    async def fetchall(query: str, params: tuple = ()):
        started_at = time.time()
        verb, table, summary = _query_summary(query)
        async with aiosqlite.connect(Config.DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("PRAGMA journal_mode=WAL;")
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                if DB_LOGS:
                    log_event(
                        "DB",
                        "FETCHALL",
                        "SQL fetchall executed",
                        verb=verb,
                        table=table,
                        params=len(params or ()),
                        rows=len(rows),
                        duration_ms=int((time.time() - started_at) * 1000),
                        query=summary,
                    )
                return rows
