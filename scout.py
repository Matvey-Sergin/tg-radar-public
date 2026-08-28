import argparse
import asyncio
import json
import random
import re
from datetime import datetime, timezone

from pyrogram.errors import FloodWait
from pyrogram.raw.functions.contacts import Search

from account_manager import AccountManager
from ai_engine import AIEngine
from database import Database
from it_radar_rules import IT_SEARCH_QUERIES, IT_SEGMENTS
from logging_utils import log_event
from quality_rules import chat_activity_rejection_reason, message_quality_counts, title_rejection_reason
from runtime_settings import setting, speed_label


ai = AIEngine()
REQUESTS_PER_ACCOUNT = setting("SCOUT_REQUESTS_PER_ACCOUNT")
ACCOUNT_COOLDOWN = setting("SCOUT_ACCOUNT_COOLDOWN")
NO_ACCOUNT_SLEEP = setting("SCOUT_NO_ACCOUNT_SLEEP")
KEYWORD_GEN_SLEEP = setting("SCOUT_KEYWORD_GEN_SLEEP")
SAVED_CHAT_SLEEP = setting("SCOUT_SAVED_CHAT_SLEEP")
ERROR_SLEEP = setting("SCOUT_ERROR_SLEEP")
JITTER_MIN = setting("SCOUT_JITTER_MIN")
JITTER_MAX = setting("SCOUT_JITTER_MAX")
SEED_KEYS = IT_SEARCH_QUERIES


def _message_text(message):
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


