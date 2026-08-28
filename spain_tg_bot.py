import asyncio
import html
import os
import sqlite3
import time
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import Config, PROJECT_ROOT
from networking import telegram_proxy_url


DB_PATH = Path(os.getenv("SPAIN_DB_PATH") or PROJECT_ROOT / "data" / "spain_relocation.db")
SOURCE_PAGE_SIZE = 8
CASE_PAGE_SIZE = 5
NOTIFICATION_BATCH_SIZE = 6
NOTIFICATION_POLL_SECONDS = 30

router = Router()


def connect_db(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def initialize_notification_store(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spain_bot_notifications (
                case_id TEXT PRIMARY KEY,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def fetch_status(path=DB_PATH):
    now = int(time.time())
    with closing(connect_db(path)) as conn:
        if not table_exists(conn, "sources") or not table_exists(conn, "cases"):
            return None
        values = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM sources) AS sources,
                (SELECT COUNT(*) FROM cases WHERE confirmed=1) AS confirmed,
                (SELECT COUNT(*) FROM cases WHERE confirmed=0) AS candidates,
                (SELECT COUNT(*) FROM sources WHERE status='Active' AND next_check_at<=?) AS due,
                (SELECT MAX(last_checked_at) FROM sources) AS last_checked_at,
                (SELECT MAX(first_seen_at) FROM cases) AS last_found_at
            """,
            (now,),
        ).fetchone()
    return dict(values)


def fetch_sources(page, path=DB_PATH):
    offset = max(0, page) * SOURCE_PAGE_SIZE
    with closing(connect_db(path)) as conn:
        if not table_exists(conn, "sources"):
            return [], 0
        total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        rows = conn.execute(
            """
            SELECT username,title,members,link,status,last_checked_at
            FROM sources
            ORDER BY status='Active' DESC,members DESC,title COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,
            (SOURCE_PAGE_SIZE, offset),
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def fetch_cases(kind, page=0, path=DB_PATH):
    where = ""
    params = []
    if kind == "confirmed":
        where = "WHERE confirmed=1"
    elif kind == "candidates":
        where = "WHERE confirmed=0"
    offset = max(0, page) * CASE_PAGE_SIZE
    with closing(connect_db(path)) as conn:
        if not table_exists(conn, "cases"):
            return [], 0
        total = conn.execute(f"SELECT COUNT(*) FROM cases {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT case_id,confirmed,evidence_kind,visa_type,msg_at,chat_title,
                   message_link,evidence_excerpt,first_seen_at
            FROM cases {where}
            ORDER BY first_seen_at DESC,msg_at DESC,case_id
            LIMIT ? OFFSET ?
            """,
            (*params, CASE_PAGE_SIZE, offset),
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def fetch_case(case_id, path=DB_PATH):
    with closing(connect_db(path)) as conn:
        if not table_exists(conn, "cases"):
            return None
        row = conn.execute(
            """
            SELECT case_id,confirmed,evidence_kind,visa_type,msg_at,chat_title,
                   chat_username,message_id,message_link,evidence_excerpt,origin
            FROM cases WHERE case_id=?
            """,
            (case_id,),
        ).fetchone()
    return dict(row) if row else None


def fetch_unsent_cases(limit=NOTIFICATION_BATCH_SIZE, path=DB_PATH):
    initialize_notification_store(path)
    with closing(connect_db(path)) as conn:
        if not table_exists(conn, "cases"):
            return []
        rows = conn.execute(
            """
            SELECT c.case_id,c.confirmed,c.evidence_kind,c.visa_type,c.msg_at,
                   c.chat_title,c.message_link,c.evidence_excerpt,c.first_seen_at
            FROM cases c
            LEFT JOIN spain_bot_notifications n ON n.case_id=c.case_id
            WHERE n.case_id IS NULL
            ORDER BY c.confirmed DESC,c.first_seen_at,c.case_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_notified(case_ids, path=DB_PATH):
    if not case_ids:
        return
    initialize_notification_store(path)
    with closing(connect_db(path)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO spain_bot_notifications(case_id) VALUES (?)",
            [(case_id,) for case_id in case_ids],
        )
        conn.commit()


def esc(value):
    return html.escape(str(value or "не указано"))


def short(value, limit=230):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def format_time(value):
    if value in (None, ""):
        return "никогда"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value)).astimezone().strftime("%d.%m.%Y %H:%M")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, OSError):
        return str(value)


def evidence_label(value):
    return {
        "direct_applicant": "личный опыт заявителя",
        "close_relative": "опыт близкого родственника",
        "professional_case_report": "профессиональный кейс",
    }.get(value, "требует ручной проверки")


def authorized(user_id):
    # The bot interface is public: Telegram itself always supplies the user id.
    # ADMIN_ID is used only as the destination for automatic radar alerts.
    return bool(user_id)


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус радара"), KeyboardButton(text="💬 Все чаты")],
            [KeyboardButton(text="✅ Подтверждённые"), KeyboardButton(text="🧐 Кандидаты")],
            [KeyboardButton(text="🆕 Последние")],
        ],
        resize_keyboard=True,
    )


