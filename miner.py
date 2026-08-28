import asyncio
import argparse
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, PeerIdInvalid, UsernameInvalid

from account_manager import AccountManager
from ai_engine import AIEngine
from database import Database
from logging_utils import log_event
from it_radar_rules import anonymize_author, normalize_wtp, pain_score
from runtime_settings import setting, speed_label
from quality_rules import (
    chat_activity_rejection_reason,
    clip_text,
    message_quality_counts,
    pain_rejection_reason,
    title_rejection_reason,
)


ai = AIEngine()
# Fresh evidence is primary; older rows already in the DB can still corroborate
# repeatability. A rolling horizon avoids silently expiring in future years.
HISTORY_HORIZON = datetime.now(timezone.utc) - timedelta(days=180)
MAX_HISTORY = setting("MINER_MAX_HISTORY")
MIN_CONFIDENCE = 0.70
CHUNK_CHAR_LIMIT = setting("MINER_CHUNK_CHAR_LIMIT")
CHATS_PER_ACCOUNT = setting("MINER_CHATS_PER_ACCOUNT")
ACCOUNT_COOLDOWN = setting("MINER_ACCOUNT_COOLDOWN")
NO_ACCOUNT_SLEEP = setting("MINER_NO_ACCOUNT_SLEEP")
IDLE_SLEEP = setting("MINER_IDLE_SLEEP")
CHUNK_SLEEP = setting("MINER_CHUNK_SLEEP")
JITTER_MIN = setting("MINER_JITTER_MIN")
JITTER_MAX = setting("MINER_JITTER_MAX")
RECHECK_EMPTY_SECONDS = setting("MINER_RECHECK_EMPTY_SECONDS")
RECHECK_FEW_SECONDS = setting("MINER_RECHECK_FEW_SECONDS")
RECHECK_ACTIVE_SECONDS = setting("MINER_RECHECK_ACTIVE_SECONDS")


def _message_text(message):
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


def telegram_chat_link(username):
    return f"https://t.me/{str(username).lstrip('@')}" if username else None


def author_display_name(author):
    if not author:
        return None
    parts = [
        getattr(author, "first_name", None),
        getattr(author, "last_name", None),
    ]
    return " ".join(part for part in parts if part) or None


def message_to_storage_row(message, chat_id, username, chat_title):
    text = _message_text(message)
    author = getattr(message, "from_user", None)
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
        "text": text,
        "raw_type": type(message).__name__,
        "msg_at": message_date,
        "has_text": bool(text),
        "is_bot": bool(getattr(author, "is_bot", False)),
    }


def build_context(records, target_id, window=2):
    ordered = sorted(records, key=lambda row: row["id"])
    index_by_id = {row["id"]: index for index, row in enumerate(ordered)}
    if target_id not in index_by_id:
        return ""

    index = index_by_id[target_id]
    start = max(0, index - window)
    end = min(len(ordered), index + window + 1)
    return "\n".join(f"[ID:{row['id']}] {row['text']}" for row in ordered[start:end])


def parse_business_pains(raw):
    try:
        data = json.loads(raw)
    except Exception as exc:
        log_event("MINER", "LLM_PARSE_ERROR", "Could not parse miner JSON", error=exc, raw_preview=clip_text(raw or "", 500))
        return []

    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        rows = [row for row in data.get("business_pains", []) if isinstance(row, dict)]
    else:
        rows = []
    log_event("MINER", "LLM_PARSE_DONE", "Parsed business pain candidates", candidates=len(rows))
    return rows


