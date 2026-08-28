import asyncio
import html
import json
import os
import random
import re
import time
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram import Client
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, PeerIdInvalid, UsernameInvalid
from pyrogram.handlers import MessageHandler

from ai_engine import AIEngine
from config import Config
from database import Database
from logging_utils import log_event
from networking import telegram_proxy, telegram_proxy_url
from lead_radar_rules import (
    ACTOR_SELLER,
    RADAR_LABELS,
    TEMPERATURE_LABELS,
    detect_radar_type,
    normalize_actor_type,
    normalize_radar_type,
    normalize_temperature,
    local_rejection_code,
    service_offer_rejection_reason,
    verifier_rejection_code,
    verifier_labels,
    verifier_passed as strict_verifier_passed,
)
from margin_radar_policy import (
    ROLE_PRIMARY,
    ROLE_RESERVE,
    TIER_COLD,
    TIER_HOT,
    TIER_QUARANTINE,
    TIER_WARM,
    chat_poll_delay_seconds,
    flood_decision,
    history_limit,
    history_page_decision,
    hourly_cycle_limit,
    jittered_timestamp,
    normalized_text_hash,
    request_interval_seconds,
    role_budget,
    schedule_delay_seconds,
)
from quality_rules import clip_text, noise_rejection_reason, normalize_text
from runtime_settings import speed_label


MODULE = "MARGIN"
PRIMARY_SESSION = "account_1"
RESERVE_SESSION = "account_2"

MARGIN_SPEED = speed_label()
HISTORY_LIMIT = history_limit(MARGIN_SPEED)
LIVE_CHAT_LIMIT = 15
RESERVE_CHAT_LIMIT = 10
VERIFY_MIN_CONFIDENCE = 0.80
NO_ACCOUNT_SLEEP = 60
IDLE_SLEEP = 60
ERROR_RECHECK_SECONDS = 12 * 3600
CHAT_LEASE_SECONDS = 5 * 60
RANK_REFRESH_SECONDS = 10 * 60
MEMBERSHIP_REFRESH_SECONDS = 6 * 3600

ai = None


def get_ai():
    global ai
    if ai is None:
        ai = AIEngine()
    return ai


def message_text(message):
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def telegram_message_link(username, chat_id, message_id):
    if not message_id:
        return None
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"
    if chat_id:
        clean_chat_id = str(chat_id).replace("-100", "")
        return f"https://t.me/c/{clean_chat_id}/{message_id}"
    return None


def author_display_name(author):
    if not author:
        return None
    parts = [getattr(author, "first_name", None), getattr(author, "last_name", None)]
    return " ".join(part for part in parts if part) or None


def parse_json(raw):
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception as exc:
        log_event(MODULE, "LLM_PARSE_ERROR", "Could not parse JSON response", error=exc, raw_preview=clip_text(raw, 700))
        return {}
    return data if isinstance(data, dict) else {}


def as_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


PAIN_KEYWORDS = {
    "маржа": ["марж", "прибыл", "комисс", "съедает", "в минус", "невыгод", "юнит", "unit", "себест", "закуп"],
    "FBS": [
        "fbs",
        "фбс",
        "fbo",
        "фбо",
        "остатк",
        "поставк",
        "отгруз",
        "склад",
        "заморож",
        "не в продаже",
        "продажи встали",
        "товар приняли",
        "поддержка молчит",
        "не добавляются заказы",
    ],
    "штраф": ["штраф", "списа", "удержа", "потеря", "компенсац", "возврат", "претензи", "не выплат", "утер"],
    "налоги": ["ндс", "аусн", "усн", "налог", "доход", "корректиров", "бухгалтер", "отчёт", "отчет", "выплат", "реализац"],
    "реклама": ["реклам", "дрр", "ставка", "продвиж", "конверси", "показы", "клики"],
    "логистика": ["логист", "доставк", "хранен", "приемк", "приёмк", "сортиров"],
}

ALLOWED_PAIN_TYPES = ["маржа", "комиссия", "логистика", "штраф", "FBS", "возвраты", "налоги", "реклама"]

TITLE_QUARANTINE_SIGNALS = [
    "dev",
    "api",
    "разработ",
    "ваканс",
    "работа",
    "замены",
    "резюме",
    "пвз чат",
    "отзывы",
    "кэшбэк",
    "добросовестные продавцы",
    "покупаем выгодно",
    "шопоголик",
    "новости",
]

HARD_REJECT_PATTERNS = {
    "vacancy": [
        r"\bваканс",
        r"\bвыйду на замен",
        r"\bищ[уе]м\s+(?:сотрудник|менеджер|человек|работник)",
        r"\bставка\s+(?:от\s*)?\d",
        r"\bграфик\s+\d",
        r"\bоплата\s+(?:за\s+)?смен",
    ],
    "promo": [
        r"\b(?:оказываем|предлагаю|предлагаем)\s+услуг",
        r"\b(?:помогу|сделаю|настрою|веду)\b.{0,100}\b(?:лк|кабинет|реклам|карточ|маркетплейс)",
        r"\b(?:пишите|напишите|жду)\s+(?:мне\s+)?(?:в\s+)?(?:лс|личк|директ)",
        r"\b(?:курс|вебинар|марафон|наставничество|аудит вашей бухгалтерии)\b",
        r"\bпортфолио\b",
    ],
    "cashback_review": [
        r"\bкэшб[эе]к\b",
        r"\bклассн(?:ый|ая)\s+менеджер",
        r"\bотличн(?:ый|ая)\s+менеджер",
        r"\bоднозначно рекомендую\b",
        r"\bвыплатил[аи]?\b.{0,80}\b(?:кэшб|за\s+\w+)",
    ],
    "news": [
        r"^\s*[🔥🟣🏪📢].{0,80}\b(?:селлеры|wildberries|ozon|маркетплейс)",
        r"\bсоюз электронной торговли\b",
        r"\bобратил(?:ся|ась)\s+в\s+фас\b",
        r"\bвед[её]т переговоры\b",
        r"\bсообщает\b.{0,40}\b(?:сми|рбк|коммерсант)",
        r"\#[a-zа-я]",
    ],
    "non_marketplace_tech": [
        r"\bxeon\b",
        r"\b(?:bios|биос|socket|сокет|pinmod|пинмод)\b",
        r"\b(?:python|golang|javascript|api endpoint|postman)\b",
    ],
}

FIRST_PERSON_SIGNALS = [
    " у меня ",
    " мне ",
    " мой ",
    " моя ",
    " мои ",
    " мы ",
    " нам ",
    " нашу ",
    " не могу ",
    " не понимаю ",
    " столкнул",
    " списали ",
    " удержали ",
    " потерял",
    " продаю ",
    " работаю ",
    " создал",
    " отправил",
    " поставил",
]

HELP_SIGNALS = [
    "?",
    "подскаж",
    "что делать",
    "как решить",
    "как оспор",
    "кто сталкивался",
    "помогите",
    "нужен совет",
    "не понимаю",
    "не могу",
]

ADVICE_PREFIXES = [
    "да,",
    "если ",
    "похоже",
    "официальной даты",
    "я бы ",
    "советую ",
    "для начала ",
    "тут зависит",
    "обычно ",
    "лучше ",
]


def detect_pain_types(text):
    clean = normalize_text(text)
    return [
        pain_type
        for pain_type, keywords in PAIN_KEYWORDS.items()
        if any(normalize_text(keyword) in clean for keyword in keywords)
    ]


def title_quarantine_reason(title):
    clean = normalize_text(title)
    for signal in TITLE_QUARANTINE_SIGNALS:
        if signal in clean:
            return f"title signal: {signal}"
    return None


def chat_quarantine_reason(title, rejection_reason=""):
    reason = title_quarantine_reason(title)
    if reason:
        return reason
    clean = normalize_text(f"{title or ''} {rejection_reason or ''}")
    quarantine_signals = [
        "vacancy",
        "job",
        "service offer",
        "promo",
        "advert",
        "noise",
        "ваканс",
        "работ",
        "реклам",
        "услуг",
        "оффер",
        "шум",
        "кэшбэк",
        "отзыв",
        "новост",
        "dev",
        "api",
    ]
    for signal in quarantine_signals:
        if signal in clean:
            return f"chat signal: {signal}"
    return None


def hard_rejection_reason(text, chat_title=""):
    clean = f" {normalize_text(text)} "
    title_reason = title_quarantine_reason(chat_title)
    if title_reason:
        return title_reason
    if len(clean.split()) < 5:
        return "too short"
    if not detect_pain_types(clean):
        return "no margin-leak keyword"

    service_reason = service_offer_rejection_reason(text)
    if service_reason:
        return service_reason

    for label, patterns in HARD_REJECT_PATTERNS.items():
        if any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in patterns):
            return label

    noise_reason = noise_rejection_reason(text, chat_title)
    if noise_reason:
        return noise_reason

    has_personal = any(signal in clean for signal in FIRST_PERSON_SIGNALS)
    seeks_help = any(signal in clean for signal in HELP_SIGNALS)
    if any(clean.strip().startswith(prefix) for prefix in ADVICE_PREFIXES) and not (has_personal and seeks_help):
        return "expert/advice reply"
    return None


def normalize_pain_type(value, fallback_types=None):
    clean = normalize_text(value)
    for pain_type in ALLOWED_PAIN_TYPES:
        if normalize_text(pain_type) in clean:
            return pain_type
    return (fallback_types or ["маржа"])[0]


def lead_labels(lead, candidate):
    score = max(0, min(7, as_int(lead.get("score"), 0)))
    actor_type = normalize_actor_type(lead.get("actor_type"), lead.get("is_seller"))
    radar_type = normalize_radar_type(
        lead.get("radar_type"),
        candidate.get("text", ""),
        candidate.get("pain_types"),
    )
    temperature = normalize_temperature(
        lead.get("temperature"),
        candidate.get("text", ""),
        score,
    )
    return actor_type, radar_type, temperature


def percentile_ranks(values):
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    denominator = max(1, len(values) - 1)
    for position, index in enumerate(order):
        ranks[index] = position / denominator
    return ranks


def message_to_candidate(message, fallback_chat=None):
    chat = getattr(message, "chat", None)
    author = getattr(message, "from_user", None)
    text = message_text(message)
    username = getattr(chat, "username", None) or (fallback_chat or {}).get("username")
    chat_id = (fallback_chat or {}).get("chat_id") or getattr(chat, "id", None)
    chat_title = getattr(chat, "title", None) or (fallback_chat or {}).get("title") or username or str(chat_id)
    message_date = None
    if getattr(message, "date", None):
        dt = message.date.replace(tzinfo=timezone.utc) if message.date.tzinfo is None else message.date
        message_date = dt.isoformat()
    return {
        "chat_id": chat_id,
        "message_id": message.id,
        "chat_title": chat_title,
        "chat_username": username,
        "message_link": telegram_message_link(username, chat_id, message.id),
        "author_id": getattr(author, "id", None),
        "author_username": getattr(author, "username", None),
        "author_name": author_display_name(author),
        "msg_at": message_date,
        "text": text,
        "pain_types": detect_pain_types(text),
        "text_hash": normalized_text_hash(text),
    }


