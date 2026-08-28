import argparse
import asyncio
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from config import Config
from database import Database


def db_sidecar_paths(db_path):
    return [db_path, f"{db_path}-wal", f"{db_path}-shm"]


def backup_database(db_path):
    if not os.path.exists(db_path):
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = PROJECT_ROOT / "backups" / "db"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{Path(db_path).name}.backup_{stamp}"
    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def load_existing_chats(db_path, include_invalid):
    if not os.path.exists(db_path):
        return []

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            SELECT DISTINCT chat_id, title, username
            FROM target_chats
            WHERE chat_id IS NOT NULL
              AND username IS NOT NULL
              AND TRIM(username) != ''
              AND (? = 1 OR COALESCE(status, '') NOT IN ('Invalid', 'Skipped_Private'))
            ORDER BY title COLLATE NOCASE
            """,
            (1 if include_invalid else 0,),
        )
        return cursor.fetchall()


def remove_database_files(db_path):
    for path in db_sidecar_paths(db_path):
        if os.path.exists(path):
            os.remove(path)


def reset_database_in_place(db_path, chats):
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("DELETE FROM target_chats")
        conn.execute("DELETE FROM keywords")
        conn.execute("DELETE FROM system_state")
        conn.execute("DELETE FROM pains")
        conn.execute("DELETE FROM verified_insights")
        conn.execute("DELETE FROM trash_bin")
        conn.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name IN (
                'target_chats',
                'keywords',
                'pains',
                'verified_insights',
                'trash_bin'
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO target_chats
            (chat_id, title, username, status, last_read_msg_id)
            VALUES (?, ?, ?, 'New', 0)
            """,
            chats,
        )
        conn.commit()


async def recreate_database_with_chats(chats):
    await Database.init_schema()
    for chat_id, title, username in chats:
        await Database.execute(
            """
            INSERT OR IGNORE INTO target_chats
            (chat_id, title, username, status, last_read_msg_id)
            VALUES (?, ?, ?, 'New', 0)
            """,
            (chat_id, title, username),
        )


def main():
    parser = argparse.ArgumentParser(
        description="Reset scout.db, keep discovered chats, and force miner to reread them from scratch."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually reset the database. Without this flag the script only prints a preview.",
    )
    parser.add_argument(
        "--include-invalid",
        action="store_true",
        help="Also keep chats that were marked Invalid or Skipped_Private.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a .backup copy before deleting the database.",
    )
    args = parser.parse_args()

    db_path = Config.DB_PATH
    chats = load_existing_chats(db_path, include_invalid=args.include_invalid)

    print(f"DB: {os.path.abspath(db_path)}")
    print(f"Chats to keep and recheck: {len(chats)}")

    if not args.yes:
        print("\nDry run only. Nothing was deleted.")
        print("Run this to reset for real:")
        print("python reset_db_keep_chats.py --yes")
        return

    backup_path = None if args.no_backup else backup_database(db_path)
    if backup_path:
        print(f"Backup created: {os.path.abspath(backup_path)}")

    try:
        remove_database_files(db_path)
        asyncio.run(recreate_database_with_chats(chats))
        reset_mode = "file recreate"
    except PermissionError as exc:
        print(f"\nDatabase file is locked: {exc}")
        print("Falling back to in-place reset without deleting the file.")
        reset_database_in_place(db_path, chats)
        reset_mode = "in-place SQL reset"

    print("\nDone.")
    print(f"Reset mode: {reset_mode}")
    print("Saved chats were restored with status='New' and last_read_msg_id=0.")


if __name__ == "__main__":
    main()
