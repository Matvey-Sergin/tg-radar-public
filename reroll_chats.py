import argparse
import sqlite3
from pathlib import Path

from config import Config


def main():
    parser = argparse.ArgumentParser(
        description="Requeue known chats so miner checks them again."
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also reset last_read_msg_id to 0, so miner rereads old history.",
    )
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Also requeue chats rejected as non-seller.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply changes. Without this flag the script only prints a preview.",
    )
    args = parser.parse_args()

    db_path = Path(Config.DB_PATH)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    statuses = ["Processed", "Error"]
    if args.include_rejected:
        statuses.append("Rejected_NonSeller")

    placeholders = ",".join("?" for _ in statuses)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")

        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS total
            FROM target_chats
            WHERE status IN ({placeholders})
            GROUP BY status
            ORDER BY status
            """,
            statuses,
        ).fetchall()

        total = sum(int(row["total"]) for row in rows)
        print(f"DB: {db_path.resolve()}")
        print(f"Chats to reroll: {total}")
        for row in rows:
            print(f"- {row['status']}: {row['total']}")

        if args.deep:
            print("Mode: deep reread, last_read_msg_id will be reset to 0")
        else:
            print("Mode: soft recheck, only next_check_at will be reset")

        if not args.yes:
            print("\nDry run only. Add --yes to apply.")
            return

        if args.deep:
            conn.execute(
                f"""
                UPDATE target_chats
                SET status = 'New',
                    last_read_msg_id = 0,
                    next_check_at = 0,
                    empty_reads = 0,
                    last_new_messages = 0,
                    rejection_reason = NULL
                WHERE status IN ({placeholders})
                """,
                statuses,
            )
        else:
            conn.execute(
                f"""
                UPDATE target_chats
                SET next_check_at = 0,
                    empty_reads = 0,
                    last_new_messages = 0
                WHERE status IN ({placeholders})
                """,
                statuses,
            )

        conn.commit()
        print("Done.")


if __name__ == "__main__":
    main()