class ProtectionStore:
    def __init__(self, db_path):
        self.db_path = db_path

    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=30000;")
        await db.execute("PRAGMA journal_mode=WAL;")
        return db

    async def _add_column(self, db, table, column, definition):
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except Exception as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    async def init_schema(self):
        db = await self._connect()
        try:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_radar_state (
                    chat_id INTEGER PRIMARY KEY,
                    last_seen_msg_id INTEGER DEFAULT 0,
                    history_offset_id INTEGER DEFAULT 0,
                    history_high_watermark INTEGER DEFAULT 0,
                    last_checked_at TIMESTAMP,
                    next_check_at INTEGER DEFAULT 0,
                    last_new_messages INTEGER DEFAULT 0,
                    last_error TEXT,
                    tier TEXT DEFAULT 'Cold',
                    priority_score REAL DEFAULT 0,
                    noise_hits INTEGER DEFAULT 0,
                    reserve_priority INTEGER DEFAULT 0,
                    leased_by TEXT,
                    lease_until INTEGER DEFAULT 0,
                    live_member INTEGER DEFAULT 0,
                    hot_until INTEGER DEFAULT 0,
                    manual_boost INTEGER DEFAULT 0,
                    manual_boost_until INTEGER DEFAULT 0,
                    messages_7d INTEGER DEFAULT 0,
                    candidates_7d INTEGER DEFAULT 0,
                    strict_7d INTEGER DEFAULT 0,
                    strict_yield_1000 REAL DEFAULT 0,
                    last_strict_at INTEGER DEFAULT 0
                )
                """
            )
            for column, definition in [
                ("tier", "TEXT DEFAULT 'Cold'"),
                ("priority_score", "REAL DEFAULT 0"),
                ("noise_hits", "INTEGER DEFAULT 0"),
                ("reserve_priority", "INTEGER DEFAULT 0"),
                ("history_offset_id", "INTEGER DEFAULT 0"),
                ("history_high_watermark", "INTEGER DEFAULT 0"),
                ("leased_by", "TEXT"),
                ("lease_until", "INTEGER DEFAULT 0"),
                ("live_member", "INTEGER DEFAULT 0"),
                ("hot_until", "INTEGER DEFAULT 0"),
                ("manual_boost", "INTEGER DEFAULT 0"),
                ("manual_boost_until", "INTEGER DEFAULT 0"),
                ("messages_7d", "INTEGER DEFAULT 0"),
                ("candidates_7d", "INTEGER DEFAULT 0"),
                ("strict_7d", "INTEGER DEFAULT 0"),
                ("strict_yield_1000", "REAL DEFAULT 0"),
                ("last_strict_at", "INTEGER DEFAULT 0"),
            ]:
                await self._add_column(db, "margin_radar_state", column, definition)

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_account_state (
                    session_name TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    blocked_until INTEGER DEFAULT 0,
                    last_flood_at INTEGER DEFAULT 0,
                    next_request_at INTEGER DEFAULT 0,
                    cycle_started_at INTEGER DEFAULT 0,
                    cycle_limit INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for column, definition in [
                ("cycle_started_at", "INTEGER DEFAULT 0"),
                ("cycle_limit", "INTEGER DEFAULT 0"),
            ]:
                await self._add_column(db, "margin_account_state", column, definition)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_name TEXT NOT NULL,
                    request_type TEXT NOT NULL,
                    requested_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_flood_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_name TEXT NOT NULL,
                    wait_seconds INTEGER NOT NULL,
                    happened_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT,
                    chat_username TEXT,
                    message_link TEXT,
                    author_id INTEGER,
                    author_username TEXT,
                    author_name TEXT,
                    msg_at TIMESTAMP,
                    text TEXT,
                    pain_type TEXT,
                    actor_type TEXT DEFAULT 'unknown',
                    radar_type TEXT DEFAULT 'margin_leak',
                    temperature TEXT DEFAULT 'yellow',
                    filter_score INTEGER DEFAULT 0,
                    rejection_reason TEXT,
                    rejection_detail TEXT,
                    strict_at TIMESTAMP,
                    signal_strength INTEGER DEFAULT 0,
                    score INTEGER DEFAULT 0,
                    why_client TEXT,
                    risks TEXT,
                    questions TEXT,
                    soft_reply TEXT,
                    mini_audit TEXT,
                    raw_llm_json TEXT,
                    notified INTEGER DEFAULT 0,
                    notified_at TIMESTAMP,
                    text_hash TEXT,
                    verifier_json TEXT,
                    verifier_passed INTEGER DEFAULT 0,
                    feedback TEXT,
                    feedback_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
                """
            )
            for column, definition in [
                ("text_hash", "TEXT"),
                ("verifier_json", "TEXT"),
                ("verifier_passed", "INTEGER DEFAULT 0"),
                ("feedback", "TEXT"),
                ("feedback_at", "TIMESTAMP"),
                ("actor_type", "TEXT DEFAULT 'unknown'"),
                ("radar_type", "TEXT DEFAULT 'margin_leak'"),
                ("temperature", "TEXT DEFAULT 'yellow'"),
                ("filter_score", "INTEGER DEFAULT 0"),
                ("rejection_reason", "TEXT"),
                ("rejection_detail", "TEXT"),
                ("strict_at", "TIMESTAMP"),
            ]:
                await self._add_column(db, "margin_leads", column, definition)

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_screening_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_title TEXT,
                    chat_username TEXT,
                    message_link TEXT,
                    author_id INTEGER,
                    author_username TEXT,
                    author_name TEXT,
                    msg_at TIMESTAMP,
                    text TEXT,
                    text_hash TEXT,
                    source TEXT,
                    outcome TEXT NOT NULL DEFAULT 'screened',
                    rejection_reason TEXT,
                    rejection_detail TEXT,
                    lead_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_chat_membership (
                    chat_id INTEGER NOT NULL,
                    session_name TEXT NOT NULL,
                    is_member INTEGER DEFAULT 0,
                    checked_at INTEGER DEFAULT 0,
                    PRIMARY KEY(chat_id, session_name)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_notification_claims (
                    text_hash TEXT PRIMARY KEY,
                    lead_id INTEGER NOT NULL,
                    claimed_at INTEGER NOT NULL
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS margin_live_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
                """
            )

            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_state_due ON margin_radar_state(tier, next_check_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_request_time ON margin_request_log(session_name, requested_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_flood_time ON margin_flood_log(happened_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_lead_hash ON margin_leads(text_hash, notified_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_live_queue_id ON margin_live_queue(id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_screening_time ON margin_screening_events(created_at, outcome)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_screening_reason ON margin_screening_events(rejection_reason, created_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_margin_state_lease ON margin_radar_state(lease_until, next_check_at)")

            await db.execute(
                """
                INSERT OR IGNORE INTO margin_screening_events
                    (chat_id, message_id, chat_title, chat_username, message_link,
                     author_id, author_username, author_name, msg_at, text, text_hash,
                     source, outcome, rejection_reason, rejection_detail, lead_id,
                     created_at, updated_at)
                SELECT chat_id, message_id, chat_title, chat_username, message_link,
                       author_id, author_username, author_name, msg_at, text, text_hash,
                       'migration',
                       CASE
                           WHEN notified = 1 THEN 'notified'
                           WHEN verifier_passed = 1 AND filter_score >= 9 THEN 'strict'
                           ELSE 'candidate'
                       END,
                       rejection_reason, rejection_detail, id, created_at, '2000-01-01 00:00:00'
                FROM margin_leads
                """
            )

            async with db.execute(
                "SELECT session_name, cooldown_until FROM accounts ORDER BY session_name"
            ) as cursor:
                accounts = await cursor.fetchall()
            for row in accounts:
                role = ROLE_PRIMARY if row["session_name"] == PRIMARY_SESSION else ROLE_RESERVE
                await db.execute(
                    """
                    INSERT INTO margin_account_state
                        (session_name, role, blocked_until)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        role = excluded.role,
                        blocked_until = MAX(margin_account_state.blocked_until, excluded.blocked_until),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (row["session_name"], role, int(row["cooldown_until"] or 0)),
                )
            await db.commit()
        finally:
            await db.close()

    async def get_runtime_int(self, key, default=0):
        db = await self._connect()
        try:
            async with db.execute("SELECT value FROM margin_runtime_state WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
            return as_int(row["value"], default) if row else default
        finally:
            await db.close()

    async def set_runtime(self, key, value):
        db = await self._connect()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO margin_runtime_state (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            await db.commit()
        finally:
            await db.close()

    async def ensure_rollout_started(self, now):
        started = await self.get_runtime_int("rollout_started_at", 0)
        if started <= 0:
            await self.set_runtime("rollout_started_at", now)
            return now
        return started

    async def save_screening_event(
        self,
        candidate,
        *,
        source,
        outcome="screened",
        rejection_reason=None,
        rejection_detail=None,
        lead_id=None,
    ):
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO margin_screening_events
                    (chat_id, message_id, chat_title, chat_username, message_link,
                     author_id, author_username, author_name, msg_at, text, text_hash,
                     source, outcome, rejection_reason, rejection_detail, lead_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    source = excluded.source,
                    outcome = CASE
                        WHEN margin_screening_events.outcome = 'notified' THEN 'notified'
                        WHEN margin_screening_events.outcome = 'strict'
                             AND excluded.outcome IN ('screened', 'candidate') THEN 'strict'
                        WHEN margin_screening_events.outcome = 'candidate'
                             AND excluded.outcome = 'screened' THEN 'candidate'
                        ELSE excluded.outcome
                    END,
                    rejection_reason = CASE
                        WHEN margin_screening_events.outcome IN ('strict', 'notified')
                             AND excluded.outcome IN ('screened', 'candidate')
                        THEN margin_screening_events.rejection_reason
                        ELSE excluded.rejection_reason
                    END,
                    rejection_detail = CASE
                        WHEN margin_screening_events.outcome IN ('strict', 'notified')
                             AND excluded.outcome IN ('screened', 'candidate')
                        THEN margin_screening_events.rejection_detail
                        ELSE excluded.rejection_detail
                    END,
                    lead_id = COALESCE(excluded.lead_id, margin_screening_events.lead_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    candidate.get("chat_id"),
                    candidate.get("message_id"),
                    candidate.get("chat_title"),
                    candidate.get("chat_username"),
                    candidate.get("message_link"),
                    candidate.get("author_id"),
                    candidate.get("author_username"),
                    candidate.get("author_name"),
                    candidate.get("msg_at"),
                    candidate.get("text"),
                    candidate.get("text_hash"),
                    source,
                    outcome,
                    rejection_reason,
                    clip_text(rejection_detail, 700) if rejection_detail else None,
                    lead_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def save_screening_events(self, events):
        if not events:
            return
        db = await self._connect()
        try:
            rows = []
            for event in events:
                candidate = event["candidate"]
                rows.append(
                    (
                        candidate.get("chat_id"),
                        candidate.get("message_id"),
                        candidate.get("chat_title"),
                        candidate.get("chat_username"),
                        candidate.get("message_link"),
                        candidate.get("author_id"),
                        candidate.get("author_username"),
                        candidate.get("author_name"),
                        candidate.get("msg_at"),
                        candidate.get("text"),
                        candidate.get("text_hash"),
                        event.get("source"),
                        event.get("outcome", "screened"),
                        event.get("rejection_reason"),
                        clip_text(event.get("rejection_detail"), 700),
                        event.get("lead_id"),
                    )
                )
            await db.executemany(
                """
                INSERT INTO margin_screening_events
                    (chat_id, message_id, chat_title, chat_username, message_link,
                     author_id, author_username, author_name, msg_at, text, text_hash,
                     source, outcome, rejection_reason, rejection_detail, lead_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    source = excluded.source,
                    outcome = CASE
                        WHEN margin_screening_events.outcome = 'notified' THEN 'notified'
                        WHEN margin_screening_events.outcome = 'strict'
                             AND excluded.outcome IN ('screened', 'candidate') THEN 'strict'
                        WHEN margin_screening_events.outcome = 'candidate'
                             AND excluded.outcome = 'screened' THEN 'candidate'
                        ELSE excluded.outcome
                    END,
                    rejection_reason = CASE
                        WHEN margin_screening_events.outcome IN ('strict', 'notified')
                             AND excluded.outcome IN ('screened', 'candidate')
                        THEN margin_screening_events.rejection_reason
                        ELSE excluded.rejection_reason
                    END,
                    rejection_detail = CASE
                        WHEN margin_screening_events.outcome IN ('strict', 'notified')
                             AND excluded.outcome IN ('screened', 'candidate')
                        THEN margin_screening_events.rejection_detail
                        ELSE excluded.rejection_detail
                    END,
                    lead_id = COALESCE(excluded.lead_id, margin_screening_events.lead_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
            await db.commit()
        finally:
            await db.close()

    async def update_screening_outcome(
        self,
        chat_id,
        message_id,
        outcome,
        *,
        rejection_reason=None,
        rejection_detail=None,
        lead_id=None,
    ):
        db = await self._connect()
        try:
            await db.execute(
                """
                UPDATE margin_screening_events
                SET outcome = ?,
                    rejection_reason = ?,
                    rejection_detail = ?,
                    lead_id = COALESCE(?, lead_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND message_id = ?
                """,
                (
                    outcome,
                    rejection_reason,
                    clip_text(rejection_detail, 700) if rejection_detail else None,
                    lead_id,
                    chat_id,
                    message_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def funnel_stats(self, seconds):
        modifier = f"-{max(1, int(seconds))} seconds"
        db = await self._connect()
        try:
            result = {}
            for outcome in ("screened", "candidate", "strict", "notified"):
                async with db.execute(
                    """
                    SELECT COUNT(*) FROM margin_screening_events
                    WHERE outcome = ? AND updated_at >= datetime('now', ?)
                    """,
                    (outcome, modifier),
                ) as cursor:
                    result[outcome] = int((await cursor.fetchone())[0])
            async with db.execute(
                """
                SELECT rejection_reason, COUNT(*) AS total
                FROM margin_screening_events
                WHERE rejection_reason IS NOT NULL
                  AND updated_at >= datetime('now', ?)
                GROUP BY rejection_reason
                ORDER BY total DESC
                """,
                (modifier,),
            ) as cursor:
                result["reasons"] = {
                    row["rejection_reason"]: int(row["total"])
                    for row in await cursor.fetchall()
                }
            return result
        finally:
            await db.close()

    async def assign_chat_tiers(self, now):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT t.chat_id, t.username, t.title, t.status, t.rejection_reason,
                       COALESCE(t.quality_score, 0) AS quality_score,
                       COALESCE(s.manual_boost, 0) AS manual_boost,
                       COALESCE(s.manual_boost_until, 0) AS manual_boost_until,
                       COALESCE(s.next_check_at, 0) AS current_due,
                       COALESCE(s.live_member, 0) AS live_member,
                       COALESCE((
                           SELECT COUNT(*) FROM chat_messages m
                           WHERE m.chat_id = t.chat_id
                             AND m.captured_at >= datetime('now', '-7 day')
                       ), 0) AS messages_7d,
                       COALESCE((
                           SELECT COUNT(*) FROM margin_leads l
                           WHERE l.chat_id = t.chat_id
                             AND l.created_at >= datetime('now', '-7 day')
                       ), 0) AS candidates_7d,
                       COALESCE((
                           SELECT COUNT(*) FROM margin_leads l
                           WHERE l.chat_id = t.chat_id
                             AND l.verifier_passed = 1
                             AND l.filter_score >= 9
                             AND l.created_at >= datetime('now', '-7 day')
                       ), 0) AS strict_7d,
                       COALESCE((
                           SELECT COUNT(*) FROM margin_leads l
                           WHERE l.chat_id = t.chat_id
                             AND l.verifier_passed = 1
                             AND l.filter_score >= 9
                             AND l.temperature = 'red'
                             AND l.created_at >= datetime('now', '-7 day')
                       ), 0) AS red_7d,
                       COALESCE((
                           SELECT MAX(CAST(strftime('%s', l.created_at) AS INTEGER))
                           FROM margin_leads l
                           WHERE l.chat_id = t.chat_id
                             AND l.verifier_passed = 1
                             AND l.filter_score >= 9
                       ), 0) AS last_strict_at,
                       COALESCE((
                           SELECT MIN(strict_time) FROM (
                               SELECT CAST(strftime('%s', l.created_at) AS INTEGER) AS strict_time
                               FROM margin_leads l
                               WHERE l.chat_id = t.chat_id
                                 AND l.verifier_passed = 1
                                 AND l.filter_score >= 9
                                 AND l.created_at >= datetime('now', '-7 day')
                               ORDER BY l.created_at DESC
                               LIMIT 2
                           )
                       ), 0) AS second_recent_strict_at
                FROM target_chats t
                LEFT JOIN margin_radar_state s ON s.chat_id = t.chat_id
                WHERE t.username IS NOT NULL AND t.username != ''
                """
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

            total_messages = sum(int(row["messages_7d"] or 0) for row in rows)
            total_strict = sum(int(row["strict_7d"] or 0) for row in rows)
            global_rate = total_strict / max(1, total_messages)
            yields = [
                (
                    (int(row["strict_7d"] or 0) + global_rate * 200)
                    / (int(row["messages_7d"] or 0) + 200)
                    * 1000
                )
                for row in rows
            ]
            yield_percentiles = percentile_ranks(yields)

            ranked = []
            for index, row in enumerate(rows):
                strict_7d = int(row["strict_7d"] or 0)
                red_7d = int(row["red_7d"] or 0)
                last_strict_at = int(row["last_strict_at"] or 0)
                second_recent = int(row["second_recent_strict_at"] or 0)
                manual_until = int(row["manual_boost_until"] or 0)
                manual_boost = int(row["manual_boost"] or 0) if manual_until > now else 0
                recency = max(0.0, 1.0 - ((now - last_strict_at) / (7 * 86400))) if last_strict_at else 0.0
                paid_share = red_7d / max(1, strict_7d)
                priority = (
                    55 * yield_percentiles[index]
                    + 25 * recency
                    + 15 * paid_share
                    + min(20, max(0, manual_boost))
                )

                hot_until = 0
                if last_strict_at:
                    hot_until = last_strict_at + 72 * 3600
                if strict_7d >= 2 and second_recent:
                    hot_until = max(hot_until, second_recent + 7 * 86400)
                hot_until = max(hot_until, manual_until)

                if chat_quarantine_reason(row["title"] or "", row["rejection_reason"] or ""):
                    tier = TIER_QUARANTINE
                elif hot_until > now:
                    tier = TIER_HOT
                elif row["status"] == "Processed":
                    tier = TIER_WARM
                else:
                    tier = TIER_COLD

                current_due = int(row["current_due"] or 0)
                if current_due <= now:
                    delay = chat_poll_delay_seconds(tier, bool(row["live_member"]), False)
                    next_check = jittered_timestamp(now, delay)
                else:
                    next_check = current_due
                await db.execute(
                    """
                    INSERT INTO margin_radar_state
                        (chat_id, tier, priority_score, reserve_priority, next_check_at,
                         hot_until, manual_boost, manual_boost_until, messages_7d,
                         candidates_7d, strict_7d, strict_yield_1000, last_strict_at)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        tier = excluded.tier,
                        priority_score = excluded.priority_score,
                        reserve_priority = 0,
                        hot_until = excluded.hot_until,
                        manual_boost = excluded.manual_boost,
                        manual_boost_until = excluded.manual_boost_until,
                        messages_7d = excluded.messages_7d,
                        candidates_7d = excluded.candidates_7d,
                        strict_7d = excluded.strict_7d,
                        strict_yield_1000 = excluded.strict_yield_1000,
                        last_strict_at = excluded.last_strict_at,
                        next_check_at = CASE
                            WHEN margin_radar_state.tier != excluded.tier THEN excluded.next_check_at
                            ELSE margin_radar_state.next_check_at
                        END
                    """,
                    (
                        row["chat_id"],
                        tier,
                        priority,
                        next_check,
                        hot_until,
                        manual_boost,
                        manual_until,
                        int(row["messages_7d"] or 0),
                        int(row["candidates_7d"] or 0),
                        strict_7d,
                        yields[index],
                        last_strict_at,
                    ),
                )
                ranked.append({**row, "tier": tier, "priority_score": priority, "strict_yield_1000": yields[index]})
            await db.commit()
            ranked.sort(key=lambda row: (row["priority_score"], row["chat_id"]), reverse=True)
            return ranked[:LIVE_CHAT_LIMIT]
        finally:
            await db.close()

    async def lease_due_chat(self, session_name, now):
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT t.chat_id, t.username, t.title, t.status,
                       COALESCE(t.last_read_msg_id, 0) AS target_last_read,
                       COALESCE(s.last_seen_msg_id, 0) AS radar_last_seen,
                       COALESCE(s.history_offset_id, 0) AS history_offset_id,
                       COALESCE(s.history_high_watermark, 0) AS history_high_watermark,
                       s.tier, s.priority_score, s.live_member
                FROM target_chats t
                JOIN margin_radar_state s ON s.chat_id = t.chat_id
                WHERE s.next_check_at <= ?
                  AND COALESCE(s.lease_until, 0) <= ?
                  AND t.username IS NOT NULL
                  AND t.username != ''
                ORDER BY
                    CASE s.tier
                        WHEN 'Hot' THEN 0
                        WHEN 'Warm' THEN 1
                        WHEN 'Cold' THEN 2
                        ELSE 3
                    END,
                    s.priority_score DESC,
                    s.next_check_at ASC,
                    RANDOM()
                LIMIT 1
                """,
                (now, now),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await db.commit()
                return None
            await db.execute(
                """
                UPDATE margin_radar_state
                SET leased_by = ?, lease_until = ?
                WHERE chat_id = ? AND COALESCE(lease_until, 0) <= ?
                """,
                (session_name, now + CHAT_LEASE_SECONDS, row["chat_id"], now),
            )
            await db.commit()
            return dict(row)
        finally:
            await db.close()

    async def schedule_chat(
        self,
        chat_id,
        tier,
        last_seen,
        new_messages,
        error=None,
        history_offset_id=0,
        history_high_watermark=0,
        continue_backfill=False,
    ):
        now = int(time.time())
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT live_member FROM margin_radar_state WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                state = await cursor.fetchone()
            live_member = bool(state["live_member"]) if state else False
            if continue_backfill:
                next_check = now
            else:
                delay = (
                    ERROR_RECHECK_SECONDS
                    if error
                    else chat_poll_delay_seconds(tier, live_member, new_messages > 0)
                )
                next_check = jittered_timestamp(now, delay)
            await db.execute(
                """
                UPDATE margin_radar_state
                SET last_seen_msg_id = CASE
                        WHEN ? IS NULL THEN last_seen_msg_id
                        ELSE MAX(COALESCE(last_seen_msg_id, 0), ?)
                    END,
                    history_offset_id = ?,
                    history_high_watermark = ?,
                    last_checked_at = CURRENT_TIMESTAMP,
                    next_check_at = ?,
                    last_new_messages = ?,
                    last_error = ?,
                    leased_by = NULL,
                    lease_until = 0
                WHERE chat_id = ?
                """,
                (
                    last_seen,
                    int(last_seen or 0),
                    int(history_offset_id or 0),
                    int(history_high_watermark or 0),
                    next_check,
                    int(new_messages or 0),
                    error,
                    chat_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def update_live_cursor(self, chat_id, message_id):
        db = await self._connect()
        try:
            await db.execute(
                """
                UPDATE margin_radar_state
                SET last_seen_msg_id = MAX(COALESCE(last_seen_msg_id, 0), ?),
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (int(message_id or 0), chat_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def enqueue_live_candidate(self, candidate):
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO margin_live_queue
                    (chat_id, message_id, payload_json)
                VALUES (?, ?, ?)
                """,
                (
                    candidate["chat_id"],
                    candidate["message_id"],
                    json.dumps(candidate, ensure_ascii=False),
                ),
            )
            await db.execute(
                """
                UPDATE margin_radar_state
                SET last_seen_msg_id = MAX(COALESCE(last_seen_msg_id, 0), ?),
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (int(candidate["message_id"]), candidate["chat_id"]),
            )
            await db.commit()
        finally:
            await db.close()

    async def next_live_candidate(self):
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT id, payload_json, attempts FROM margin_live_queue ORDER BY id LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            candidate = json.loads(row["payload_json"])
            return {
                "queue_id": int(row["id"]),
                "attempts": int(row["attempts"] or 0),
                "candidate": candidate,
            }
        finally:
            await db.close()

    async def complete_live_candidate(self, queue_id):
        db = await self._connect()
        try:
            await db.execute("DELETE FROM margin_live_queue WHERE id = ?", (queue_id,))
            await db.commit()
        finally:
            await db.close()

    async def fail_live_candidate(self, queue_id, error):
        db = await self._connect()
        try:
            await db.execute(
                """
                UPDATE margin_live_queue
                SET attempts = attempts + 1,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clip_text(str(error), 700), queue_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def account(self, session_name):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT a.session_name, a.session_string, a.api_id, a.api_hash,
                       a.device_model, a.system_version, a.app_version,
                       a.status, a.cooldown_until,
                       s.role, s.blocked_until, s.last_flood_at,
                       s.next_request_at, s.cycle_started_at, s.cycle_limit
                FROM accounts a
                JOIN margin_account_state s ON s.session_name = a.session_name
                WHERE a.session_name = ?
                """,
                (session_name,),
            ) as cursor:
                row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await db.close()

    async def ensure_account_cycle(self, session_name, now):
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT cycle_started_at, cycle_limit FROM margin_account_state WHERE session_name = ?",
                (session_name,),
            ) as cursor:
                row = await cursor.fetchone()
            started = int(row["cycle_started_at"] or 0) if row else 0
            limit = int(row["cycle_limit"] or 0) if row else 0
            if started <= 0 or now - started >= 3600 or not 15 <= limit <= 21:
                started = now + random.randint(0, 45)
                limit = hourly_cycle_limit()
                await db.execute(
                    """
                    UPDATE margin_account_state
                    SET cycle_started_at = ?, cycle_limit = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE session_name = ?
                    """,
                    (started, limit, session_name),
                )
                await db.commit()
            return started, limit
        finally:
            await db.close()

    async def cycle_request_count(self, session_name, cycle_started_at):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT COUNT(*) FROM margin_request_log
                WHERE session_name = ? AND requested_at >= ?
                """,
                (session_name, cycle_started_at),
            ) as cursor:
                return int((await cursor.fetchone())[0])
        finally:
            await db.close()

    async def mark_client_status(self, session_name, status):
        db = await self._connect()
        try:
            await db.execute("UPDATE accounts SET status = ? WHERE session_name = ?", (status, session_name))
            await db.commit()
        finally:
            await db.close()

    async def request_counts(self, session_name, now):
        db = await self._connect()
        try:
            hourly = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM margin_request_log WHERE session_name = ? AND requested_at > ?",
                        (session_name, now - 3600),
                    )
                ).fetchone()
            )[0]
            daily = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM margin_request_log WHERE session_name = ? AND requested_at > ?",
                        (session_name, now - 86400),
                    )
                ).fetchone()
            )[0]
            return int(hourly), int(daily)
        finally:
            await db.close()

    async def record_request(self, session_name, role, now):
        next_request_at = now + request_interval_seconds(role, MARGIN_SPEED)
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO margin_request_log (session_name, request_type, requested_at) VALUES (?, 'history', ?)",
                (session_name, now),
            )
            await db.execute(
                "UPDATE margin_account_state SET next_request_at = ?, updated_at = CURRENT_TIMESTAMP WHERE session_name = ?",
                (next_request_at, session_name),
            )
            await db.execute("DELETE FROM margin_request_log WHERE requested_at < ?", (now - 14 * 86400,))
            await db.commit()
            return next_request_at
        finally:
            await db.close()

    async def budget_wait_seconds(self, session_name, hourly_limit, daily_limit, now):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT requested_at
                FROM margin_request_log
                WHERE session_name = ? AND requested_at > ?
                ORDER BY requested_at
                """,
                (session_name, now - 86400),
            ) as cursor:
                timestamps = [int(row[0]) for row in await cursor.fetchall()]
        finally:
            await db.close()

        waits = [30]
        hourly = [timestamp for timestamp in timestamps if timestamp > now - 3600]
        if len(hourly) >= hourly_limit:
            release_index = len(hourly) - hourly_limit
            waits.append(hourly[release_index] + 3601 - now)
        if len(timestamps) >= daily_limit:
            release_index = len(timestamps) - daily_limit
            waits.append(timestamps[release_index] + 86401 - now)
        return max(30, max(waits))

    async def record_flood(self, session_name, wait_seconds, now):
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO margin_flood_log (session_name, wait_seconds, happened_at) VALUES (?, ?, ?)",
                (session_name, int(wait_seconds), now),
            )
            async with db.execute(
                """
                SELECT COUNT(*) AS total FROM margin_flood_log
                WHERE session_name = ? AND happened_at > ?
                """,
                (session_name, now - 86400),
            ) as cursor:
                total = int((await cursor.fetchone())["total"])
            decision = flood_decision(wait_seconds, total)
            account_until = now + decision.account_block_seconds
            current_pause = await self.get_runtime_int("global_pause_until", 0)
            async with db.execute(
                """
                SELECT COUNT(DISTINCT session_name) AS accounts
                FROM margin_flood_log
                WHERE happened_at > ?
                """,
                (now - 6 * 3600,),
            ) as cursor:
                flooded_accounts = int((await cursor.fetchone())["accounts"])
            global_until = current_pause
            if flooded_accounts >= 2:
                global_until = max(current_pause, now + decision.global_pause_seconds)
            await db.execute(
                """
                UPDATE margin_account_state
                SET blocked_until = MAX(blocked_until, ?),
                    last_flood_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_name = ?
                """,
                (account_until, now, session_name),
            )
            await db.execute(
                "UPDATE accounts SET cooldown_until = MAX(cooldown_until, ?), status = 'Cooldown' WHERE session_name = ?",
                (account_until, session_name),
            )
            if flooded_accounts >= 2:
                await db.execute(
                    "INSERT OR REPLACE INTO margin_runtime_state (key, value) VALUES ('global_pause_until', ?)",
                    (str(global_until),),
                )
            await db.execute("DELETE FROM margin_flood_log WHERE happened_at < ?", (now - 14 * 86400,))
            await db.commit()
            return decision, account_until, global_until, total
        finally:
            await db.close()

    async def recent_duplicate(self, text_hash, exclude_lead_id=None):
        db = await self._connect()
        try:
            params = [text_hash]
            exclude = ""
            if exclude_lead_id:
                exclude = "AND id != ?"
                params.append(exclude_lead_id)
            async with db.execute(
                f"""
                SELECT id FROM margin_leads
                WHERE text_hash = ?
                  {exclude}
                  AND (notified_at >= datetime('now', '-7 day') OR created_at >= datetime('now', '-7 day'))
                LIMIT 1
                """,
                tuple(params),
            ) as cursor:
                return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def claim_notification(self, lead_id, text_hash, now=None):
        now = int(now or time.time())
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT id FROM margin_leads
                WHERE text_hash = ? AND id != ?
                  AND notified_at >= datetime('now', '-7 day')
                LIMIT 1
                """,
                (text_hash, lead_id),
            ) as cursor:
                if await cursor.fetchone():
                    await db.commit()
                    return False
            async with db.execute(
                "SELECT lead_id, claimed_at FROM margin_notification_claims WHERE text_hash = ?",
                (text_hash,),
            ) as cursor:
                claim = await cursor.fetchone()
            if claim and int(claim["lead_id"]) != int(lead_id) and int(claim["claimed_at"]) > now - 900:
                await db.commit()
                return False
            await db.execute(
                """
                INSERT OR REPLACE INTO margin_notification_claims
                    (text_hash, lead_id, claimed_at)
                VALUES (?, ?, ?)
                """,
                (text_hash, lead_id, now),
            )
            await db.commit()
            return True
        finally:
            await db.close()

    async def release_notification_claim(self, lead_id, text_hash):
        db = await self._connect()
        try:
            await db.execute(
                "DELETE FROM margin_notification_claims WHERE text_hash = ? AND lead_id = ?",
                (text_hash, lead_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_lead(self, chat_id, message_id):
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT * FROM margin_leads WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            for field in ("why_client", "risks", "questions"):
                try:
                    result[field] = json.loads(result.get(field) or "[]")
                except (TypeError, json.JSONDecodeError):
                    result[field] = []
            return result
        finally:
            await db.close()

    async def save_lead(self, candidate, lead, raw_llm_json):
        score = max(0, min(7, as_int(lead.get("score"), 0)))
        signal_strength = max(1, min(5, as_int(lead.get("signal_strength"), score)))
        pain_type = normalize_pain_type(lead.get("pain_type"), candidate.get("pain_types"))
        actor_type, radar_type, temperature = lead_labels(lead, candidate)
        why_client = json.dumps(as_list(lead.get("why_client")), ensure_ascii=False)
        risks = json.dumps(as_list(lead.get("risks")) or ["непонятный контекст"], ensure_ascii=False)
        questions = json.dumps(as_list(lead.get("questions"))[:3], ensure_ascii=False)
        mini_audit = normalize_text(lead.get("mini_audit") or "позже")

        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO margin_leads
                    (chat_id, message_id, chat_title, chat_username, message_link,
                     author_id, author_username, author_name, msg_at, text,
                     pain_type, actor_type, radar_type, temperature, signal_strength,
                     score, why_client, risks, questions,
                     soft_reply, mini_audit, raw_llm_json, text_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    pain_type = excluded.pain_type,
                    actor_type = excluded.actor_type,
                    radar_type = excluded.radar_type,
                    temperature = excluded.temperature,
                    signal_strength = excluded.signal_strength,
                    score = excluded.score,
                    why_client = excluded.why_client,
                    risks = excluded.risks,
                    questions = excluded.questions,
                    soft_reply = excluded.soft_reply,
                    mini_audit = excluded.mini_audit,
                    raw_llm_json = excluded.raw_llm_json,
                    text_hash = excluded.text_hash,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    candidate["chat_id"],
                    candidate["message_id"],
                    candidate["chat_title"],
                    candidate["chat_username"],
                    candidate["message_link"],
                    candidate.get("author_id"),
                    candidate.get("author_username"),
                    candidate.get("author_name"),
                    candidate.get("msg_at"),
                    candidate["text"],
                    pain_type,
                    actor_type,
                    radar_type,
                    temperature,
                    signal_strength,
                    score,
                    why_client,
                    risks,
                    questions,
                    str(lead.get("soft_reply") or "").strip(),
                    mini_audit,
                    raw_llm_json,
                    candidate["text_hash"],
                ),
            )
            async with db.execute(
                "SELECT * FROM margin_leads WHERE chat_id = ? AND message_id = ?",
                (candidate["chat_id"], candidate["message_id"]),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            result = dict(row)
            result["why_client"] = json.loads(result.get("why_client") or "[]")
            result["risks"] = json.loads(result.get("risks") or "[]")
            result["questions"] = json.loads(result.get("questions") or "[]")
            return result
        finally:
            await db.close()

    async def save_verifier(self, lead_id, verifier, passed):
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT text, pain_type, score FROM margin_leads WHERE id = ?",
                (lead_id,),
            ) as cursor:
                lead = await cursor.fetchone()
            labels = verifier_labels(
                verifier,
                lead["text"] if lead else "",
                [lead["pain_type"]] if lead and lead["pain_type"] else None,
                lead["score"] if lead else 0,
            )
            rejection_reason = None if passed else verifier_rejection_code(verifier)
            rejection_detail = None if passed else str(verifier.get("reason") or "")
            await db.execute(
                """
                UPDATE margin_leads
                SET verifier_json = ?,
                    verifier_passed = ?,
                    actor_type = ?,
                    radar_type = ?,
                    temperature = ?,
                    filter_score = ?,
                    rejection_reason = ?,
                    rejection_detail = ?,
                    strict_at = CASE WHEN ? THEN COALESCE(strict_at, CURRENT_TIMESTAMP) ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    json.dumps(verifier, ensure_ascii=False),
                    1 if passed else 0,
                    labels["actor_type"],
                    labels["radar_type"],
                    labels["temperature"],
                    labels["filter_score"],
                    rejection_reason,
                    clip_text(rejection_detail, 700),
                    1 if passed else 0,
                    lead_id,
                ),
            )
            await db.commit()
            return {
                **labels,
                "rejection_reason": rejection_reason,
                "rejection_detail": rejection_detail,
            }
        finally:
            await db.close()

    async def mark_notified(self, lead_id):
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE margin_leads SET notified = 1, notified_at = CURRENT_TIMESTAMP WHERE id = ?",
                (lead_id,),
            )
            await db.execute(
                """
                UPDATE margin_screening_events
                SET outcome = 'notified', rejection_reason = NULL,
                    rejection_detail = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE lead_id = ?
                """,
                (lead_id,),
            )
            await db.commit()
        finally:
            await db.close()

    async def pending_verified_leads(self, limit=50):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT *
                FROM margin_leads
                WHERE verifier_passed = 1
                  AND filter_score >= 9
                  AND notified = 0
                ORDER BY created_at, id
                LIMIT ?
                """,
                (int(limit),),
            ) as cursor:
                rows = await cursor.fetchall()
            results = []
            for row in rows:
                lead = dict(row)
                for field in ("why_client", "risks", "questions"):
                    try:
                        lead[field] = json.loads(lead.get(field) or "[]")
                    except (TypeError, json.JSONDecodeError):
                        lead[field] = []
                results.append(lead)
            return results
        finally:
            await db.close()

    async def regrade_recent_verifiers(self, hours=None):
        db = await self._connect()
        changed = 0
        passed = 0
        try:
            time_filter = ""
            params = []
            if hours is not None:
                time_filter = "AND created_at >= datetime('now', ?)"
                params.append(f"-{int(hours)} hours")
            async with db.execute(
                f"""
                SELECT id, verifier_json, verifier_passed, text, pain_type, score,
                       actor_type, radar_type, temperature, filter_score,
                       rejection_reason, rejection_detail, notified
                FROM margin_leads
                WHERE verifier_json IS NOT NULL
                  {time_filter}
                """,
                tuple(params),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                try:
                    result = json.loads(row["verifier_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                new_passed = 1 if verifier_passed(result) else 0
                labels = verifier_labels(
                    result,
                    row["text"] or "",
                    [row["pain_type"]] if row["pain_type"] else None,
                    row["score"],
                )
                rejection_reason = None if new_passed else verifier_rejection_code(result)
                rejection_detail = None if new_passed else str(result.get("reason") or "")
                passed += new_passed
                labels_changed = any(
                    [
                        labels["actor_type"] != (row["actor_type"] or "unknown"),
                        labels["radar_type"] != (row["radar_type"] or "margin_leak"),
                        labels["temperature"] != (row["temperature"] or "yellow"),
                        labels["filter_score"] != int(row["filter_score"] or 0),
                        rejection_reason != row["rejection_reason"],
                    ]
                )
                if new_passed != int(row["verifier_passed"] or 0) or labels_changed:
                    await db.execute(
                        """
                        UPDATE margin_leads
                        SET verifier_passed = ?,
                            actor_type = ?,
                            radar_type = ?,
                            temperature = ?,
                            filter_score = ?,
                            rejection_reason = ?,
                            rejection_detail = ?,
                            strict_at = CASE WHEN ? THEN COALESCE(strict_at, CURRENT_TIMESTAMP) ELSE NULL END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            new_passed,
                            labels["actor_type"],
                            labels["radar_type"],
                            labels["temperature"],
                            labels["filter_score"],
                            rejection_reason,
                            clip_text(rejection_detail, 700),
                            new_passed,
                            row["id"],
                        ),
                    )
                    await db.execute(
                        """
                        UPDATE margin_screening_events
                        SET outcome = ?,
                            rejection_reason = ?,
                            rejection_detail = ?,
                            updated_at = CASE
                                WHEN source = 'migration' THEN updated_at
                                ELSE CURRENT_TIMESTAMP
                            END
                        WHERE lead_id = ?
                        """,
                        (
                            (
                                "notified"
                                if int(row["notified"] or 0)
                                else ("strict" if new_passed else "candidate")
                            ),
                            None if int(row["notified"] or 0) else rejection_reason,
                            None if int(row["notified"] or 0) else clip_text(rejection_detail, 700),
                            row["id"],
                        ),
                    )
                    changed += 1
            await db.commit()
            return changed, passed, len(rows)
        finally:
            await db.close()

    async def set_feedback(self, lead_id, feedback):
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE margin_leads SET feedback = ?, feedback_at = CURRENT_TIMESTAMP WHERE id = ?",
                (feedback, lead_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def top_chats(self, limit=10):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT t.title, t.username, s.tier, s.priority_score,
                       s.strict_yield_1000, s.messages_7d, s.candidates_7d,
                       s.strict_7d, s.live_member, s.hot_until,
                       s.manual_boost, s.manual_boost_until
                FROM margin_radar_state s
                JOIN target_chats t ON t.chat_id = s.chat_id
                ORDER BY s.priority_score DESC
                LIMIT ?
                """,
                (int(limit),),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def set_manual_boost(self, username, days):
        clean = str(username or "").strip().lstrip("@").lower()
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT t.chat_id, t.title
                FROM target_chats t
                WHERE lower(ltrim(t.username, '@')) = ?
                """,
                (clean,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            until = int(time.time()) + int(days) * 86400 if int(days) > 0 else 0
            boost = 20 if until else 0
            await db.execute(
                """
                UPDATE margin_radar_state
                SET manual_boost = ?, manual_boost_until = ?,
                    next_check_at = CASE WHEN ? > 0 THEN MIN(next_check_at, ?) ELSE next_check_at END
                WHERE chat_id = ?
                """,
                (boost, until, until, int(time.time()), row["chat_id"]),
            )
            await db.commit()
            return {"chat_id": row["chat_id"], "title": row["title"], "until": until}
        finally:
            await db.close()

    async def target_chat_usernames(self):
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT chat_id, username, title
                FROM target_chats
                WHERE username IS NOT NULL AND username != ''
                """
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def save_membership(self, session_name, member_usernames, checked_at):
        member_usernames = {normalize_text(value).lstrip("@") for value in member_usernames if value}
        db = await self._connect()
        try:
            async with db.execute(
                """
                SELECT chat_id, username FROM target_chats
                WHERE username IS NOT NULL AND username != ''
                """
            ) as cursor:
                targets = await cursor.fetchall()
            rows = [
                (
                    row["chat_id"],
                    session_name,
                    1 if normalize_text(row["username"]).lstrip("@") in member_usernames else 0,
                    checked_at,
                )
                for row in targets
            ]
            await db.executemany(
                """
                INSERT INTO margin_chat_membership
                    (chat_id, session_name, is_member, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, session_name) DO UPDATE SET
                    is_member = excluded.is_member,
                    checked_at = excluded.checked_at
                """,
                rows,
            )
            await db.execute(
                """
                UPDATE margin_radar_state
                SET live_member = CASE WHEN EXISTS (
                    SELECT 1 FROM margin_chat_membership m
                    WHERE m.chat_id = margin_radar_state.chat_id
                      AND m.is_member = 1
                ) THEN 1 ELSE 0 END
                """
            )
            await db.commit()
            return sum(row[2] for row in rows)
        finally:
            await db.close()


class TelegramNotifier:
    def __init__(self, store):
        self.store = store
        self.bot = None
        self.dp = None
        self.polling_task = None

    async def start(self):
        if not Config.BOT_TOKEN or not Config.ADMIN_ID:
            log_event(MODULE, "NOTIFIER_DISABLED", "Bot token or ADMIN_ID is missing")
            return
        proxy_url = telegram_proxy_url()
        session = AiohttpSession(proxy=proxy_url) if proxy_url else None
        self.bot = Bot(
            token=Config.BOT_TOKEN,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()

        @self.dp.callback_query(F.data.startswith("margin_feedback:"))
        async def feedback_handler(callback: CallbackQuery):
            if callback.from_user.id != Config.ADMIN_ID:
                await callback.answer("Недоступно", show_alert=True)
                return
            _, verdict, raw_id = callback.data.split(":", 2)
            lead_id = as_int(raw_id, 0)
            if verdict not in {"good", "trash"} or lead_id <= 0:
                await callback.answer("Некорректная оценка", show_alert=True)
                return
            await self.store.set_feedback(lead_id, verdict)
            await callback.answer("Сохранено: годный" if verdict == "good" else "Сохранено: мусор")

        @self.dp.message(Command("radar_stats"))
        async def radar_stats_handler(message: Message):
            if message.from_user.id != Config.ADMIN_ID:
                return
            blocks = []
            for seconds, label in ((1200, "20 минут"), (3600, "1 час"), (86400, "24 часа")):
                stats = await self.store.funnel_stats(seconds)
                candidates = stats["candidate"] + stats["strict"] + stats["notified"]
                strict = stats["strict"] + stats["notified"]
                conversion = strict / candidates if candidates else 0
                reasons = ", ".join(
                    f"{key}={value}" for key, value in list(stats["reasons"].items())[:6]
                ) or "нет"
                blocks.append(
                    f"<b>{label}</b>: screened={stats['screened']}, candidates={candidates}, "
                    f"strict={strict}, notified={stats['notified']}, conversion={conversion:.0%}\n"
                    f"Отсев: {html.escape(reasons)}"
                )
            await message.answer("\n\n".join(blocks))

        @self.dp.message(Command("radar_top"))
        async def radar_top_handler(message: Message):
            if message.from_user.id != Config.ADMIN_ID:
                return
            rows = await self.store.top_chats(10)
            lines = ["<b>LeadRadar · свежий топ источников</b>"]
            for index, row in enumerate(rows, 1):
                live = " live" if row["live_member"] else ""
                lines.append(
                    f"{index}. <b>{html.escape(row['title'] or row['username'])}</b> "
                    f"[{row['tier']}{live}]\n"
                    f"yield={row['strict_yield_1000']:.1f}/1000 · "
                    f"strict={row['strict_7d']} · candidates={row['candidates_7d']} · "
                    f"messages={row['messages_7d']}"
                )
            await message.answer("\n\n".join(lines)[:3900], disable_web_page_preview=True)

        @self.dp.message(Command("radar_boost"))
        async def radar_boost_handler(message: Message):
            if message.from_user.id != Config.ADMIN_ID:
                return
            parts = (message.text or "").split()
            if len(parts) != 3 or not parts[2].isdigit() or not 1 <= int(parts[2]) <= 7:
                await message.answer("Формат: /radar_boost @chat 1-7")
                return
            result = await self.store.set_manual_boost(parts[1], int(parts[2]))
            if not result:
                await message.answer("Чат не найден в target_chats")
                return
            await message.answer(
                f"Boost включён: {html.escape(result['title'])} на {parts[2]} дн."
            )

        @self.dp.message(Command("radar_unboost"))
        async def radar_unboost_handler(message: Message):
            if message.from_user.id != Config.ADMIN_ID:
                return
            parts = (message.text or "").split()
            if len(parts) != 2:
                await message.answer("Формат: /radar_unboost @chat")
                return
            result = await self.store.set_manual_boost(parts[1], 0)
            await message.answer(
                f"Boost снят: {html.escape(result['title'])}"
                if result
                else "Чат не найден в target_chats"
            )

        self.polling_task = asyncio.create_task(
            self.dp.start_polling(self.bot, handle_signals=False, close_bot_session=False)
        )
        log_event(
            MODULE,
            "NOTIFIER_READY",
            "Telegram notifier and feedback polling are ready",
            admin_id=Config.ADMIN_ID,
            system_proxy="yes" if proxy_url else "no",
        )
        await self.flush_pending_leads()

    async def close(self):
        if self.dp and self.polling_task and not self.polling_task.done():
            await self.dp.stop_polling()
            await self.polling_task
        if self.bot:
            await self.bot.session.close()

    async def send_service(self, text):
        if not self.bot:
            return
        try:
            await self.bot.send_message(Config.ADMIN_ID, html.escape(text), disable_web_page_preview=True)
        except Exception as exc:
            log_event(MODULE, "SERVICE_NOTIFY_ERROR", "Could not send service notification", error=exc)

    def lead_keyboard(self, lead_id):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Годный", callback_data=f"margin_feedback:good:{lead_id}"),
                    InlineKeyboardButton(text="Мусор", callback_data=f"margin_feedback:trash:{lead_id}"),
                ]
            ]
        )

    def format_card(self, lead):
        def e(value):
            return html.escape(str(value or ""))

        radar_type = lead.get("radar_type") or detect_radar_type(
            lead.get("text", ""),
            [lead.get("pain_type")] if lead.get("pain_type") else None,
        )
        radar_label = RADAR_LABELS.get(radar_type, "LeadRadar")
        temperature = TEMPERATURE_LABELS.get(lead.get("temperature"), "🟡 жёлтый")
        why = "\n".join(f"- {e(item)}" for item in lead.get("why_client", [])[:4])
        risks = " / ".join(e(item) for item in lead.get("risks", [])[:4])
        questions = "\n".join(
            f"{index}. {e(item)}" for index, item in enumerate(lead.get("questions", [])[:3], 1)
        )
        return (
            f"<b>Новый лид · {e(radar_label)}</b>\n"
            f"<b>Температура:</b> {e(temperature)} · фильтр={e(lead.get('filter_score'))}/10\n\n"
            f"<b>Чат:</b>\n{e(lead.get('chat_title'))}\n"
            f"<b>Ссылка:</b>\n{e(lead.get('message_link'))}\n"
            f"<b>Дата:</b>\n{e(lead.get('msg_at'))}\n"
            f"<b>Автор:</b>\n{e(lead.get('author_name') or lead.get('author_username') or 'не сохранён')}\n"
            f"<b>Сообщение:</b>\n<i>{e(clip_text(lead.get('text'), 900))}</i>\n\n"
            f"<b>Кто говорит:</b> {e(lead.get('actor_type') or ACTOR_SELLER)}\n"
            f"<b>Тип боли:</b> {e(lead.get('pain_type'))}\n"
            f"<b>Сила:</b> {e(lead.get('signal_strength'))}/5 · score={e(lead.get('score'))}\n\n"
            f"<b>Почему клиент:</b>\n{why}\n\n"
            f"<b>Риски:</b>\n{risks}\n\n"
            f"<b>Что спросить:</b>\n{questions}\n\n"
            f"<b>Мягкий ответ:</b>\n- {e(lead.get('soft_reply'))}\n\n"
            f"<b>Мини-разбор:</b> {e(lead.get('mini_audit'))}"
        )[:3900]

    async def send_lead(self, lead):
        if not self.bot:
            return False
        for attempt in range(1, 4):
            try:
                await self.bot.send_message(
                    Config.ADMIN_ID,
                    self.format_card(lead),
                    reply_markup=self.lead_keyboard(lead["id"]),
                    disable_web_page_preview=True,
                )
                return True
            except Exception as exc:
                log_event(
                    MODULE,
                    "LEAD_NOTIFY_ERROR",
                    "Could not send lead card",
                    lead_id=lead.get("id"),
                    attempt=attempt,
                    error=exc,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt * 5)
        return False

    async def flush_pending_leads(self):
        pending = await self.store.pending_verified_leads()
        if not pending:
            return
        sent = 0
        for lead in pending:
            if not await self.store.claim_notification(lead["id"], lead.get("text_hash") or f"lead:{lead['id']}"):
                continue
            if not await self.send_lead(lead):
                await self.store.release_notification_claim(
                    lead["id"], lead.get("text_hash") or f"lead:{lead['id']}"
                )
                log_event(
                    MODULE,
                    "PENDING_NOTIFY_PAUSED",
                    "Pending lead delivery paused after a send failure",
                    lead_id=lead.get("id"),
                    remaining=len(pending) - sent,
                )
                break
            await self.store.mark_notified(lead["id"])
            sent += 1
            log_event(
                MODULE,
                "LEAD_NOTIFIED_RETRY",
                "Pending verified lead card sent",
                lead_id=lead["id"],
                source="startup_queue",
            )
            await asyncio.sleep(0.8)
        log_event(
            MODULE,
            "PENDING_NOTIFY_DONE",
            "Pending verified lead delivery finished",
            sent=sent,
            total=len(pending),
        )


def classifier_prompt(chat_title, candidates):
    blocks = []
    for candidate in candidates:
        blocks.append(
            "\n".join(
                [
                    f"ID:{candidate['message_id']}",
                    f"CHAT:{chat_title}",
                    f"AUTHOR:{candidate.get('author_name') or candidate.get('author_username') or 'unknown'}",
                    f"TEXT:{clip_text(candidate['text'], 1200)}",
                ]
            )
        )
    return (
        "Ты первый широкий фильтр кандидатов LeadRadar среди селлеров маркетплейсов. Не доверяй одним ключевым словам.\n"
        "Сначала определи роль автора: seller — человек описывает свой кейс; service_provider — продаёт бухгалтерию, инфографику, аналитику, ведение или другую услугу; unknown — неясно.\n"
        "Любой service_provider, рекламный оффер, портфолио, цена услуги, призыв написать в ЛС, вакансия, новость, отзыв или экспертный ответ получает score 0-2 и is_lead=false.\n"
        "Score 6-7 ставь только личному текущему кейсу seller с уже возникшей денежной/операционной потерей и поиском решения.\n"
        "radar_type: margin_leak — комиссия/маржа/убыток; frozen_stock — товар принят или числится, но завис/не продаётся/хранение списывается/поддержка молчит; fbs_chaos — поставка/FBS/ПВЗ/отгрузка/статус/недовоз/горящие сроки.\n"
        "temperature: red — уже горят деньги, товар или продажи; yellow — проблема реальна, но не критична; green — вопрос роста или масштабирования.\n"
        "Верни STRICT JSON: {\"leads\":[{\"message_id\":1,\"is_lead\":true,\"score\":6,"
        "\"actor_type\":\"seller\",\"radar_type\":\"frozen_stock\",\"temperature\":\"red\","
        "\"signal_strength\":5,\"pain_type\":\"логистика\",\"why_client\":[\"...\"],"
        "\"risks\":[\"...\"],\"questions\":[\"...\"],\"soft_reply\":\"...\",\"mini_audit\":\"да\"}]}\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def verifier_prompt(candidate):
    return (
        "Ты второй, жёсткий и независимый фильтр одного исходного Telegram-сообщения.\n"
        "Сначала ответь: автор сам селлер с личной болью или исполнитель, продающий услугу. Упоминание боли клиентов в рекламе не делает автора лидом.\n"
        "Бухгалтерская реклама, инфографика/дизайн карточек, портфолио, цена, опыт, «мы поможем», призыв написать в ЛС, вакансия, отзыв, новость и ответ эксперта всегда отклоняются.\n"
        "Пропускай только личный текущий кейс селлера, где уже затронуты деньги, товар, продажи или операция и человек ищет решение.\n"
        "FrozenStockRadar: товар принят/числится, но заморожен, не в продаже, хранение списывают, продажи встали или поддержка молчит.\n"
        "FBSChaosRadar: поставка, FBS, ПВЗ, отгрузка, недовоз, статус или сроки. MarginLeakRadar: комиссия, маржа, убыток, неокупаемая реклама или логистика съела прибыль.\n"
        "STRICT JSON: {\"actor_type\":\"seller|service_provider|unknown\",\"radar_type\":\"margin_leak|frozen_stock|fbs_chaos\",\"temperature\":\"red|yellow|green\","
        "\"is_seller\":bool,\"personal_case\":bool,\"current_problem\":bool,"
        "\"money_or_ops_loss\":bool,\"seeks_solution\":bool,\"is_ad\":bool,\"is_review\":bool,"
        "\"is_news\":bool,\"is_expert_reply\":bool,\"has_specificity\":bool,\"helplessness\":bool,"
        "\"urgent\":bool,\"confidence\":0.0,\"reason\":\"...\"}\n\n"
        f"CHAT: {candidate['chat_title']}\n"
        f"AUTHOR_NAME: {candidate.get('author_name') or 'unknown'}\n"
        f"AUTHOR_USERNAME: {candidate.get('author_username') or 'unknown'}\n"
        f"TEXT: {clip_text(candidate['text'], 1600)}"
    )


def verifier_passed(result):
    return strict_verifier_passed(result, VERIFY_MIN_CONFIDENCE)


async def classify_candidates(candidates):
    if not candidates:
        return []
    engine = get_ai()
    raw, _ = await engine.chat(
        [{"role": "user", "content": classifier_prompt(candidates[0]["chat_title"], candidates)}],
        model="llama-3.1-8b-instant",
        json_mode=True,
    )
    if not raw:
        raise RuntimeError("lead classifier is temporarily unavailable")
    data = parse_json(raw)
    if not isinstance(data.get("leads"), list):
        raise RuntimeError("lead classifier returned invalid JSON")
    by_id = {candidate["message_id"]: candidate for candidate in candidates}
    rows_by_id = {}
    for row in data["leads"]:
        message_id = as_int(row.get("message_id"), 0)
        if message_id in by_id:
            rows_by_id[message_id] = row
    return [
        (
            candidate,
            rows_by_id.get(
                candidate["message_id"],
                {
                    "message_id": candidate["message_id"],
                    "is_lead": False,
                    "score": 0,
                    "signal_strength": 1,
                    "pain_type": (candidate.get("pain_types") or ["маржа"])[0],
                    "risks": ["первый классификатор не выделил сообщение"],
                },
            ),
            raw,
        )
        for candidate in candidates
    ]


async def verify_candidate(candidate):
    engine = get_ai()
    raw, _ = await engine.chat(
        [{"role": "user", "content": verifier_prompt(candidate)}],
        model="llama-3.1-8b-instant",
        json_mode=True,
    )
    if not raw:
        raise RuntimeError("independent lead verifier is temporarily unavailable")
    result = parse_json(raw)
    required_fields = {
        "is_seller",
        "personal_case",
        "current_problem",
        "money_or_ops_loss",
        "seeks_solution",
        "is_ad",
        "is_review",
        "is_news",
        "is_expert_reply",
        "confidence",
    }
    if not required_fields.issubset(result):
        raise RuntimeError("independent lead verifier returned invalid JSON")
    return result, verifier_passed(result)


class SafeMarginRadar:
    def __init__(self):
        self.store = ProtectionStore(Config.DB_PATH)
        self.notifier = TelegramNotifier(self.store)
        self.clients = {}
        self.client_locks = {}
        self.live_by_username = {}
        self.event_queue = asyncio.Queue(maxsize=500)
        self.stop_event = asyncio.Event()
        self.rollout_started_at = 0

    async def validate_configuration(self):
        missing = []
        if not Config.API_ID:
            missing.append("API_ID")
        if not Config.API_HASH:
            missing.append("API_HASH")
        if not Config.GROQ_KEYS:
            missing.append("GROQ_API_KEYS")
        if not Config.BOT_TOKEN:
            missing.append("OVERSEER_BOT_TOKEN")
        if not Config.ADMIN_ID:
            missing.append("ADMIN_ID")
        if missing:
            raise RuntimeError("MarginLeakRadar configuration is incomplete: " + ", ".join(missing))
        if not await self.store.account(PRIMARY_SESSION):
            raise RuntimeError("MarginLeakRadar requires SESSION_STRING1/account_1")
        if not await self.store.account(RESERVE_SESSION):
            raise RuntimeError("LeadRadar two-account mode requires SESSION_STRING2/account_2")

    async def build_client(self, session_name):
        account = await self.store.account(session_name)
        if not account:
            return None
        app = Client(
            name=session_name,
            session_string=account["session_string"],
            api_id=account["api_id"],
            api_hash=account["api_hash"],
            proxy=telegram_proxy(),
            device_model=account["device_model"],
            system_version=account["system_version"],
            app_version=account["app_version"],
            in_memory=True,
        )
        app.add_handler(MessageHandler(self.on_live_message))
        return app

    async def ensure_connected(self, session_name):
        lock = self.client_locks.setdefault(session_name, asyncio.Lock())
        async with lock:
            if session_name in self.clients:
                return self.clients[session_name]
            account = await self.store.account(session_name)
            now = int(time.time())
            if not account:
                return None
            ready_at = max(int(account["cooldown_until"] or 0), int(account["blocked_until"] or 0))
            if ready_at > now:
                return None
            app = await self.build_client(session_name)
            if not app:
                return None
            try:
                await asyncio.wait_for(app.start(), timeout=45)
            except asyncio.TimeoutError:
                try:
                    await asyncio.wait_for(app.stop(), timeout=5)
                except Exception:
                    pass
                await self.store.mark_client_status(session_name, "Connection_Error")
                log_event(
                    MODULE,
                    "CLIENT_CONNECT_TIMEOUT",
                    "Telegram client could not connect within 45 seconds using the active system network settings",
                    session_name=session_name,
                )
                return None
            except FloodWait as exc:
                await self.handle_flood(session_name, exc.value)
                return None
            except Exception as exc:
                try:
                    await asyncio.wait_for(app.stop(), timeout=5)
                except Exception:
                    pass
                await self.store.mark_client_status(session_name, "Connection_Error")
                log_event(
                    MODULE,
                    "CLIENT_CONNECT_ERROR",
                    "Telegram connection failed using the active system network settings",
                    session_name=session_name,
                    error=exc,
                )
                return None
            self.clients[session_name] = app
            await self.store.mark_client_status(session_name, "Monitoring")
            log_event(MODULE, "CLIENT_CONNECTED", "Persistent Telegram client connected", session_name=session_name)
            return app

    async def close_clients(self):
        for session_name, app in list(self.clients.items()):
            try:
                await app.stop()
            except Exception:
                pass
            account = await self.store.account(session_name)
            status = "Cooldown" if account and max(int(account["cooldown_until"] or 0), int(account["blocked_until"] or 0)) > int(time.time()) else "Active"
            await self.store.mark_client_status(session_name, status)
        self.clients.clear()

    async def refresh_memberships(self):
        total = 0
        for session_name in (PRIMARY_SESSION, RESERVE_SESSION):
            app = await self.ensure_connected(session_name)
            if not app:
                continue
            usernames = set()
            try:
                async for dialog in app.get_dialogs():
                    username = getattr(getattr(dialog, "chat", None), "username", None)
                    if username:
                        usernames.add(username)
            except FloodWait as exc:
                await self.handle_flood(session_name, exc.value)
                continue
            except Exception as exc:
                log_event(
                    MODULE,
                    "MEMBERSHIP_ERROR",
                    "Could not refresh Telegram dialog membership",
                    session_name=session_name,
                    error=exc,
                )
                continue
            matched = await self.store.save_membership(session_name, usernames, int(time.time()))
            total += matched
            log_event(
                MODULE,
                "MEMBERSHIP_READY",
                "Telegram target-chat membership refreshed",
                session_name=session_name,
                matched=matched,
            )
        targets = await self.store.target_chat_usernames()
        self.live_by_username = {
            normalize_text(chat["username"]).lstrip("@"): chat
            for chat in targets
            if chat.get("username")
        }
        return total

    async def membership_loop(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=MEMBERSHIP_REFRESH_SECONDS)
            except asyncio.TimeoutError:
                await self.refresh_memberships()

    async def ranking_loop(self):
        while not self.stop_event.is_set():
            await self.store.assign_chat_tiers(int(time.time()))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=RANK_REFRESH_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def on_live_message(self, _client, message):
        username = normalize_text(getattr(getattr(message, "chat", None), "username", None)).lstrip("@")
        live_chat = self.live_by_username.get(username)
        if not live_chat:
            return
        candidate = message_to_candidate(message, live_chat)
        reason = hard_rejection_reason(candidate["text"], candidate["chat_title"])
        if reason:
            await self.store.save_screening_event(
                candidate,
                source="live",
                outcome="screened",
                rejection_reason=local_rejection_code(reason),
                rejection_detail=reason,
            )
            await self.store.update_live_cursor(live_chat["chat_id"], message.id)
            log_event(MODULE, "LIVE_SKIP", "Live message rejected locally", reason=reason, chat=candidate["chat_title"])
            return
        await self.store.enqueue_live_candidate(candidate)
        try:
            self.event_queue.put_nowait(1)
        except asyncio.QueueFull:
            log_event(
                MODULE,
                "LIVE_QUEUE_BUSY",
                "Live candidate is safe in SQLite and will be processed later",
                chat=candidate["chat_title"],
                message_id=candidate["message_id"],
            )

    async def event_consumer(self):
        while not self.stop_event.is_set():
            queued = await self.store.next_live_candidate()
            if not queued:
                try:
                    await asyncio.wait_for(self.event_queue.get(), timeout=5)
                    self.event_queue.task_done()
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.process_candidates([queued["candidate"]], source="live")
                await self.store.complete_live_candidate(queued["queue_id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.store.fail_live_candidate(queued["queue_id"], exc)
                log_event(
                    MODULE,
                    "LIVE_PROCESS_ERROR",
                    "Live candidate remains in SQLite for retry",
                    queue_id=queued["queue_id"],
                    attempts=queued["attempts"] + 1,
                    error=exc,
                )
                await asyncio.sleep(min(300, 15 * (queued["attempts"] + 1)))

    async def process_candidates(self, candidates, source):
        fresh = []
        for candidate in candidates:
            existing = await self.store.get_lead(candidate["chat_id"], candidate["message_id"])
            if existing:
                if existing.get("notified"):
                    continue
                if not existing.get("verifier_json"):
                    verifier, passed = await verify_candidate(candidate)
                    labels = await self.store.save_verifier(existing["id"], verifier, passed)
                    existing.update(labels)
                    await self.store.update_screening_outcome(
                        candidate["chat_id"],
                        candidate["message_id"],
                        "strict" if passed else "candidate",
                        rejection_reason=labels.get("rejection_reason"),
                        rejection_detail=labels.get("rejection_detail"),
                        lead_id=existing["id"],
                    )
                    if not passed:
                        log_event(
                            MODULE,
                            "VERIFY_REJECT",
                            "Independent verifier rejected lead",
                            lead_id=existing["id"],
                            filter_score=labels["filter_score"],
                            actor_type=labels["actor_type"],
                            reason=verifier.get("reason"),
                        )
                        continue
                    existing["verifier_passed"] = 1
                elif int(existing.get("filter_score") or 0) < 9:
                    try:
                        verifier = json.loads(existing["verifier_json"])
                    except (TypeError, json.JSONDecodeError):
                        verifier = {}
                    passed = verifier_passed(verifier)
                    labels = await self.store.save_verifier(existing["id"], verifier, passed)
                    existing.update(labels)
                    existing["verifier_passed"] = 1 if passed else 0
                if existing.get("verifier_passed") and int(existing.get("filter_score") or 0) >= 9:
                    if not await self.store.claim_notification(
                        existing["id"], existing.get("text_hash") or candidate["text_hash"]
                    ):
                        await self.store.update_screening_outcome(
                            candidate["chat_id"],
                            candidate["message_id"],
                            "candidate",
                            rejection_reason="duplicate",
                            rejection_detail="notification already claimed by another matching lead",
                            lead_id=existing["id"],
                        )
                        continue
                    if await self.notifier.send_lead(existing):
                        await self.store.mark_notified(existing["id"])
                        log_event(
                            MODULE,
                            "LEAD_NOTIFIED_RETRY",
                            "Previously verified lead card sent",
                            lead_id=existing["id"],
                            source=source,
                        )
                        continue
                    await self.store.release_notification_claim(
                        existing["id"], existing.get("text_hash") or candidate["text_hash"]
                    )
                    raise RuntimeError(f"could not notify verified lead {existing['id']}")
                if existing.get("verifier_json") and not existing.get("verifier_passed"):
                    continue
            if await self.store.recent_duplicate(candidate["text_hash"]):
                await self.store.save_screening_event(
                    candidate,
                    source=source,
                    outcome="screened",
                    rejection_reason="duplicate",
                    rejection_detail="same normalized text seen during the last 7 days",
                )
                log_event(MODULE, "DUPLICATE_SKIP", "Duplicate text skipped before LLM", source=source, message_id=candidate["message_id"])
                continue
            await self.store.save_screening_event(
                candidate,
                source=source,
                outcome="candidate",
            )
            fresh.append(candidate)
        for candidate, lead_row, raw in await classify_candidates(fresh):
            lead = await self.store.save_lead(candidate, lead_row, raw)
            await self.store.update_screening_outcome(
                candidate["chat_id"],
                candidate["message_id"],
                "candidate",
                lead_id=lead["id"],
            )
            verifier, passed = await verify_candidate(candidate)
            labels = await self.store.save_verifier(lead["id"], verifier, passed)
            lead.update(labels)
            await self.store.update_screening_outcome(
                candidate["chat_id"],
                candidate["message_id"],
                "strict" if passed else "candidate",
                rejection_reason=labels.get("rejection_reason"),
                rejection_detail=labels.get("rejection_detail"),
                lead_id=lead["id"],
            )
            if not passed:
                log_event(
                    MODULE,
                    "VERIFY_REJECT",
                    "Independent verifier rejected lead",
                    lead_id=lead["id"],
                    filter_score=labels["filter_score"],
                    actor_type=labels["actor_type"],
                    reason=verifier.get("reason"),
                )
                continue
            if await self.store.recent_duplicate(candidate["text_hash"], exclude_lead_id=lead["id"]):
                await self.store.update_screening_outcome(
                    candidate["chat_id"],
                    candidate["message_id"],
                    "candidate",
                    rejection_reason="duplicate",
                    rejection_detail="strict candidate duplicates a recent stored lead",
                    lead_id=lead["id"],
                )
                log_event(MODULE, "NOTIFY_DEDUPE", "Lead passed quality but duplicate notification was suppressed", lead_id=lead["id"])
                continue
            if not await self.store.claim_notification(lead["id"], candidate["text_hash"]):
                await self.store.update_screening_outcome(
                    candidate["chat_id"],
                    candidate["message_id"],
                    "candidate",
                    rejection_reason="duplicate",
                    rejection_detail="notification already claimed by another matching lead",
                    lead_id=lead["id"],
                )
                continue
            if await self.notifier.send_lead(lead):
                await self.store.mark_notified(lead["id"])
                log_event(
                    MODULE,
                    "LEAD_NOTIFIED",
                    "Verified lead card sent",
                    lead_id=lead["id"],
                    score=lead["score"],
                    filter_score=lead["filter_score"],
                    radar_type=lead["radar_type"],
                    temperature=lead["temperature"],
                    source=source,
                )
            else:
                await self.store.release_notification_claim(lead["id"], candidate["text_hash"])
                raise RuntimeError(f"could not notify verified lead {lead['id']}")

    async def poll_chat(self, app, session_name, role, chat):
        now = int(time.time())
        await self.store.record_request(session_name, role, now)
        baseline = max(int(chat["target_last_read"] or 0), int(chat["radar_last_seen"] or 0))
        offset_id = int(chat.get("history_offset_id") or 0)
        high_watermark = max(baseline, int(chat.get("history_high_watermark") or 0))
        stored = []
        candidates = []
        page_count = 0
        page_oldest = 0
        page_newest = 0
        page_message_ids = []
        screening_events = []
        try:
            history_kwargs = {"limit": HISTORY_LIMIT}
            if offset_id > 0:
                history_kwargs["offset_id"] = offset_id
            async for message in app.get_chat_history(chat["username"], **history_kwargs):
                message_id = int(message.id)
                page_count += 1
                page_message_ids.append(message_id)
                if message_id <= baseline:
                    old_candidate = message_to_candidate(message, chat)
                    screening_events.append(
                        {
                            "candidate": old_candidate,
                            "source": "poll",
                            "outcome": "screened",
                            "rejection_reason": "old_message",
                            "rejection_detail": f"message_id={message_id} baseline={baseline}",
                        }
                    )
                    break
                page_oldest = message_id if page_oldest == 0 else min(page_oldest, message_id)
                page_newest = max(page_newest, message_id)
                candidate = message_to_candidate(message, chat)
                stored.append(
                    {
                        "chat_id": candidate["chat_id"],
                        "message_id": candidate["message_id"],
                        "chat_title": candidate["chat_title"],
                        "chat_username": candidate["chat_username"],
                        "message_link": candidate["message_link"],
                        "author_id": candidate["author_id"],
                        "author_username": candidate["author_username"],
                        "author_name": candidate["author_name"],
                        "text": candidate["text"],
                        "raw_type": type(message).__name__,
                        "msg_at": candidate["msg_at"],
                        "has_text": bool(candidate["text"]),
                        "is_bot": bool(getattr(getattr(message, "from_user", None), "is_bot", False)),
                    }
                )
                if not candidate["text"] or stored[-1]["is_bot"]:
                    screening_events.append(
                        {
                            "candidate": candidate,
                            "source": "poll",
                            "outcome": "screened",
                            "rejection_reason": "unsupported_topic",
                            "rejection_detail": "empty text or bot-authored message",
                        }
                    )
                    continue
                reason = hard_rejection_reason(candidate["text"], candidate["chat_title"])
                if reason:
                    screening_events.append(
                        {
                            "candidate": candidate,
                            "source": "poll",
                            "outcome": "screened",
                            "rejection_reason": local_rejection_code(reason),
                            "rejection_detail": reason,
                        }
                    )
                    continue
                candidates.append(candidate)
            await Database.save_chat_messages(stored)
            await self.store.save_screening_events(screening_events)
            await self.process_candidates(sorted(candidates, key=lambda row: row["message_id"]), source="poll")
            page_decision = history_page_decision(
                baseline,
                high_watermark,
                page_message_ids,
                HISTORY_LIMIT,
            )
            high_watermark = page_decision.high_watermark
            if page_decision.continue_backfill:
                await self.store.schedule_chat(
                    chat["chat_id"],
                    chat["tier"],
                    None,
                    page_decision.new_message_count,
                    history_offset_id=page_decision.next_offset_id,
                    history_high_watermark=high_watermark,
                    continue_backfill=True,
                )
            else:
                await self.store.schedule_chat(
                    chat["chat_id"],
                    chat["tier"],
                    page_decision.committed_last_seen,
                    page_decision.new_message_count,
                    history_offset_id=0,
                    history_high_watermark=0,
                )
            log_event(
                MODULE,
                "POLL_DONE",
                "Safe history request finished",
                session_name=session_name,
                role=role,
                chat=chat["title"],
                messages=len(stored),
                candidates=len(candidates),
                baseline=baseline,
                page_oldest=page_oldest,
                page_newest=page_newest,
                continue_backfill=page_decision.continue_backfill,
            )
        except FloodWait:
            raise
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid, UsernameInvalid) as exc:
            await self.store.schedule_chat(
                chat["chat_id"],
                TIER_QUARANTINE,
                None,
                0,
                error=str(exc),
                history_offset_id=offset_id,
                history_high_watermark=high_watermark,
            )
            log_event(MODULE, "POLL_CHAT_INVALID", "Chat moved to delayed retry", chat=chat["title"], error=exc)
        except Exception as exc:
            await self.store.schedule_chat(
                chat["chat_id"],
                chat["tier"],
                None,
                0,
                error=str(exc),
                history_offset_id=offset_id,
                history_high_watermark=high_watermark,
            )
            log_event(MODULE, "POLL_ERROR", "History request failed", chat=chat["title"], error=exc)

    async def handle_flood(self, session_name, wait_seconds):
        now = int(time.time())
        decision, account_until, global_until, flood_count = await self.store.record_flood(
            session_name, wait_seconds, now
        )
        await self.notifier.send_service(
            "MarginLeakRadar: Telegram FloodWait.\n"
            f"Аккаунт: {session_name}\n"
            f"Telegram wait: {wait_seconds} сек.\n"
            f"Аккаунт заблокирован до: {datetime.fromtimestamp(account_until)}\n"
            f"Общая пауза: {'до ' + str(datetime.fromtimestamp(global_until)) if global_until > now else 'нет; второй аккаунт продолжает работу'}\n"
            f"FloodWait за 24 часа: {flood_count}"
        )
        log_event(
            MODULE,
            "CIRCUIT_BREAKER",
            "FloodWait activated persistent protection",
            session_name=session_name,
            wait_seconds=wait_seconds,
            account_until=account_until,
            global_until=global_until,
            stop_for_day=decision.stop_polling_for_day,
        )

    async def polling_loop(self, session_name, role):
        while not self.stop_event.is_set():
            now = int(time.time())
            global_pause_until = await self.store.get_runtime_int("global_pause_until", 0)
            if global_pause_until > now:
                await asyncio.sleep(min(300, max(30, global_pause_until - now)))
                continue

            account = await self.store.account(session_name)
            if not account:
                await asyncio.sleep(NO_ACCOUNT_SLEEP)
                continue
            ready_at = max(int(account["cooldown_until"] or 0), int(account["blocked_until"] or 0))
            if ready_at > now:
                await asyncio.sleep(min(300, max(30, ready_at - now)))
                continue
            if int(account["next_request_at"] or 0) > now:
                await asyncio.sleep(min(300, max(30, int(account["next_request_at"]) - now)))
                continue

            cycle_started_at, cycle_limit = await self.store.ensure_account_cycle(session_name, now)
            if cycle_started_at > now:
                await asyncio.sleep(max(1, cycle_started_at - now))
                continue
            cycle_count = await self.store.cycle_request_count(session_name, cycle_started_at)
            hourly, daily = await self.store.request_counts(session_name, now)
            hard_hourly_limit = 21
            daily_limit = 240
            if cycle_count >= cycle_limit or hourly >= hard_hourly_limit or daily >= daily_limit:
                wait_seconds = await self.store.budget_wait_seconds(
                    session_name, hard_hourly_limit, daily_limit, now
                )
                if cycle_count >= cycle_limit:
                    wait_seconds = max(wait_seconds, cycle_started_at + 3600 - now)
                log_event(
                    MODULE,
                    "BUDGET_WAIT",
                    "Polling budget exhausted",
                    session_name=session_name,
                    cycle=f"{cycle_count}/{cycle_limit}",
                    hourly=f"{hourly}/{hard_hourly_limit}",
                    daily=f"{daily}/{daily_limit}",
                    wait_seconds=wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            chat = await self.store.lease_due_chat(session_name, now)
            if not chat:
                await asyncio.sleep(IDLE_SLEEP)
                continue

            app = await self.ensure_connected(session_name)
            if not app:
                await asyncio.sleep(NO_ACCOUNT_SLEEP)
                continue
            try:
                await self.poll_chat(app, session_name, role, chat)
            except FloodWait as exc:
                await self.handle_flood(session_name, exc.value)

    async def notify_live_list_once(self, live_chats):
        if await self.store.get_runtime_int("live_list_notified", 0):
            return
        links = "\n".join(
            f"- https://t.me/{chat['username']} — {chat['title']}" for chat in live_chats
        )
        await self.notifier.send_service(
            "15 live-чатов выбраны. Вступите в них вручную основным account_1; автоматического join нет:\n"
            + links
        )
        await self.store.set_runtime("live_list_notified", 1)

    async def run(self):
        await Database.init_schema()
        await self.store.init_schema()
        await self.validate_configuration()
        now = int(time.time())
        self.rollout_started_at = await self.store.ensure_rollout_started(now)
        hourly_limit, daily_limit = role_budget(
            ROLE_PRIMARY, self.rollout_started_at, now, MARGIN_SPEED
        )
        log_event(
            MODULE,
            "SPEED_PROFILE",
            "Margin radar speed profile is active",
            speed=MARGIN_SPEED,
            history_limit=HISTORY_LIMIT,
            hourly_limit=hourly_limit,
            daily_limit=daily_limit,
        )
        live_chats = await self.store.assign_chat_tiers(now)
        changed, passed, total = await self.store.regrade_recent_verifiers()
        log_event(
            MODULE,
            "VERIFIER_REGRADE",
            "Recent high-score leads were regraded with the active verifier policy",
            changed=changed,
            passed=passed,
            total=total,
        )
        await self.notifier.start()
        await self.notify_live_list_once(live_chats)
        await self.ensure_connected(PRIMARY_SESSION)
        await self.ensure_connected(RESERVE_SESSION)
        await self.refresh_memberships()
        consumer = asyncio.create_task(self.event_consumer())
        polling_primary = asyncio.create_task(self.polling_loop(PRIMARY_SESSION, ROLE_PRIMARY))
        polling_reserve = asyncio.create_task(self.polling_loop(RESERVE_SESSION, ROLE_RESERVE))
        membership = asyncio.create_task(self.membership_loop())
        ranking = asyncio.create_task(self.ranking_loop())
        try:
            while True:
                await self.ensure_connected(PRIMARY_SESSION)
                await self.ensure_connected(RESERVE_SESSION)
                await asyncio.sleep(60)
        finally:
            self.stop_event.set()
            polling_primary.cancel()
            polling_reserve.cancel()
            consumer.cancel()
            membership.cancel()
            ranking.cancel()
            await asyncio.gather(
                polling_primary,
                polling_reserve,
                consumer,
                membership,
                ranking,
                return_exceptions=True,
            )
            await self.close_clients()
            await self.notifier.close()


async def run_margin_radar():
    radar = SafeMarginRadar()
    await radar.run()


if __name__ == "__main__":
    try:
        asyncio.run(run_margin_radar())
    except KeyboardInterrupt:
        log_event(MODULE, "STOP", "MarginLeakRadar stopped")
