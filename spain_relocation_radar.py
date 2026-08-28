"""Continuous public Telegram radar for Spain relocation and SPb applications."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.raw.functions.contacts import Search

from config import Config
from networking import telegram_proxy
from spain_relocation_rules import (
    DISCOVERY_QUERIES,
    GLOBAL_MESSAGE_QUERIES,
    KNOWN_PUBLIC_CHATS,
    classify_spb_spain_case,
    looks_like_spain_relocation_source,
    relevant_excerpt,
    stable_case_key,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.getenv("SPAIN_DB_PATH") or PROJECT_ROOT / "data" / "spain_relocation.db")
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "spain_spb_cases.md"
DEFAULT_JSON = PROJECT_ROOT / "reports" / "spain_spb_cases.json"
LOCK_FILE = None


def _message_text(message) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def _message_link(username, message_id):
    return f"https://t.me/{str(username).lstrip('@')}/{message_id}" if username and message_id else None


def _chat_type(chat) -> str:
    return str(getattr(chat, "type", "unknown")).split(".")[-1].lower()


def _date(message):
    value = getattr(message, "date", None)
    if not value:
        return None
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value.isoformat()


def acquire_lock(path: Path):
    global LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE = open(path, "a+", encoding="utf-8")
    try:
        import msvcrt

        LOCK_FILE.seek(0)
        msvcrt.locking(LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, OSError) as exc:
        LOCK_FILE.close()
        LOCK_FILE = None
        raise RuntimeError("Spain radar is already running") from exc
    except ImportError:
        pass
    LOCK_FILE.seek(0)
    LOCK_FILE.truncate()
    LOCK_FILE.write(f"pid={os.getpid()} started_at={datetime.now().isoformat()}\n")
    LOCK_FILE.flush()


def release_lock():
    global LOCK_FILE
    if LOCK_FILE is None:
        return
    try:
        import msvcrt

        LOCK_FILE.seek(0)
        msvcrt.locking(LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    LOCK_FILE.close()
    LOCK_FILE = None


class SpainStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    username TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    title TEXT NOT NULL,
                    description TEXT,
                    type TEXT,
                    members INTEGER DEFAULT 0,
                    link TEXT,
                    status TEXT DEFAULT 'Active',
                    last_read_msg_id INTEGER DEFAULT 0,
                    last_checked_at INTEGER DEFAULT 0,
                    next_check_at INTEGER DEFAULT 0,
                    empty_reads INTEGER DEFAULT 0,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_spain_sources_due
                    ON sources(status, next_check_at, members DESC);

                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    chat_username TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    confirmed INTEGER DEFAULT 0,
                    evidence_kind TEXT,
                    visa_type TEXT,
                    msg_at TEXT,
                    chat_title TEXT,
                    message_link TEXT,
                    evidence_excerpt TEXT,
                    origin TEXT,
                    checks_json TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_username, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_spain_cases_quality
                    ON cases(confirmed DESC, evidence_kind, msg_at DESC);

                CREATE TABLE IF NOT EXISTS radar_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    def upsert_sources(self, sources):
        sources = list(sources or [])
        if not sources:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO sources
                    (username,chat_id,title,description,type,members,link,status,updated_at)
                VALUES (?,?,?,?,?,?,?,'Active',CURRENT_TIMESTAMP)
                ON CONFLICT(username) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    title=excluded.title,
                    description=excluded.description,
                    type=excluded.type,
                    members=excluded.members,
                    link=excluded.link,
                    status='Active',
                    updated_at=CURRENT_TIMESTAMP
                """,
                [
                    (
                        row["username"].lower(), row.get("chat_id"), row["title"], row.get("description"),
                        row.get("type"), row.get("members", 0), row.get("link"),
                    )
                    for row in sources
                ],
            )

    def due_sources(self, limit: int = 0):
        query = """
            SELECT username,chat_id,title,description,type,members,link,last_read_msg_id
            FROM sources
            WHERE status='Active' AND next_check_at<=?
            ORDER BY CASE WHEN last_read_msg_id=0 THEN 0 ELSE 1 END, members DESC, next_check_at
        """
        params = [int(time.time())]
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]

    def mark_checked(self, username: str, highest_msg_id: int, new_messages: int, delay_seconds: int):
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sources SET
                    last_read_msg_id=MAX(COALESCE(last_read_msg_id,0),?),
                    last_checked_at=?, next_check_at=?,
                    empty_reads=CASE WHEN ?>0 THEN 0 ELSE COALESCE(empty_reads,0)+1 END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE username=?
                """,
                (highest_msg_id, now, now + delay_seconds, new_messages, username.lower()),
            )

    def mark_source_error(self, username: str, delay_seconds: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE sources SET next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE username=?",
                (int(time.time()) + delay_seconds, username.lower()),
            )

    def upsert_cases(self, cases):
        cases = list(cases or [])
        if not cases:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO cases
                    (case_id,chat_username,message_id,confirmed,evidence_kind,visa_type,msg_at,
                     chat_title,message_link,evidence_excerpt,origin,checks_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                    confirmed=MAX(cases.confirmed,excluded.confirmed),
                    evidence_kind=CASE WHEN excluded.confirmed>=cases.confirmed THEN excluded.evidence_kind ELSE cases.evidence_kind END,
                    visa_type=excluded.visa_type,
                    msg_at=excluded.msg_at,
                    chat_title=excluded.chat_title,
                    message_link=excluded.message_link,
                    evidence_excerpt=excluded.evidence_excerpt,
                    origin=excluded.origin,
                    checks_json=excluded.checks_json,
                    last_seen_at=CURRENT_TIMESTAMP
                """,
                [
                    (
                        row["case_id"], row["chat_username"].lower(), row["message_id"], int(row["confirmed"]),
                        row["evidence_kind"], row["visa_type"], row.get("date"), row["chat"],
                        row.get("message_link"), row.get("evidence_excerpt"), row.get("origin"),
                        json.dumps(row.get("checks", {}), ensure_ascii=False),
                    )
                    for row in cases
                ],
            )

    def get_state(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM radar_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO radar_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def all_sources(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT chat_id,username,title,description,type,members,link FROM sources WHERE status='Active' ORDER BY members DESC,title"
            ).fetchall()
        return [dict(row) for row in rows]

    def all_cases(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT case_id,confirmed,evidence_kind,visa_type,msg_at AS date,chat_title AS chat,
                       chat_username,message_id,message_link,evidence_excerpt,origin,checks_json
                FROM cases
                ORDER BY confirmed DESC,msg_at DESC,case_id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["confirmed"] = bool(item["confirmed"])
            item["checks"] = json.loads(item.pop("checks_json") or "{}")
            result.append(item)
        return result

    def counts(self):
        with self.connect() as conn:
            sources = conn.execute("SELECT COUNT(*) FROM sources WHERE status='Active'").fetchone()[0]
            confirmed = conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
            candidates = conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=0").fetchone()[0]
        return {"sources": sources, "confirmed_cases": confirmed, "review_candidates": candidates}

    def import_snapshot_if_empty(self, path: Path | None):
        if not path or not path.exists() or self.counts()["sources"]:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.upsert_sources(payload.get("sources", []))
        self.upsert_cases(payload.get("cases", []))


async def _chat_record(app, identifier):
    try:
        chat = await app.get_chat(identifier)
    except RPCError:
        return None
    username = getattr(chat, "username", None)
    if not username:
        return None
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or username
    description = getattr(chat, "description", None) or getattr(chat, "bio", None) or ""
    if not looks_like_spain_relocation_source(title, description):
        return None
    return {
        "chat_id": getattr(chat, "id", None),
        "username": username,
        "title": title,
        "description": description,
        "type": _chat_type(chat),
        "members": int(getattr(chat, "members_count", 0) or 0),
        "link": f"https://t.me/{username}",
    }


async def discover_public_sources(app, max_queries: int | None = None):
    sources = {}
    for username in KNOWN_PUBLIC_CHATS:
        row = await _chat_record(app, username)
        if row:
            sources[row["username"].lower()] = row
    queries = DISCOVERY_QUERIES[:max_queries] if max_queries else DISCOVERY_QUERIES
    for query in queries:
        try:
            result = await app.invoke(Search(q=query, limit=100))
        except FloodWait:
            raise
        except RPCError:
            continue
        for raw_chat in list(getattr(result, "chats", []) or []):
            username = getattr(raw_chat, "username", None)
            if not username or username.lower() in sources:
                continue
            row = await _chat_record(app, username)
            if row:
                sources[row["username"].lower()] = row
    return sorted(sources.values(), key=lambda row: (-row["members"], row["title"].lower()))


def _case_from_message(message, source, origin):
    text = _message_text(message)
    classification = classify_spb_spain_case(text, source_is_spain_specific=True)
    if not classification["candidate"]:
        return None
    return {
        "case_id": stable_case_key(getattr(message.chat, "id", None), message.id),
        "confirmed": classification["confirmed"],
        "evidence_kind": classification["evidence_kind"],
        "visa_type": classification["visa_type"],
        "date": _date(message),
        "chat": source["title"],
        "chat_username": source["username"],
        "message_id": message.id,
        "message_link": _message_link(source["username"], message.id),
        "evidence_excerpt": relevant_excerpt(text),
        "origin": origin,
        "checks": {key: classification[key] for key in (
            "has_spb", "has_spain", "has_application", "first_person", "seeker_only", "direct_venue_match"
        )},
    }


async def scan_source_history(app, source, history_limit: int):
    rows = []
    last_read = int(source.get("last_read_msg_id") or 0)
    highest = last_read
    new_messages = 0
    async for message in app.get_chat_history(source["username"], limit=history_limit):
        if message.id <= last_read:
            break
        highest = max(highest, message.id)
        new_messages += 1
        row = _case_from_message(message, source, "source_history")
        if row:
            rows.append(row)
    return rows, highest, new_messages


async def global_case_search(app, source_map):
    rows = []
    search_global = getattr(app, "search_global", None)
    if not search_global:
        return rows
    for query in GLOBAL_MESSAGE_QUERIES:
        try:
            async for message in search_global(query, limit=100):
                chat = getattr(message, "chat", None)
                username = getattr(chat, "username", None)
                if not username:
                    continue
                key = username.lower()
                source = source_map.get(key)
                if not source:
                    source = await _chat_record(app, username)
                    if not source:
                        continue
                    source_map[key] = source
                row = _case_from_message(message, source, "global_search")
                if row:
                    rows.append(row)
        except FloodWait:
            raise
        except RPCError:
            continue
    return rows


def build_markdown(sources, cases):
    direct = [row for row in cases if row["confirmed"] and row["evidence_kind"] in {"direct_applicant", "close_relative"}]
    professional = [row for row in cases if row["confirmed"] and row["evidence_kind"] == "professional_case_report"]
    candidates = [row for row in cases if not row["confirmed"]]
    lines = [
        "# Испания: Telegram-чаты и подачи из Санкт-Петербурга", "",
        f"Обновлено: {datetime.now().astimezone().isoformat(timespec='seconds')}", "",
        "Исследованы только публично доступные Telegram-источники. Авторы обезличены; ссылки ведут на публичные сообщения.", "",
        f"Найдено публичных источников: **{len(sources)}**. "
        f"Прямых сообщений заявителей/семьи: **{len(direct)}**. "
        f"Профессиональных кейс-репортов: **{len(professional)}**. "
        f"Кандидатов для проверки: **{len(candidates)}**.", "",
        "## Публичные источники", "",
    ]
    for source in sources:
        members = f" — {source['members']} участников" if source.get("members") else ""
        lines.append(f"- [{source['title']}]({source['link']}) · {source.get('type','unknown')}{members}")
    lines.extend(["", "## Прямые сообщения заявителей или членов семьи", ""])
    if not direct:
        lines.append("Подтверждённых сообщений пока нет.")
    for row in direct:
        lines.extend([
            f"### {row['case_id']} · {row['visa_type']}", "",
            f"- Дата: {row.get('date') or 'не указана'}",
            f"- Источник: [{row['chat']}]({row['message_link']})",
            f"- Фрагмент: «{row['evidence_excerpt']}»", "",
        ])
    lines.extend(["## Профессиональные отчёты о конкретных кейсах", ""])
    if not professional:
        lines.append("Подтверждённых профессиональных кейс-репортов пока нет.")
    for row in professional:
        lines.extend([
            f"### {row['case_id']} · {row['visa_type']}", "",
            f"- Дата публикации: {row.get('date') or 'не указана'}",
            f"- Источник: [{row['chat']}]({row['message_link']})",
            f"- Фрагмент: «{row['evidence_excerpt']}»", "",
        ])
    lines.extend(["## Требуют ручной проверки", ""])
    if not candidates:
        lines.append("Кандидатов нет.")
    for row in candidates[:200]:
        lines.append(
            f"- [{row['case_id']}]({row['message_link']}) · {row['chat']} · "
            f"{row['visa_type']} · «{row['evidence_excerpt']}»"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_reports(store: SpainStore, output: Path, json_output: Path | None):
    sources = store.all_sources()
    cases = store.all_cases()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_markdown(sources, cases), encoding="utf-8")
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps({"sources": sources, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_cycle(app, store: SpainStore, args, force_discovery=False):
    now = int(time.time())
    last_discovery = int(store.get_state("last_discovery_at", "0") or 0)
    discovery_due = force_discovery or now - last_discovery >= args.discovery_interval
    discovered = globally_found = 0
    if discovery_due:
        print(json.dumps({"stage": "discovery_start"}, ensure_ascii=False), flush=True)
        sources = await discover_public_sources(app, args.max_search_queries)
        if args.max_chats:
            sources = sources[:args.max_chats]
        store.upsert_sources(sources)
        source_map = {row["username"].lower(): row for row in store.all_sources()}
        global_cases = await global_case_search(app, source_map)
        store.upsert_sources(source_map.values())
        store.upsert_cases(global_cases)
        store.set_state("last_discovery_at", now)
        discovered = len(sources)
        globally_found = len(global_cases)

    due = store.due_sources(args.max_chats_per_cycle)
    scanned = new_messages = new_cases = 0
    for index, source in enumerate(due, 1):
        print(json.dumps({
            "stage": "history", "chat": source["username"], "index": index, "total": len(due),
            "cursor": source.get("last_read_msg_id", 0),
        }, ensure_ascii=False), flush=True)
        try:
            cases, highest, count = await scan_source_history(app, source, args.history_limit)
            store.upsert_cases(cases)
            delay = args.active_recheck if count else args.idle_recheck
            store.mark_checked(source["username"], highest, count, delay)
            scanned += 1
            new_messages += count
            new_cases += len(cases)
        except FloodWait:
            raise
        except RPCError as exc:
            store.mark_source_error(source["username"], args.error_recheck)
            print(json.dumps({"stage": "chat_error", "chat": source["username"], "error": str(exc)}, ensure_ascii=False), flush=True)
        if index < len(due):
            await asyncio.sleep(args.chat_delay)

    write_reports(store, args.output, args.json_output)
    summary = {
        "stage": "cycle_done", "discovered_this_cycle": discovered,
        "global_matches_this_cycle": globally_found, "chats_scanned": scanned,
        "new_messages": new_messages, "matching_messages": new_cases,
        **store.counts(), "output": str(args.output.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


async def run(args):
    if not Config.SESSIONS:
        raise RuntimeError("No SESSION_STRING credentials configured")
    store = SpainStore(args.db)
    store.import_snapshot_if_empty(args.json_output)
    app = Client(
        "spain_relocation_research",
        session_string=Config.SESSIONS[0], api_id=Config.API_ID, api_hash=Config.API_HASH,
        proxy=telegram_proxy(), in_memory=True, no_updates=True,
    )
    async with app:
        first_cycle = True
        while True:
            try:
                await run_cycle(app, store, args, force_discovery=first_cycle)
            except FloodWait as exc:
                wait = int(exc.value) + 5
                print(json.dumps({"stage": "flood_wait", "sleep_seconds": wait}, ensure_ascii=False), flush=True)
                if args.once:
                    raise
                await asyncio.sleep(wait)
                continue
            except Exception as exc:
                print(json.dumps({"stage": "cycle_error", "error": str(exc), "retry_in": args.error_recheck}, ensure_ascii=False), flush=True)
                if args.once:
                    raise
                await asyncio.sleep(args.error_recheck)
                continue
            if args.once:
                return
            first_cycle = False
            print(json.dumps({"stage": "sleep", "seconds": args.cycle_seconds}, ensure_ascii=False), flush=True)
            await asyncio.sleep(args.cycle_seconds)


def parse_args():
    parser = argparse.ArgumentParser(description="Continuous Spain relocation radar for public Telegram sources")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--max-search-queries", type=int)
    parser.add_argument("--max-chats", type=int, default=0, help="limit stored discoveries; 0 means unlimited")
    parser.add_argument("--max-chats-per-cycle", type=int, default=0, help="limit due chats per cycle; 0 means all")
    parser.add_argument("--history-limit", type=int, default=2500)
    parser.add_argument("--chat-delay", type=float, default=1.0)
    parser.add_argument("--cycle-seconds", type=int, default=300)
    parser.add_argument("--discovery-interval", type=int, default=10800)
    parser.add_argument("--active-recheck", type=int, default=900)
    parser.add_argument("--idle-recheck", type=int, default=3600)
    parser.add_argument("--error-recheck", type=int, default=300)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--lock", type=Path, default=PROJECT_ROOT / "spain_relocation.lock")
    return parser.parse_args()


def main():
    args = parse_args()
    acquire_lock(args.lock)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Spain relocation radar stopped by user.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
