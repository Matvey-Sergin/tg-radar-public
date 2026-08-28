import time

from pyrogram import Client

from database import Database
from logging_utils import log_event
from networking import telegram_proxy


class AccountManager:
    @classmethod
    async def get_available_client(cls) -> tuple[Client, str]:
        account_data = await Database.get_available_account()
        if not account_data:
            log_event("ACCOUNT", "LEASE", "No available account")
            return None, None

        (
            session_name,
            session_string,
            api_id,
            api_hash,
            device_model,
            system_version,
            app_version,
        ) = account_data

        lease_lock_time = int(time.time()) + 300
        log_event(
            "ACCOUNT",
            "LEASE",
            "Locking account for worker",
            session_name=session_name,
            lease_until=lease_lock_time,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
        )
        await Database.execute(
            "UPDATE accounts SET cooldown_until = ?, status = 'In_Use' WHERE session_name = ?",
            (lease_lock_time, session_name),
        )

        app = Client(
            name=session_name,
            session_string=session_string,
            api_id=api_id,
            api_hash=api_hash,
            proxy=telegram_proxy(),
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            in_memory=True,
        )

        log_event("ACCOUNT", "CLIENT_READY", "Pyrogram client prepared", session_name=session_name)
        return app, session_name

    @classmethod
    async def release_account(cls, session_name: str):
        if not session_name:
            return
        await Database.execute(
            "UPDATE accounts SET cooldown_until = 0, status = 'Active' WHERE session_name = ? AND status = 'In_Use'",
            (session_name,),
        )
        log_event("ACCOUNT", "RELEASE", "Account released if it was still in use", session_name=session_name)

    @classmethod
    async def report_flood_wait(cls, session_name: str, wait_seconds: int):
        safe_wait = int(wait_seconds * 1.1)
        cooldown_until = int(time.time()) + safe_wait
        log_event(
            "ACCOUNT",
            "FLOOD_WAIT",
            "Telegram flood wait reported",
            session_name=session_name,
            wait_seconds=wait_seconds,
            safe_wait=safe_wait,
            cooldown_until=cooldown_until,
        )
        await Database.update_account_cooldown(session_name, cooldown_until, status="Cooldown")

    @classmethod
    async def apply_soft_cooldown(cls, session_name: str, wait_seconds: int):
        cooldown_until = int(time.time()) + wait_seconds
        log_event(
            "ACCOUNT",
            "SOFT_COOLDOWN",
            "Account sent to cooldown",
            session_name=session_name,
            wait_seconds=wait_seconds,
            cooldown_until=cooldown_until,
        )
        await Database.update_account_cooldown(session_name, cooldown_until, status="Cooldown")