def page_keyboard(prefix, page, total, page_size):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="← Назад", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * page_size < total:
        buttons.append(InlineKeyboardButton(text="Дальше →", callback_data=f"{prefix}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


def case_page_keyboard(kind, page, rows, total):
    keyboard = [
        [InlineKeyboardButton(text=f"Открыть {index + 1}", callback_data=f"case:{row['case_id']}")]
        for index, row in enumerate(rows)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"cases:{kind}:{page - 1}"))
    if (page + 1) * CASE_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Дальше →", callback_data=f"cases:{kind}:{page + 1}"))
    if nav:
        keyboard.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def render_case_page(kind, page, rows, total):
    titles = {
        "confirmed": "✅ Подтверждённые случаи",
        "candidates": "🧐 Требуют проверки",
        "latest": "🆕 Последние находки",
    }
    lines = [f"<b>{titles[kind]}</b>", f"Всего: {total}. Страница {page + 1}."]
    for index, row in enumerate(rows, start=1):
        marker = "✅" if row["confirmed"] else "🧐"
        lines.extend(
            [
                "",
                f"{index}. {marker} <b>{esc(row['visa_type'])}</b>",
                f"{esc(row['chat_title'])} · {esc(format_time(row['msg_at']))}",
                f"<i>{esc(short(row['evidence_excerpt'], 180))}</i>",
            ]
        )
    if not rows:
        lines.append("\nПока ничего нет.")
    return "\n".join(lines)


def render_notification(rows):
    confirmed = sum(bool(row["confirmed"]) for row in rows)
    candidates = len(rows) - confirmed
    lines = [
        "<b>🇪🇸 Spain Radar: обнаружены записи</b>",
        f"В этой подборке: подтверждённых {confirmed}, на проверку {candidates}.",
    ]
    for row in rows:
        marker = "✅" if row["confirmed"] else "🧐"
        link = row.get("message_link")
        source = esc(row.get("chat_title"))
        source_text = f'<a href="{html.escape(link, quote=True)}">{source}</a>' if link else source
        lines.extend(
            [
                "",
                f"{marker} <b>{esc(row.get('visa_type'))}</b> · {source_text}",
                f"<i>{esc(short(row.get('evidence_excerpt'), 190))}</i>",
            ]
        )
    return "\n".join(lines)


async def reject_if_needed(event):
    user = event.from_user
    if authorized(user.id if user else 0):
        return False
    if isinstance(event, CallbackQuery):
        await event.answer("Нет доступа", show_alert=True)
    else:
        await event.answer("Не удалось определить ваш Telegram ID. Отправьте /start ещё раз.")
    return True


@router.message(CommandStart())
async def start(message: Message):
    if await reject_if_needed(message):
        return
    await message.answer(
        "<b>🇪🇸 Spain Relocation Radar</b>\n\n"
        "Здесь видны найденные чаты, состояние радара и все сообщения о подаче "
        "в Испанию из Санкт-Петербурга. Новые находки приходят автоматически.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


@router.message(Command("status"))
@router.message(F.text == "📊 Статус радара")
async def status(message: Message):
    if await reject_if_needed(message):
        return
    data = await asyncio.to_thread(fetch_status)
    if not data:
        await message.answer("База радара ещё не создана. Запустите Spain-режим через Nexus.")
        return
    last_check = data["last_checked_at"]
    active = bool(last_check) and int(time.time()) - int(last_check) < 20 * 60
    state = "🟢 работает" if active else "🟠 давно не отмечался"
    await message.answer(
        "<b>📊 Состояние радара</b>\n\n"
        f"Статус: <b>{state}</b>\n"
        f"Найдено чатов: <b>{data['sources']}</b>\n"
        f"Подтверждённых случаев: <b>{data['confirmed']}</b>\n"
        f"Кандидатов на проверку: <b>{data['candidates']}</b>\n"
        f"Чатов к ближайшей проверке: <b>{data['due']}</b>\n"
        f"Последняя проверка чата: {esc(format_time(last_check))}\n"
        f"Последняя находка: {esc(format_time(data['last_found_at']))}",
        parse_mode=ParseMode.HTML,
    )


async def send_sources(target, page, edit=False):
    rows, total = await asyncio.to_thread(fetch_sources, page)
    max_page = max(0, (total - 1) // SOURCE_PAGE_SIZE)
    page = min(max(0, page), max_page)
    if page != 0 and not rows:
        rows, total = await asyncio.to_thread(fetch_sources, page)
    lines = ["<b>💬 Найденные Telegram-чаты</b>", f"Всего: {total}. Страница {page + 1}."]
    for index, row in enumerate(rows, start=page * SOURCE_PAGE_SIZE + 1):
        link = row.get("link")
        title = esc(row.get("title"))
        title = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else title
        status_icon = "🟢" if row.get("status") == "Active" else "⚪"
        lines.append(
            f"\n{index}. {status_icon} {title}\n"
            f"Участников: {int(row.get('members') or 0):,} · проверен {esc(format_time(row.get('last_checked_at')))}"
        )
    if not rows:
        lines.append("\nЧаты пока не найдены.")
    kwargs = {
        "text": "\n".join(lines),
        "parse_mode": ParseMode.HTML,
        "reply_markup": page_keyboard("src", page, total, SOURCE_PAGE_SIZE),
        "disable_web_page_preview": True,
    }
    if edit:
        await target.edit_text(**kwargs)
    else:
        await target.answer(**kwargs)


@router.message(Command("chats"))
@router.message(F.text == "💬 Все чаты")
async def sources(message: Message):
    if await reject_if_needed(message):
        return
    await send_sources(message, 0)


@router.callback_query(F.data.startswith("src:"))
async def sources_page(callback: CallbackQuery):
    if await reject_if_needed(callback):
        return
    await send_sources(callback.message, int(callback.data.rsplit(":", 1)[1]), edit=True)
    await callback.answer()


async def send_case_page(target, kind, page, edit=False):
    rows, total = await asyncio.to_thread(fetch_cases, kind, page)
    max_page = max(0, (total - 1) // CASE_PAGE_SIZE)
    page = min(max(0, page), max_page)
    if page != 0 and not rows:
        rows, total = await asyncio.to_thread(fetch_cases, kind, page)
    kwargs = {
        "text": render_case_page(kind, page, rows, total),
        "parse_mode": ParseMode.HTML,
        "reply_markup": case_page_keyboard(kind, page, rows, total),
        "disable_web_page_preview": True,
    }
    if edit:
        await target.edit_text(**kwargs)
    else:
        await target.answer(**kwargs)


@router.message(Command("confirmed"))
@router.message(F.text == "✅ Подтверждённые")
async def confirmed(message: Message):
    if await reject_if_needed(message):
        return
    await send_case_page(message, "confirmed", 0)


@router.message(Command("candidates"))
@router.message(F.text == "🧐 Кандидаты")
async def candidates(message: Message):
    if await reject_if_needed(message):
        return
    await send_case_page(message, "candidates", 0)


@router.message(Command("latest"))
@router.message(F.text == "🆕 Последние")
async def latest(message: Message):
    if await reject_if_needed(message):
        return
    await send_case_page(message, "latest", 0)


@router.callback_query(F.data.startswith("cases:"))
async def cases_page(callback: CallbackQuery):
    if await reject_if_needed(callback):
        return
    _, kind, page = callback.data.split(":")
    await send_case_page(callback.message, kind, int(page), edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("case:"))
async def case_detail(callback: CallbackQuery):
    if await reject_if_needed(callback):
        return
    row = await asyncio.to_thread(fetch_case, callback.data.split(":", 1)[1])
    if not row:
        await callback.answer("Запись уже недоступна", show_alert=True)
        return
    marker = "✅ Подтверждено" if row["confirmed"] else "🧐 Нужна ручная проверка"
    text = (
        f"<b>{marker}</b>\n\n"
        f"Тип визы: <b>{esc(row['visa_type'])}</b>\n"
        f"Основание: {esc(evidence_label(row['evidence_kind']))}\n"
        f"Дата сообщения: {esc(format_time(row['msg_at']))}\n"
        f"Источник: {esc(row['chat_title'])}\n\n"
        f"<b>Фрагмент</b>\n<i>{esc(short(row['evidence_excerpt'], 1200))}</i>"
    )
    keyboard = None
    if row.get("message_link"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть исходное сообщение ↗", url=row["message_link"])]
            ]
        )
    await callback.message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.message()
async def fallback(message: Message):
    if await reject_if_needed(message):
        return
    await message.answer("Выберите раздел на клавиатуре ниже.", reply_markup=main_keyboard())


async def notification_loop(bot):
    while True:
        try:
            rows = await asyncio.to_thread(fetch_unsent_cases)
            if rows:
                await bot.send_message(
                    Config.ADMIN_ID,
                    render_notification(rows),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await asyncio.to_thread(mark_notified, [row["case_id"] for row in rows])
                await asyncio.sleep(1)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SPAIN_BOT] notification error: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(NOTIFICATION_POLL_SECONDS)


async def main():
    if not Config.SPAIN_BOT_TOKEN:
        raise RuntimeError("SPAIN_BOT_TOKEN is missing in .env")
    if not Config.ADMIN_ID:
        raise RuntimeError("ADMIN_ID is missing in .env")

    initialize_notification_store()
    proxy = telegram_proxy_url()
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=Config.SPAIN_BOT_TOKEN, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть меню"),
            BotCommand(command="status", description="Статус радара"),
            BotCommand(command="chats", description="Все найденные чаты"),
            BotCommand(command="confirmed", description="Подтверждённые случаи"),
            BotCommand(command="candidates", description="Кандидаты на проверку"),
            BotCommand(command="latest", description="Последние находки"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=True)
    notifier = asyncio.create_task(notification_loop(bot))
    try:
        print("[SPAIN_BOT] Telegram bot started", flush=True)
        await dispatcher.start_polling(bot)
    finally:
        notifier.cancel()
        with suppress(asyncio.CancelledError):
            await notifier
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
