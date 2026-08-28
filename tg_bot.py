import asyncio
from html import escape

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import Config
from database import Database
from networking import telegram_proxy_url


proxy = telegram_proxy_url()
bot = Bot(token=Config.BOT_TOKEN, session=AiohttpSession(proxy=proxy) if proxy else None,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="💰 First Money"), KeyboardButton(text="📊 Топ IT-болей")],
    [KeyboardButton(text="🆕 Свежие сигналы")],
], resize_keyboard=True)

INSIGHT_COLUMNS = """id,core_problem,description,icp,pain_score,wtp_score,reachability_score,
unique_authors,unique_chats,total_messages,recent_signals,trend,current_workaround,manual_mvp,
offer,price_hypothesis,acquisition_source,next_50_source,main_risk,next_experiment,
first_money_score,confidence"""


def insight_kb(insight_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Первичные свидетельства", callback_data=f"sources_{insight_id}_0")],
        [InlineKeyboardButton(text="⏭ Следующий кластер", callback_data="next_insight")],
    ])


def sources_kb(insight_id, page, total, link=None):
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Открыть исходное сообщение", url=link)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sources_{insight_id}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total}", callback_data="noop"))
    if page + 1 < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sources_{insight_id}_{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 К кластеру", callback_data=f"back_{insight_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def fetch_insight(insight_id=None):
    async with aiosqlite.connect(Config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if insight_id:
            query, params = f"SELECT {INSIGHT_COLUMNS} FROM verified_insights WHERE id=?", (insight_id,)
        else:
            query, params = (f"SELECT {INSIGHT_COLUMNS} FROM verified_insights WHERE total_messages>=2 "
                             "ORDER BY first_money_score DESC, RANDOM() LIMIT 1"), ()
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()


def fmt(row):
    if not row:
        return "Кластеры ещё не собраны. Запустите pipeline и дождитесь независимых свидетельств."
    e = lambda value: escape(str(value or "неизвестно"))
    return (
        f"💰 <b>FIRST MONEY #{row['id']}</b> · {int(row['first_money_score'] or 0)}/100\n"
        f"🛑 <b>{e(row['core_problem'])}</b>\n\n"
        f"Pain: <b>{int(row['pain_score'] or 0)}/100</b> · WTP: <b>{e(row['wtp_score'])}</b> · "
        f"Reachability: <b>{int(row['reachability_score'] or 0)}/100</b>\n"
        f"Независимые люди: <b>{int(row['unique_authors'] or 0)}</b> · Чаты: <b>{int(row['unique_chats'] or 0)}</b> · "
        f"Сообщения: <b>{int(row['total_messages'] or 0)}</b> · 30 дней: <b>{int(row['recent_signals'] or 0)}</b> · {e(row['trend'])}\n\n"
        f"<b>Боль:</b> {e(row['description'])}\n\n"
        f"<b>ICP:</b> {e(row['icp'])}\n"
        f"<b>Что делает сейчас:</b> {e(row['current_workaround'])}\n\n"
        f"<b>Manual MVP:</b> {e(row['manual_mvp'])}\n"
        f"<b>Оффер на 48 часов:</b> {e(row['offer'])}\n"
        f"<b>Цена-гипотеза:</b> {e(row['price_hypothesis'])}\n\n"
        f"<b>Первые 10:</b> {e(row['acquisition_source'])}\n"
        f"<b>Следующие 50:</b> {e(row['next_50_source'])}\n"
        f"<b>Риск:</b> {e(row['main_risk'])}\n"
        f"<b>Следующий тест:</b> {e(row['next_experiment'])}"
    )


async def source_count(insight_id):
    row = await Database.fetchone("SELECT COUNT(*) FROM pains WHERE insight_id=?", (insight_id,))
    return int(row[0] or 0) if row else 0


async def source_at(insight_id, offset):
    async with aiosqlite.connect(Config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.evidence_quote,p.evidence_context,p.msg_at,p.message_link,p.source_title,
                      p.profession_segment,p.pain_summary,p.current_workaround,p.potential_wtp
               FROM pains p WHERE p.insight_id=? ORDER BY p.msg_at DESC LIMIT 1 OFFSET ?""",
            (insight_id, offset),
        ) as cursor:
            return await cursor.fetchone()


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "<b>IT Pain Radar</b> ищет первичные свидетельства повторяющихся проблем в русскоязычных "
        "профессиональных Telegram-чатах и ранжирует их по боли, WTP, доступности лидов и скорости первой оплаты.",
        reply_markup=main_kb,
    )


@dp.message(F.text == "💰 First Money")
async def first_money(message: types.Message):
    row = await fetch_insight()
    await message.answer(fmt(row), reply_markup=insight_kb(row["id"]) if row else None)


@dp.callback_query(F.data == "next_insight")
async def next_insight(callback: types.CallbackQuery):
    row = await fetch_insight()
    await callback.message.edit_text(fmt(row), reply_markup=insight_kb(row["id"]) if row else None)


@dp.callback_query(F.data.startswith("sources_"))
async def sources(callback: types.CallbackQuery):
    _, raw_id, raw_page = callback.data.split("_")
    insight_id, page = int(raw_id), int(raw_page)
    total = await source_count(insight_id)
    row = await source_at(insight_id, page)
    if not row:
        await callback.answer("Свидетельство не найдено", show_alert=True)
        return
    text = (
        f"🔎 <b>ПЕРВИЧНОЕ СВИДЕТЕЛЬСТВО</b>\n"
        f"Чат: {escape(str(row['source_title'] or 'неизвестно'))}\n"
        f"Сегмент: {escape(str(row['profession_segment'] or 'не определён'))}\n"
        f"Дата: {escape(str(row['msg_at'] or 'неизвестно'))}\n"
        f"WTP: {escape(str(row['potential_wtp'] or 'LOW'))}\n\n"
        f"<b>Сообщение:</b> <i>«{escape(str(row['evidence_quote'] or ''))}»</i>\n\n"
        f"<b>Контекст:</b> {escape(str(row['evidence_context'] or 'не сохранён'))}\n\n"
        f"<b>Извлечённая боль:</b> {escape(str(row['pain_summary'] or 'не определена'))}\n"
        f"<b>Текущий обход:</b> {escape(str(row['current_workaround'] or 'неизвестно'))}"
    )
    await callback.message.edit_text(text, reply_markup=sources_kb(insight_id, page, total, row["message_link"]))


@dp.callback_query(F.data.startswith("back_"))
async def back(callback: types.CallbackQuery):
    row = await fetch_insight(int(callback.data.split("_")[1]))
    await callback.message.edit_text(fmt(row), reply_markup=insight_kb(row["id"]) if row else None)


@dp.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.message(F.text == "📊 Топ IT-болей")
async def top(message: types.Message):
    async with aiosqlite.connect(Config.DB_PATH) as db:
        async with db.execute(
            """SELECT core_problem,first_money_score,pain_score,unique_authors,unique_chats
               FROM verified_insights ORDER BY first_money_score DESC LIMIT 5"""
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await message.answer("Рейтинг пока пуст.")
        return
    text = "🏆 <b>ТОП IT-БОЛЕЙ</b>\n\n" + "\n\n".join(
        f"{i}. <b>{escape(str(row[0]))}</b>\nFirst Money {row[1]}/100 · Pain {row[2]}/100 · {row[3]} людей · {row[4]} чатов"
        for i, row in enumerate(rows, 1)
    )
    await message.answer(text)


@dp.message(F.text == "🆕 Свежие сигналы")
async def fresh(message: types.Message):
    rows = await Database.fetchall(
        """SELECT category,COUNT(*),COUNT(DISTINCT author_anonymized_id)
           FROM pains WHERE msg_at>=datetime('now','-7 days') GROUP BY category ORDER BY COUNT(*) DESC LIMIT 5"""
    )
    text = "🆕 <b>СИГНАЛЫ ЗА 7 ДНЕЙ</b>\n\n" + ("\n".join(
        f"• {escape(str(category))}: {count} сообщений, {authors} авторов" for category, count, authors in rows
    ) if rows else "Свежих сигналов пока нет.")
    await message.answer(text)


async def main():
    await Database.init_schema()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