async def generate_keywords():
    past_rows = await Database.fetchall("SELECT keyword FROM keywords ORDER BY id DESC LIMIT 150")
    past_keys = [row[0] for row in past_rows]
    prompt = (
        "Сгенерируй 50 коротких русскоязычных запросов для поиска живых Telegram-чатов IT-специалистов. "
        "Нужны профессиональные дискуссии middle+/senior: разработка, DevOps/SRE, QA, data/ML, "
        "архитектура, engineering management, remote/relocation, собеседования, freelancing, CI/CD, "
        "debugging, observability, legacy и developer productivity. Не предлагай каналы новостей, "
        "курсы, крипту или доски вакансий без обсуждений. Формат: JSON {\"queries\":[строки]}.\n"
        f"Не повторяй: {json.dumps(past_keys, ensure_ascii=False)}"
    )
    raw, _ = await ai.chat([{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", json_mode=True)
    try:
        values = json.loads(raw or "{}").get("queries", [])
    except (ValueError, AttributeError):
        values = []
    accepted = []
    for value in values:
        keyword = re.sub(r"^[\d.\-*\s]+", "", str(value)).strip().strip("\"'")
        if 4 <= len(keyword) <= 64 and title_rejection_reason(keyword) is None:
            accepted.append(keyword)
    return sorted(set(accepted))


async def is_it_chat_llm(title, description, sample_text):
    prompt = (
        "Ответь JSON {\"accept\":bool,\"topic\":str,\"audience_segment\":str,\"reason\":str}. "
        "Прими только живой русскоязычный Telegram-чат, где IT-специалисты обсуждают свою работу и "
        "реальные профессиональные проблемы. Отклони новостной канал, одни вакансии, рекламу, курсы, "
        "крипту, студенческий флуд, ботов и мемы. Не требуй явных слов боли: оцени характер дискуссии.\n"
        f"Название: {title}\nОписание: {description or 'нет'}\nСообщения:\n{sample_text[:4000]}"
    )
    raw, _ = await ai.chat([{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", json_mode=True)
    try:
        result = json.loads(raw or "{}")
        return bool(result.get("accept")), result
    except (ValueError, AttributeError):
        return False, {"reason": "invalid LLM assessment"}


async def check_chat(app, identifier, title):
    reason = title_rejection_reason(title)
    if reason:
        return None, reason
    try:
        details = await app.get_chat(identifier)
        chat_type = str(getattr(details, "type", "")).lower()
        if "channel" in chat_type and "supergroup" not in chat_type:
            return None, "broadcast channel instead of discussion chat"

        messages = []
        raw_count = bot_count = 0
        async for message in app.get_chat_history(identifier, limit=160):
            raw_count += 1
            text = _message_text(message)
            author = getattr(message, "from_user", None)
            if not text:
                continue
            if getattr(author, "is_bot", False):
                bot_count += 1
                continue
            messages.append(message)

        texts = [_message_text(message) for message in messages]
        reason = chat_activity_rejection_reason(title, texts)
        if reason:
            return None, reason
        counts = message_quality_counts(texts)
        authors = {getattr(message.from_user, "id", None) for message in messages if getattr(message, "from_user", None)}
        replies = sum(bool(getattr(message, "reply_to_message_id", None)) for message in messages)
        questions = sum("?" in text for text in texts)
        reply_ratio = replies / max(len(messages), 1)
        question_ratio = questions / max(len(messages), 1)
        bot_ratio = bot_count / max(raw_count, 1)
        dates = [message.date for message in messages if getattr(message, "date", None)]
        newest = max(dates) if dates else None
        if newest and newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - newest).days if newest else 999
        freshness = max(0, 1 - age_days / 30)
        substantive_ratio = counts["substantive"] / max(len(texts), 1)
        activity = min(100, round(45 * freshness + 35 * min(1, len(messages) / 120) + 20 * min(1, len(authors) / 20), 1))
        quality_score = max(0, min(100, round(
            25 * substantive_ratio + 20 * min(1, len(authors) / 20) + 15 * min(1, reply_ratio / .15) +
            10 * min(1, question_ratio / .15) + 20 * freshness + 10 * min(1, counts["it"] / max(len(texts), 1) / .25) -
            25 * bot_ratio, 1
        )))
        if reply_ratio < .01 or len(authors) < 4 or quality_score < 35:
            return None, "weak discussion or low chat quality score"

        description = getattr(details, "description", None) or getattr(details, "bio", None)
        accepted, llm = await is_it_chat_llm(title, description, "\n".join(texts[:35]))
        if not accepted:
            return None, llm.get("reason") or "LLM rejected community"
        assessment = {
            "description": description,
            "estimated_topic": llm.get("topic") or "IT",
            "audience_segment": llm.get("audience_segment") or "IT professionals",
            "activity": activity,
            "message_count_collected": len(messages),
            "last_activity": newest.isoformat() if newest else None,
            "member_count": getattr(details, "members_count", 0) or 0,
            "quality_score": quality_score,
            "quality_details": json.dumps({
                "unique_authors": len(authors), "reply_ratio": round(reply_ratio, 3),
                "question_ratio": round(question_ratio, 3), "substantive_ratio": round(substantive_ratio, 3),
                "bot_ratio": round(bot_ratio, 3), "llm": llm,
            }, ensure_ascii=False),
        }
        return assessment, None
    except Exception as exc:
        return None, str(exc)


async def discovery_pass(app, max_keywords):
    requests_done = accepted_count = 0
    while requests_done < max_keywords:
        row = await Database.fetchone("SELECT id, keyword FROM keywords WHERE used = 0 ORDER BY id LIMIT 1")
        if not row:
            generated = await generate_keywords()
            for keyword in generated:
                await Database.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
            if not generated:
                break
            continue
        row_id, keyword = row
        result = await app.invoke(Search(q=keyword, limit=50))
        requests_done += 1
        for chat in list(getattr(result, "chats", []) or []):
            username = getattr(chat, "username", None)
            title = getattr(chat, "title", "") or ""
            chat_id = getattr(chat, "id", None)
            if not username or chat_id is None:
                continue
            if await Database.fetchone("SELECT 1 FROM target_chats WHERE chat_id = ?", (chat_id,)):
                continue
            assessment, reason = await check_chat(app, username, title)
            if assessment:
                await Database.execute(
                    """INSERT INTO target_chats
                    (chat_id,title,username,status,quality_score,rejection_reason,description,estimated_topic,
                     activity,message_count_collected,last_activity,audience_segment,quality_details,member_count)
                    VALUES (?,?,?,'New',?,NULL,?,?,?,?,?,?,?,?)""",
                    (chat_id, title, username, assessment["quality_score"], assessment["description"],
                     assessment["estimated_topic"], assessment["activity"], assessment["message_count_collected"],
                     assessment["last_activity"], assessment["audience_segment"], assessment["quality_details"],
                     assessment["member_count"]),
                )
                accepted_count += 1
                await asyncio.sleep(SAVED_CHAT_SLEEP)
            else:
                log_event("SCOUT", "CHAT_REJECT", "Rejected IT chat candidate", title=title, username=username, reason=reason)
        await Database.execute("UPDATE keywords SET used = 1 WHERE id = ?", (row_id,))
        if requests_done < max_keywords:
            await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
    return {"queries": requests_done, "accepted_chats": accepted_count}


async def run_scout(run_once=False, max_keywords=None):
    await Database.init_schema()
    for keyword in SEED_KEYS:
        await Database.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
    limit = max_keywords or REQUESTS_PER_ACCOUNT
    while True:
        app, session_name = await AccountManager.get_available_client()
        if not app:
            if run_once:
                return {"queries": 0, "accepted_chats": 0, "error": "no available Telegram account"}
            await asyncio.sleep(NO_ACCOUNT_SLEEP)
            continue
        try:
            async with app:
                result = await discovery_pass(app, limit)
                log_event("SCOUT", "PASS_DONE", "IT chat discovery pass finished", **result)
                if run_once:
                    return result
                await AccountManager.apply_soft_cooldown(session_name, ACCOUNT_COOLDOWN)
        except FloodWait as exc:
            await AccountManager.report_flood_wait(session_name, exc.value)
            if run_once:
                return {"queries": 0, "accepted_chats": 0, "error": f"FloodWait {exc.value}"}
        except Exception as exc:
            log_event("SCOUT", "PASS_ERROR", "IT discovery failed", error=exc)
            if run_once:
                return {"queries": 0, "accepted_chats": 0, "error": str(exc)}
            await asyncio.sleep(ERROR_SLEEP)
        finally:
            await AccountManager.release_account(session_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover professional IT Telegram chats")
    parser.add_argument("--once", action="store_true", help="run one bounded discovery pass")
    parser.add_argument("--max-keywords", type=int, default=None)
    args = parser.parse_args()
    result = asyncio.run(run_scout(run_once=args.once, max_keywords=args.max_keywords))
    if args.once:
        print(json.dumps(result, ensure_ascii=False, indent=2))
