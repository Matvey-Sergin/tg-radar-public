import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from config import Config
from database import Database
from quality_rules import distinct_source_count, insight_quality_grade, noise_rejection_reason, source_key


def row_to_dict(row):
    return {
        "id": row[0],
        "problem_title": row[1],
        "evidence_quote": row[2],
        "source_title": row[3],
        "source_username": row[4],
        "chat_id": row[5],
        "author_anonymized_id": row[6],
        "author_id": row[7],
    }


def regrade(db_path):
    summary = {"Gold": 0, "Silver": 0, "Trash": 0}
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        insights = conn.execute(
            """
            SELECT id, core_problem, COALESCE(business_impact, '')
            FROM verified_insights
            ORDER BY id
            """
        ).fetchall()

        for insight_id, core_problem, business_impact in insights:
            source_rows = [
                row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT id, problem_title, evidence_quote, source_title, source_username, chat_id,
                           author_anonymized_id, author_id
                    FROM pains
                    WHERE insight_id = ?
                    ORDER BY id
                    """,
                    (insight_id,),
                ).fetchall()
            ]
            grade, reason = insight_quality_grade(core_problem, business_impact, source_rows)
            source_count = distinct_source_count(source_rows)
            independent_authors = {
                row.get("author_anonymized_id") or
                (f"{row.get('chat_id')}:{row.get('author_id')}" if row.get("author_id") else None)
                for row in source_rows
            }
            independent_authors.discard(None)
            if grade != "Trash" and source_count >= 2 and len(independent_authors) >= 2:
                grade = "Gold"

            noisy_rows = 0
            for row in source_rows:
                if noise_rejection_reason(
                    f"{row.get('problem_title', '')} {row.get('evidence_quote', '')}",
                    row.get("source_title", ""),
                ):
                    noisy_rows += 1
            noise_score = noisy_rows / max(len(source_rows), 1)
            source_chats = sorted({source_key(row) for row in source_rows if source_key(row)})

            conn.execute(
                """
                UPDATE verified_insights
                SET quality_grade = ?, source_count = ?, noise_score = ?, source_chats = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    grade,
                    source_count,
                    noise_score,
                    json.dumps(source_chats, ensure_ascii=False),
                    insight_id,
                ),
            )
            summary[grade] = summary.get(grade, 0) + 1
            if grade == "Trash":
                print(f"Trash #{insight_id}: {core_problem} ({reason})")

        conn.commit()
    return summary


def main():
    parser = argparse.ArgumentParser(description="Regrade existing insights into Gold/Silver/Trash without deleting data.")
    parser.add_argument("--yes", action="store_true", help="Actually update the database.")
    args = parser.parse_args()

    print(f"DB: {Config.DB_PATH}")
    if not args.yes:
        print("Dry run only. Run: python regrade_insights.py --yes")
        return

    asyncio.run(Database.init_schema())
    summary = regrade(Config.DB_PATH)
    print("Done.")
    for grade in ["Gold", "Silver", "Trash"]:
        print(f"{grade}: {summary.get(grade, 0)}")


if __name__ == "__main__":
    main()
