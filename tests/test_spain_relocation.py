import unittest
import tempfile
from pathlib import Path

from spain_relocation_radar import SpainStore
from spain_relocation_rules import (
    classify_spb_spain_case,
    looks_like_spain_relocation_source,
)


class SpainRelocationRulesTests(unittest.TestCase):
    def test_first_person_spb_application_is_confirmed(self):
        result = classify_spb_spain_case(
            "Я подавалась на визу Испании через BLS в Санкт-Петербурге, документы приняли.",
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["evidence_kind"], "direct_applicant")

    def test_question_about_other_people_is_not_confirmed(self):
        result = classify_spb_spain_case(
            "Кто-нибудь подавался на визу Испании в СПб? Подскажите сроки.",
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["candidate"])

    def test_spain_chat_can_supply_destination_context(self):
        result = classify_spb_spain_case(
            "Я уже подала документы в Питере, приняли без вопросов.",
            source_is_spain_specific=True,
        )
        self.assertTrue(result["confirmed"])

    def test_spb_mentioned_only_for_pension_document_is_not_confirmed(self):
        result = classify_spb_spain_case(
            "Я подалась на DN внутри Испании. Справку СФР брала в Петербурге по доверенности.",
            source_is_spain_specific=True,
        )
        self.assertFalse(result["confirmed"])

    def test_direct_statistics_without_pronoun_are_confirmed(self):
        result = classify_spb_spain_case(
            "#статистика Подача 21.07 СПб. Получила паспорт, визу дали на 30 дней.",
            source_is_spain_specific=True,
        )
        self.assertTrue(result["confirmed"])

    def test_professional_concrete_case_is_separate_kind(self):
        result = classify_spb_spain_case(
            "Получили одобрение визы D для клиента в консульстве Испании в Петербурге.",
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["evidence_kind"], "professional_case_report")

    def test_unrelated_spb_message_is_ignored(self):
        result = classify_spb_spain_case(
            "Живу в Санкт-Петербурге и думаю о поездке следующим летом.",
            source_is_spain_specific=True,
        )
        self.assertFalse(result["candidate"])

    def test_source_needs_spain_and_relocation_context(self):
        self.assertTrue(looks_like_spain_relocation_source("Digital Nomad Испания", "ВНЖ и переезд"))
        self.assertFalse(looks_like_spain_relocation_source("Испания: футбол", "Новости Ла Лиги"))


class SpainRelocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SpainStore(Path(self.tmp.name) / "spain.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_cursor_is_persistent_and_incremental(self):
        self.store.upsert_sources([{
            "username": "spain_test", "chat_id": -1001, "title": "Испания ВНЖ чат",
            "description": "Релокация", "type": "supergroup", "members": 100,
            "link": "https://t.me/spain_test",
        }])
        self.store.mark_checked("spain_test", 500, 10, 0)
        source = self.store.due_sources()[0]
        self.assertEqual(source["last_read_msg_id"], 500)
        self.store.upsert_sources([{
            "username": "spain_test", "chat_id": -1001, "title": "Испания ВНЖ чат 2",
            "description": "Релокация", "type": "supergroup", "members": 120,
            "link": "https://t.me/spain_test",
        }])
        self.assertEqual(self.store.due_sources()[0]["last_read_msg_id"], 500)

    def test_cases_are_deduplicated(self):
        row = {
            "case_id": "case_1", "chat_username": "spain_test", "message_id": 10,
            "confirmed": True, "evidence_kind": "direct_applicant", "visa_type": "Schengen",
            "date": "2026-08-27T00:00:00+00:00", "chat": "Испания чат",
            "message_link": "https://t.me/spain_test/10", "evidence_excerpt": "Подала в СПб",
            "origin": "source_history", "checks": {"has_spb": True},
        }
        self.store.upsert_cases([row, row])
        self.assertEqual(self.store.counts()["confirmed_cases"], 1)


if __name__ == "__main__":
    unittest.main()
