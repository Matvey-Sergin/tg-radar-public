import sqlite3
import tempfile
import unittest
from pathlib import Path

from margin_leak_radar import ProtectionStore


class MarginStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "radar.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE accounts (
                session_name TEXT PRIMARY KEY,
                session_string TEXT,
                api_id INTEGER,
                api_hash TEXT,
                device_model TEXT,
                system_version TEXT,
                app_version TEXT,
                status TEXT DEFAULT 'Active',
                cooldown_until INTEGER DEFAULT 0
            );
            CREATE TABLE target_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                title TEXT,
                username TEXT,
                status TEXT DEFAULT 'Processed',
                last_read_msg_id INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0,
                rejection_reason TEXT
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE margin_radar_state (
                chat_id INTEGER PRIMARY KEY,
                last_seen_msg_id INTEGER DEFAULT 0,
                last_checked_at TIMESTAMP,
                next_check_at INTEGER DEFAULT 0,
                last_new_messages INTEGER DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE margin_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                chat_title TEXT,
                chat_username TEXT,
                message_link TEXT,
                author_id INTEGER,
                author_username TEXT,
                author_name TEXT,
                msg_at TIMESTAMP,
                text TEXT,
                pain_type TEXT,
                signal_strength INTEGER DEFAULT 0,
                score INTEGER DEFAULT 0,
                why_client TEXT,
                risks TEXT,
                questions TEXT,
                soft_reply TEXT,
                mini_audit TEXT,
                raw_llm_json TEXT,
                notified INTEGER DEFAULT 0,
                notified_at TIMESTAMP,
                text_hash TEXT,
                verifier_json TEXT,
                verifier_passed INTEGER DEFAULT 0,
                feedback TEXT,
                feedback_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, message_id)
            );
            INSERT INTO accounts
                (session_name, session_string, api_id, api_hash)
            VALUES
                ('account_1', 'one', 1, 'hash'),
                ('account_2', 'two', 1, 'hash');
            INSERT INTO target_chats
                (chat_id, title, username, status, quality_score)
            VALUES
                (101, 'Seller chat', 'seller_chat', 'Processed', 10);
            """
        )
        conn.commit()
        conn.close()
        self.store = ProtectionStore(self.db_path)
        await self.store.init_schema()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_live_queue_and_cursor_are_persisted_together(self):
        candidate = {
            "chat_id": 101,
            "message_id": 501,
            "chat_title": "Seller chat",
            "chat_username": "seller_chat",
            "message_link": "https://t.me/seller_chat/501",
            "author_id": 7,
            "author_username": "seller",
            "author_name": "Seller",
            "msg_at": "2026-06-18T10:00:00+00:00",
            "text": "У меня списали штраф, как оспорить?",
            "pain_types": ["штраф"],
            "text_hash": "hash",
        }
        await self.store.assign_chat_tiers(1_000_000)
        await self.store.enqueue_live_candidate(candidate)

        queued = await self.store.next_live_candidate()
        self.assertEqual(queued["candidate"], candidate)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT last_seen_msg_id FROM margin_radar_state WHERE chat_id = 101"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(cursor, 501)

        await self.store.complete_live_candidate(queued["queue_id"])
        self.assertIsNone(await self.store.next_live_candidate())

    async def test_backfill_checkpoint_survives_store_reopen(self):
        await self.store.assign_chat_tiers(1_000_000)
        await self.store.schedule_chat(
            101,
            "Warm",
            None,
            20,
            history_offset_id=481,
            history_high_watermark=500,
            continue_backfill=True,
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT history_offset_id, history_high_watermark, last_seen_msg_id
            FROM margin_radar_state
            WHERE chat_id = 101
            """
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 481)
        self.assertEqual(row[1], 500)
        self.assertEqual(row[2], 0)

    async def test_second_layer_labels_and_score_are_persisted(self):
        candidate = {
            "chat_id": 101,
            "message_id": 777,
            "chat_title": "Seller chat",
            "chat_username": "seller_chat",
            "message_link": "https://t.me/seller_chat/777",
            "author_id": 9,
            "author_username": "seller",
            "author_name": "Seller",
            "msg_at": "2026-06-19T10:00:00+00:00",
            "text": (
                "У меня товар приняли на складе, но он не в продаже. "
                "Хранение списывают, поддержка молчит. Что делать?"
            ),
            "pain_types": ["FBS", "логистика"],
            "text_hash": "frozen-hash",
        }
        lead = await self.store.save_lead(
            candidate,
            {
                "score": 7,
                "signal_strength": 5,
                "actor_type": "seller",
                "radar_type": "frozen_stock",
                "temperature": "red",
                "pain_type": "логистика",
                "why_client": ["товар не продаётся"],
                "risks": [],
                "questions": ["Когда приняли товар?"],
            },
            "{}",
        )
        verifier = {
            "actor_type": "seller",
            "radar_type": "frozen_stock",
            "temperature": "red",
            "is_seller": True,
            "personal_case": True,
            "current_problem": True,
            "money_or_ops_loss": True,
            "seeks_solution": True,
            "is_ad": False,
            "is_review": False,
            "is_news": False,
            "is_expert_reply": False,
            "has_specificity": True,
            "helplessness": True,
            "urgent": True,
            "confidence": 0.95,
        }
        labels = await self.store.save_verifier(lead["id"], verifier, True)

        self.assertEqual(labels["radar_type"], "frozen_stock")
        self.assertEqual(labels["temperature"], "red")
        self.assertEqual(labels["filter_score"], 10)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT actor_type, radar_type, temperature, filter_score, verifier_passed
            FROM margin_leads
            WHERE id = ?
            """,
            (lead["id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("seller", "frozen_stock", "red", 10, 1))

    async def test_chat_lease_is_atomic_between_accounts(self):
        await self.store.assign_chat_tiers(1_000_000)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE margin_radar_state SET next_check_at = 0, lease_until = 0 WHERE chat_id = 101"
        )
        conn.commit()
        conn.close()

        first = await self.store.lease_due_chat("account_1", 1_000_000)
        second = await self.store.lease_due_chat("account_2", 1_000_000)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_candidate_funnel_reason_is_persisted(self):
        candidate = {
            "chat_id": 101,
            "message_id": 888,
            "chat_title": "Seller chat",
            "chat_username": "seller_chat",
            "message_link": "https://t.me/seller_chat/888",
            "author_id": 10,
            "author_username": "agency",
            "author_name": "Agency",
            "msg_at": "2026-06-19T10:00:00+00:00",
            "text": "Сделаем инфографику, пишите в ЛС",
            "text_hash": "agency-hash",
        }
        await self.store.save_screening_event(
            candidate,
            source="poll",
            outcome="screened",
            rejection_reason="ad_detected",
            rejection_detail="service provider offer",
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT outcome, rejection_reason, rejection_detail
            FROM margin_screening_events
            WHERE chat_id = 101 AND message_id = 888
            """
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("screened", "ad_detected", "service provider offer"))

    async def test_first_score_three_can_be_pending_strict_lead(self):
        candidate = {
            "chat_id": 101,
            "message_id": 999,
            "chat_title": "Seller chat",
            "chat_username": "seller_chat",
            "message_link": "https://t.me/seller_chat/999",
            "author_id": 11,
            "author_username": "seller",
            "author_name": "Seller",
            "msg_at": "2026-06-19T11:00:00+00:00",
            "text": "У меня товар принят, но не продаётся, хранение списывают. Что делать?",
            "pain_types": ["FBS"],
            "text_hash": "score-three-hash",
        }
        await self.store.save_screening_event(candidate, source="poll", outcome="candidate")
        lead = await self.store.save_lead(
            candidate,
            {"score": 3, "signal_strength": 3, "pain_type": "FBS"},
            "{}",
        )
        verifier = {
            "actor_type": "seller",
            "radar_type": "frozen_stock",
            "temperature": "red",
            "is_seller": True,
            "personal_case": True,
            "current_problem": True,
            "money_or_ops_loss": True,
            "seeks_solution": True,
            "is_ad": False,
            "is_review": False,
            "is_news": False,
            "is_expert_reply": False,
            "has_specificity": True,
            "helplessness": True,
            "urgent": True,
            "confidence": 0.95,
        }
        await self.store.save_verifier(lead["id"], verifier, True)

        pending = await self.store.pending_verified_leads()
        self.assertEqual([row["id"] for row in pending], [lead["id"]])

    async def test_hot_tier_decays_without_recent_strict_lead(self):
        now = 2_000_000_000
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO margin_leads
                (chat_id, message_id, score, verifier_passed, filter_score, created_at)
            VALUES (101, 1234, 7, 1, 10, datetime(?, 'unixepoch'))
            """,
            (now - 73 * 3600,),
        )
        conn.commit()
        conn.close()

        await self.store.assign_chat_tiers(now)
        conn = sqlite3.connect(self.db_path)
        tier = conn.execute(
            "SELECT tier FROM margin_radar_state WHERE chat_id = 101"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(tier, "Warm")

    async def test_notification_claim_prevents_parallel_duplicate(self):
        self.assertTrue(await self.store.claim_notification(1, "same-hash", now=1_000_000))
        self.assertFalse(await self.store.claim_notification(2, "same-hash", now=1_000_001))
        await self.store.release_notification_claim(1, "same-hash")
        self.assertTrue(await self.store.claim_notification(2, "same-hash", now=1_000_002))


if __name__ == "__main__":
    unittest.main()
