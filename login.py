import os
import sys
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client
from networking import telegram_proxy

logging.basicConfig(level=logging.WARNING)
PROJECT_ROOT = Path(__file__).resolve().parent


def log(msg):
    print(msg, flush=True)


log("⏳ [1/4] Инициализация скрипта и загрузка .env...")
load_dotenv(PROJECT_ROOT / ".env")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not API_ID or not API_HASH:
    log("❌ ОШИБКА: API_ID или API_HASH пусты! Проверь свой .env")
    sys.exit(1)

log("✅ [2/4] Данные получены. Используем активные системные сетевые настройки...")


async def main():
    log("⏳ [3/4] Запуск in-memory клиента Pyrogram...")

    app = Client(
        name="my_memory_account",
        api_id=int(API_ID),
        api_hash=API_HASH,
        proxy=telegram_proxy(),
        in_memory=True
    )

    log("☎️ СЕЙЧАС ПОЯВИТСЯ ЗАПРОС НОМЕРА ТЕЛЕФОНА И КОДА...")
    await app.start()

    log("\n" + "🔥" * 30)
    log("ГЕНЕРАЦИЯ STRING SESSION УСПЕШНА!")
    session_string = await app.export_session_string()

    log("\n" + "=" * 70)
    log(f"{session_string}")
    log("=" * 70)
    log("\n👉 СКОПИРУЙ строку выше и добавь в .env: SESSION_STRING=твоя_строка")

    await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("\n🛑 Остановлено вручную.")
    except Exception as e:
        log(f"\n❌ ОШИБКА: {e}")
