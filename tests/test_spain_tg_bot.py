import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from spain_tg_bot import (
    authorized,
    fetch_unsent_cases,
    initialize_notification_store,
    mark_notified,
    render_notification,
)


class SpainTelegramBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "spain.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """
                CREATE TABLE cases (
                    case_id TEXT PRIMARY KEY, confirmed INTEGER, evidence_kind TEXT,
                    visa_type TEXT, msg_at TEXT, chat_title TEXT, message_link TEXT,
                    evidence_excerpt TEXT, first_seen_at TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO cases VALUES
                ('case_1',1,'direct_applicant','Digital Nomad','2026-08-27',
                 'Испания чат','https://t.me/spain/1','Подалась в СПб','2026-08-27')
                """
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_notification_queue_is_deduplicated(self):
        initialize_notification_store(self.db)
        rows = fetch_unsent_cases(path=self.db)
        self.assertEqual([row["case_id"] for row in rows], ["case_1"])
        mark_notified(["case_1"], path=self.db)
        self.assertEqual(fetch_unsent_cases(path=self.db), [])

    def test_notification_contains_clickable_source(self):
        text = render_notification(fetch_unsent_cases(path=self.db))
        self.assertIn('href="https://t.me/spain/1"', text)
        self.assertIn("Подалась в СПб", text)

    def test_every_telegram_user_is_authorized(self):
        self.assertTrue(authorized(123456789))


if __name__ == "__main__":
    unittest.main()