async def process_chat(app, chat_id, username, last_read_msg_id, chat_title=""):
    peer_target = username if username else chat_id
    chat_label = chat_title or username or str(chat_id)
    log_event(
        "MINER",
        "CHAT_START",
        "Starting chat mining",
        chat_id=chat_id,
        username=username,
        title=chat_label,
        last_read_msg_id=last_read_msg_id,
    )

    reason = title_rejection_reason(chat_label)
    if reason:
        log_event("MINER", "CHAT_REJECT_TITLE", "Rejected chat before history read", chat_id=chat_id, username=username, title=chat_label, reason=reason)
        return "REJECTED_CHAT", reason

    records = []
    highest_msg_id = last_read_msg_id
    raw_messages = 0
    skipped_old = 0
    skipped_pre_horizon = 0
    skipped_empty = 0
    stored_messages = []

    try:
        async for message in app.get_chat_history(peer_target, limit=MAX_HISTORY):
            raw_messages += 1
            if message.id <= last_read_msg_id:
                skipped_old += 1
                break

            highest_msg_id = max(highest_msg_id, message.id)
            stored_messages.append(message_to_storage_row(message, chat_id, username, chat_label))
            message_date = message.date.replace(tzinfo=timezone.utc) if message.date.tzinfo is None else message.date
            if message_date < HISTORY_HORIZON:
                skipped_pre_horizon += 1
                continue

            text = _message_text(message)
            if not text:
                skipped_empty += 1
                continue
            author = getattr(message, "from_user", None)

            records.append({
                "id": message.id,
                "text": clip_text(text, 900),
                "date": message_date,
                "author_id": getattr(author, "id", None),
                "author_username": getattr(author, "username", None),
                "author_name": author_display_name(author),
                "message_link": telegram_message_link(username, chat_id, message.id),
            })

        saved_messages = await Database.save_chat_messages(stored_messages)
        if stored_messages:
            newest_date = max((row.get("msg_at") for row in stored_messages if row.get("msg_at")), default=None)
            await Database.execute(
                """UPDATE target_chats SET
                   message_count_collected=(SELECT COUNT(*) FROM chat_messages WHERE chat_id=?),
                   last_activity=COALESCE(?,last_activity)
                   WHERE chat_id=?""",
                (chat_id, newest_date, chat_id),
            )

        counts = message_quality_counts([row["text"] for row in records])
        log_event(
            "MINER",
            "HISTORY_READ",
            "Chat history sampled",
            chat_id=chat_id,
            username=username,
            title=chat_label,
            raw_messages=raw_messages,
            usable_records=len(records),
            skipped_old=skipped_old,
            skipped_pre_horizon=skipped_pre_horizon,
            skipped_empty=skipped_empty,
            saved_messages=saved_messages,
            highest_msg_id=highest_msg_id,
            seller_hits=counts["seller"],
            pain_hits=counts["pain"],
            reject_hits=counts["reject"],
            noise_hits=counts["noise"],
            job_hits=counts["job"],
            logistics_order_hits=counts["logistics_order"],
        )

        if not records:
            await Database.update_chat_last_read_id(username, highest_msg_id)
            await Database.schedule_chat_recheck(username, RECHECK_EMPTY_SECONDS, 0, empty_increment=True)
            log_event(
                "MINER",
                "CHAT_EMPTY",
                "No new readable messages; delaying recheck",
                chat_id=chat_id,
                username=username,
                highest_msg_id=highest_msg_id,
                next_check_in_seconds=RECHECK_EMPTY_SECONDS,
            )
            return True, None

        content_reason = chat_activity_rejection_reason(chat_label, [row["text"] for row in records])
        if content_reason:
            if last_read_msg_id > 0 and len(records) < 15:
                await Database.update_chat_last_read_id(username, highest_msg_id)
                await Database.schedule_chat_recheck(username, RECHECK_FEW_SECONDS, len(records), empty_increment=False)
                log_event(
                    "MINER",
                    "CHAT_TOO_FEW_NEW",
                    "Too few new messages to judge already-known chat; delaying recheck",
                    chat_id=chat_id,
                    username=username,
                    reason=content_reason,
                    new_records=len(records),
                    highest_msg_id=highest_msg_id,
                    next_check_in_seconds=RECHECK_FEW_SECONDS,
                )
                return True, None
            await Database.update_chat_last_read_id(username, highest_msg_id)
            log_event("MINER", "CHAT_REJECT_CONTENT", "Rejected chat after history read", chat_id=chat_id, username=username, reason=content_reason)
            return "REJECTED_CHAT", content_reason

        chunks = []
        current_chunk = ""
        for row in records:
            line = f"[ID:{row['id']}] {row['text']}\n"
            if len(current_chunk) + len(line) > CHUNK_CHAR_LIMIT:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line
        if current_chunk:
            chunks.append(current_chunk)
        log_event("MINER", "CHUNKS_BUILT", "Prepared chunks for LLM", chat_id=chat_id, username=username, chunks=len(chunks), records=len(records))

        prompt = (
            "Ты evidence-first аналитик IT-болей. Найди сообщения русскоязычных IT-специалистов, где человек "
            "сталкивается с препятствием на пути к реальной цели. Ищи повторяющуюся ручную работу, потерю времени/денег, "
            "провал существующих инструментов, самодельный скрипт/таблицу/workflow, поиск помощи или оплаты. "
            "Pain-маркеры полезны для поиска, но не обязательны: 'каждое утро объединяю логи из трёх систем в Excel' — кандидат.\n"
            "Не включай простой вопрос по синтаксису, домашнее задание, разовый баг без повторяемости, вопрос новичка на 2 минуты, "
            "мем, спор о языках, чистую эмоцию, вакансию/рекламу или feature request без доказательства потребности. "
            "Различай technical question и repeatable expensive pain. Не предлагай решение и не подгоняй боль под AI/SaaS/бота. "
            "Не выдумывай значения: неизвестное возвращай null или 'unknown'. evidence_quotes — короткие точные фрагменты источника.\n\n"
            "Верни STRICT JSON {\"business_pains\":[{"
            "\"message_id\":int,\"problem_title\":str,\"profession_segment\":str|null,"
            "\"pain_summary\":str,\"desired_outcome\":str|null,\"current_workaround\":str|null,"
            "\"frequency\":\"unknown|once|monthly|weekly|daily\",\"estimated_time_cost\":str|null,"
            "\"estimated_money_cost\":str|null,\"urgency\":\"unknown|low|medium|high\","
            "\"emotional_intensity\":\"unknown|low|medium|high\",\"repeatability\":\"unknown|low|medium|high\","
            "\"manual_work_indicator\":bool,\"existing_tool_failure\":bool,"
            "\"solution_seeking_indicator\":bool,\"explicit_wtp_indicator\":bool,"
            "\"personal_case_indicator\":bool,\"is_advertisement\":bool,"
            "\"potential_wtp\":\"LOW|MEDIUM|HIGH\",\"pain_category\":str,"
            "\"evidence_quotes\":[str],\"confidence\":float}]}"
        )

        msg_by_id = {row["id"]: row for row in records}
        saved_candidates = 0
        rejected_candidates = 0
        duplicate_candidates = 0
        seen_keys = set()

        for chunk_index, chunk in enumerate(chunks, 1):
            log_event("MINER", "LLM_CHUNK_START", "Sending chunk to LLM", chat_id=chat_id, username=username, chunk_index=chunk_index, chunk_chars=len(chunk))
            raw, tokens = await ai.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
                model="llama-3.1-8b-instant",
                json_mode=True,
            )
            if not raw:
                log_event("MINER", "LLM_CHUNK_EMPTY", "LLM returned no response", chat_id=chat_id, username=username, chunk_index=chunk_index)
                continue

            candidates = parse_business_pains(raw)
            log_event(
                "MINER",
                "LLM_CHUNK_RESULT",
                "LLM chunk processed",
                chat_id=chat_id,
                username=username,
                chunk_index=chunk_index,
                candidates=len(candidates),
                tokens=tokens,
            )

            for candidate_index, candidate in enumerate(candidates, 1):
                try:
                    message_id = int(candidate.get("message_id"))
                except (TypeError, ValueError):
                    rejected_candidates += 1
                    log_event("MINER", "CANDIDATE_REJECT", "Invalid message_id", chat_id=chat_id, username=username, chunk_index=chunk_index, candidate_index=candidate_index, raw_id=candidate.get("message_id"))
                    continue

                source = msg_by_id.get(message_id)
                if not source:
                    rejected_candidates += 1
                    log_event("MINER", "CANDIDATE_REJECT", "LLM referenced unknown message id", chat_id=chat_id, username=username, message_id=message_id)
                    continue

                title = (candidate.get("problem_title") or "").strip()
                category = (candidate.get("pain_category") or candidate.get("category") or "other").strip()
                wtp_signal = normalize_wtp(candidate.get("potential_wtp") or candidate.get("wtp_signal"), source["text"])
                try:
                    confidence = float(candidate.get("confidence", 0))
                except (TypeError, ValueError):
                    confidence = 0

                evidence = source["text"]
                context = build_context(records, message_id)
                rejection_reason = pain_rejection_reason(title, evidence, context, chat_label)
                if rejection_reason:
                    rejected_candidates += 1
                    log_event("MINER", "CANDIDATE_REJECT", "Rejected by quality rules", chat_id=chat_id, username=username, message_id=message_id, title=title, reason=rejection_reason, confidence=confidence)
                    continue
                if confidence < MIN_CONFIDENCE:
                    rejected_candidates += 1
                    log_event("MINER", "CANDIDATE_REJECT", "Rejected by confidence threshold", chat_id=chat_id, username=username, message_id=message_id, title=title, confidence=confidence, threshold=MIN_CONFIDENCE)
                    continue
                if candidate.get("is_advertisement") or not candidate.get("personal_case_indicator"):
                    rejected_candidates += 1
                    log_event("MINER", "CANDIDATE_REJECT", "Not a personal/team affected case",
                              chat_id=chat_id, username=username, message_id=message_id, title=title)
                    continue

                key = (message_id, title.lower())
                if key in seen_keys:
                    duplicate_candidates += 1
                    log_event("MINER", "CANDIDATE_DUPLICATE", "Duplicate candidate in same chat run", chat_id=chat_id, username=username, message_id=message_id, title=title)
                    continue
                seen_keys.add(key)

                evidence_quotes = candidate.get("evidence_quotes") if isinstance(candidate.get("evidence_quotes"), list) else [evidence]
                raw_quotes = json.dumps(evidence_quotes, ensure_ascii=False)
                signal = {
                    **candidate,
                    "original_message": evidence,
                    "pain_summary": candidate.get("pain_summary") or title,
                    "potential_wtp": wtp_signal,
                }
                computed_pain_score = pain_score(signal)
                anonymized_id = anonymize_author(chat_id, source.get("author_id"))
                inserted_id = await Database.execute(
                    """
                    INSERT OR IGNORE INTO pains
                    (chat_id, message_id, trace_id, problem_title, category, wtp_signal,
                     raw_quotes, raw_llm_json, token_usage, msg_at, evidence_quote,
                     evidence_context, confidence, quality_score, source_title, source_username,
                     author_id, author_username, author_name, message_link, source_link,
                     author_anonymized_id, profession_segment, pain_summary, desired_outcome,
                     current_workaround, frequency, estimated_time_cost, estimated_money_cost,
                     urgency, emotional_intensity, repeatability, manual_work_indicator,
                     existing_tool_failure, solution_seeking_indicator, explicit_wtp_indicator,
                     potential_wtp, pain_score, evidence_quotes, personal_case_indicator,
                     is_advertisement)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        f"TRC-{uuid.uuid4().hex[:8]}",
                        title,
                        category,
                        wtp_signal,
                        raw_quotes,
                        raw,
                        tokens,
                        source["date"].isoformat(),
                        evidence,
                        context,
                        confidence,
                        confidence * 100,
                        chat_title,
                        username,
                        source.get("author_id"),
                        source.get("author_username"),
                        source.get("author_name"),
                        source.get("message_link"),
                        telegram_chat_link(username),
                        anonymized_id,
                        candidate.get("profession_segment"),
                        candidate.get("pain_summary") or title,
                        candidate.get("desired_outcome"),
                        candidate.get("current_workaround"),
                        candidate.get("frequency") or "unknown",
                        candidate.get("estimated_time_cost"),
                        candidate.get("estimated_money_cost"),
                        candidate.get("urgency") or "unknown",
                        candidate.get("emotional_intensity") or "unknown",
                        candidate.get("repeatability") or "unknown",
                        int(bool(candidate.get("manual_work_indicator"))),
                        int(bool(candidate.get("existing_tool_failure"))),
                        int(bool(candidate.get("solution_seeking_indicator"))),
                        int(bool(candidate.get("explicit_wtp_indicator"))),
                        wtp_signal,
                        computed_pain_score,
                        raw_quotes,
                        int(bool(candidate.get("personal_case_indicator"))),
                        int(bool(candidate.get("is_advertisement"))),
                    ),
                )
                saved_candidates += 1
                log_event(
                    "MINER",
                    "CANDIDATE_SAVED",
                    "Saved pain candidate",
                    pain_id=inserted_id,
                    chat_id=chat_id,
                    username=username,
                    message_id=message_id,
                    title=title,
                    category=category,
                    wtp_signal=wtp_signal,
                    confidence=confidence,
                    evidence_preview=clip_text(evidence, 180),
                )

            await asyncio.sleep(CHUNK_SLEEP)

        await Database.update_chat_last_read_id(username, highest_msg_id)
        next_delay = RECHECK_ACTIVE_SECONDS if saved_candidates else RECHECK_FEW_SECONDS
        await Database.schedule_chat_recheck(username, next_delay, len(records), empty_increment=False)
        log_event(
            "MINER",
            "CHAT_DONE",
            "Finished chat mining",
            chat_id=chat_id,
            username=username,
            title=chat_label,
            saved=saved_candidates,
            rejected=rejected_candidates,
            duplicates=duplicate_candidates,
            highest_msg_id=highest_msg_id,
            next_check_in_seconds=next_delay,
        )
        return True, None

    except FloodWait as exc:
        log_event("MINER", "FLOOD_WAIT", "Telegram flood wait while mining", chat_id=chat_id, username=username, value=exc.value)
        raise exc
    except (ChannelInvalid, ChannelPrivate, UsernameInvalid, PeerIdInvalid, KeyError, ValueError) as exc:
        log_event("MINER", "CHAT_INVALID", "Invalid or unavailable chat", chat_id=chat_id, username=username, title=chat_label, error=exc)
        return "INVALID", str(exc)
    except Exception as exc:
        log_event("MINER", "CHAT_ERROR", "Unknown chat mining error", chat_id=chat_id, username=username, title=chat_label, error=exc)
        return "ERROR", str(exc)


async def run_miner(run_once=False, max_chats=None):
    log_event(
        "MINER",
        "START",
        "Starting evidence-first miner",
        speed=speed_label(),
        max_history=MAX_HISTORY,
        chats_per_account=CHATS_PER_ACCOUNT,
        account_cooldown=ACCOUNT_COOLDOWN,
    )
    await Database.init_schema()

    while True:
        now_ts = int(time.time())
        ready_row = await Database.fetchone(
            """
            SELECT COUNT(*)
            FROM target_chats
            WHERE status = 'New'
               OR (status IN ('Processed', 'Error') AND COALESCE(next_check_at, 0) <= ?)
            """,
            (now_ts,),
        )
        ready_chats = int(ready_row[0] or 0) if ready_row else 0
        if ready_chats == 0:
            if run_once:
                return {"processed_chats": 0, "ready_chats": 0}
            next_row = await Database.fetchone(
                """
                SELECT MIN(next_check_at)
                FROM target_chats
                WHERE status IN ('Processed', 'Error') AND COALESCE(next_check_at, 0) > ?
                """,
                (now_ts,),
            )
            next_check_at = int(next_row[0] or 0) if next_row and next_row[0] else 0
            sleep_seconds = min(300, max(IDLE_SLEEP, next_check_at - now_ts if next_check_at else IDLE_SLEEP))
            log_event(
                "MINER",
                "QUEUE_DELAYED",
                "No chats are due for mining; not leasing Telegram account",
                sleep_seconds=sleep_seconds,
                next_check_in_seconds=max(0, next_check_at - now_ts) if next_check_at else None,
            )
            await asyncio.sleep(sleep_seconds)
            continue

        app, session_name = await AccountManager.get_available_client()
        if not app:
            if run_once:
                return {"processed_chats": 0, "ready_chats": ready_chats, "error": "no available Telegram account"}
            log_event("MINER", "NO_ACCOUNT", "No free accounts; sleeping", sleep_seconds=NO_ACCOUNT_SLEEP)
            await asyncio.sleep(NO_ACCOUNT_SLEEP)
            continue

        log_event("MINER", "ACCOUNT", "Using account", session_name=session_name)
        try:
            async with app:
                chats_processed = 0
                idle_attempts = 0

                chat_limit = max_chats or CHATS_PER_ACCOUNT
                while chats_processed < chat_limit:
                    now_ts = int(time.time())
                    rows = await Database.fetchall(
                        """
                        SELECT id, chat_id, username, title
                        FROM target_chats
                        WHERE status = 'New'
                           OR (status IN ('Processed', 'Error') AND COALESCE(next_check_at, 0) <= ?)
                        ORDER BY CASE WHEN status = 'New' THEN 0 ELSE 1 END,
                                 COALESCE(quality_score, 0) DESC,
                                 COALESCE(last_new_messages, 0) DESC,
                                 RANDOM()
                        LIMIT 1
                        """,
                        (now_ts,),
                    )

                    if not rows:
                        idle_attempts += 1
                        log_event("MINER", "QUEUE_EMPTY", "No chat available", idle_attempt=idle_attempts)
                        if idle_attempts > 5:
                            break
                        await asyncio.sleep(IDLE_SLEEP)
                        continue

                    idle_attempts = 0
                    row_id, chat_id, username, title = rows[0]
                    log_event("MINER", "QUEUE_PICK", "Selected chat from queue", row_id=row_id, chat_id=chat_id, username=username, title=title)

                    if not username:
                        await Database.execute(
                            "UPDATE target_chats SET status = 'Skipped_Private', rejection_reason = ? WHERE id = ?",
                            ("missing username", row_id),
                        )
                        log_event("MINER", "QUEUE_SKIP", "Skipped chat without username", row_id=row_id, chat_id=chat_id, title=title)
                        continue

                    last_msg_id = await Database.get_chat_last_read_id(username)

                    try:
                        status, reason = await process_chat(app, chat_id, username, last_msg_id, title or "")
                        if status is True:
                            chats_processed += 1
                            await Database.execute(
                                """
                                UPDATE target_chats
                                SET status = 'Processed', rejection_reason = NULL, last_checked_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                                """,
                                (row_id,),
                            )
                            log_event("MINER", "QUEUE_STATUS", "Chat marked processed", row_id=row_id, chat_id=chat_id, username=username)
                        elif status == "REJECTED_CHAT":
                            await Database.execute(
                                """
                                UPDATE target_chats
                                SET status = 'Rejected', rejection_reason = ?, last_checked_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                                """,
                                (reason, row_id),
                            )
                            log_event("MINER", "QUEUE_STATUS", "Chat rejected and removed from mining queue", row_id=row_id, chat_id=chat_id, username=username, reason=reason)
                        elif status == "INVALID":
                            await Database.execute(
                                "UPDATE target_chats SET status = 'Invalid', rejection_reason = ? WHERE id = ?",
                                (reason, row_id),
                            )
                            log_event("MINER", "QUEUE_STATUS", "Chat marked invalid", row_id=row_id, chat_id=chat_id, username=username, reason=reason)
                        elif status == "ERROR":
                            await Database.execute(
                                "UPDATE target_chats SET status = 'Error', rejection_reason = ? WHERE id = ?",
                                (reason, row_id),
                            )
                            log_event("MINER", "QUEUE_STATUS", "Chat marked error", row_id=row_id, chat_id=chat_id, username=username, reason=reason)

                    except FloodWait as exc:
                        await AccountManager.report_flood_wait(session_name, exc.value)
                        break

                    if not run_once or chats_processed < chat_limit:
                        jitter = random.uniform(JITTER_MIN, JITTER_MAX)
                        log_event("MINER", "JITTER", "Sleeping between chats", seconds=round(jitter, 1))
                        await asyncio.sleep(jitter)

                if chats_processed >= chat_limit and not run_once:
                    log_event("MINER", "ACCOUNT_COOLDOWN", "Processed chat budget reached; cooling account", session_name=session_name, chats_processed=chats_processed)
                    await AccountManager.apply_soft_cooldown(session_name, ACCOUNT_COOLDOWN)
                if run_once:
                    return {"processed_chats": chats_processed, "ready_chats": ready_chats}
        finally:
            await AccountManager.release_account(session_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect messages and extract structured IT pain signals")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-chats", type=int, default=None)
    args = parser.parse_args()
    try:
        result = asyncio.run(run_miner(run_once=args.once, max_chats=args.max_chats))
        if args.once:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        log_event("MINER", "STOP", "Miner stopped by keyboard interrupt")
