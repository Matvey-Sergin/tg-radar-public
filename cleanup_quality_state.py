import argparse
import asyncio
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from config import Config
from database import Database
from quality_rules import title_rejection_reason


def backup_database(db_path):
    if not os.path.exists(db_path):
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = PROJECT_ROOT / "backups" / "db"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{Path(db_path).name}.quality_backup_{stamp}"
    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def clean_database(db_path):
    now = int(time.time())
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")

        conn.execute("DELETE FROM pains")
        conn.execute("DELETE FROM verified_insights")
        conn.execute("DELETE FROM trash_bin")
        conn.execute("DELETE FROM system_state")
        conn.execute("UPDATE keywords SET used = 0")

        chat_rows = conn.execute("SELECT id, title FROM target_chats ORDER BY id").fetchall()
        kept = 0
        rejected = 0
        for chat_id, title in chat_rows:
            reason = title_rejection_reason(title or "")
            if reason:
                conn.execute(
                    """
                    UPDATE target_chats
                    SET status = 'Rejected',
                        rejection_reason = ?,
                        last_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason, chat_id),
                )
                rejected += 1
            else:
                conn.execute(
                    """
                    UPDATE target_chats
                    SET status = 'New',
                        last_read_msg_id = 0,
                        rejection_reason = NULL,
                        last_checked_at = NULL
                    WHERE id = ?
                    """,
                    (chat_id,),
                )
                kept += 1

        conn.execute(
            "UPDATE accounts SET status = 'Active', cooldown_until = 0 WHERE status = 'In_Use' AND cooldown_until <= ?",
            (now,),
        )
        conn.commit()
        return kept, rejected


def main():
    parser = argparse.ArgumentParser(description="Clean generated results and requeue only likely professional IT chats.")
    parser.add_argument("--yes", action="store_true", help="Actually run cleanup.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a DB backup first.")
    args = parser.parse_args()

    db_path = Config.DB_PATH
    print(f"DB: {os.path.abspath(db_path)}")
    if not args.yes:
        print("Dry run only. Run: python cleanup_quality_state.py --yes")
        return

    asyncio.run(Database.init_schema())
    backup_path = None if args.no_backup else backup_database(db_path)
    if backup_path:
        print(f"Backup created: {os.path.abspath(backup_path)}")

    kept, rejected = clean_database(db_path)
    print("Done.")
    print(f"Chats queued for recheck: {kept}")
    print(f"Chats rejected by title: {rejected}")


if __name__ == "__main__":
    main()
