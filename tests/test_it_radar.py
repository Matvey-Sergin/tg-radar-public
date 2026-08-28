import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from butcher import cluster_is_coherent, upsert_insight
from config import Config
from database import Database
from it_radar_rules import (
    anonymize_author,
    first_money_score,
    pain_score,
    reachability_score,
    trend_from_dates,
)
from quality_rules import pain_rejection_reason
from text_encoding import repair_text


class ITRadarRuleTests(unittest.TestCase):
    def test_semantic_manual_work_passes_without_emotional_marker(self):
        evidence = "Я каждое утро выгружаю логи из трех систем, потом объединяю их в Excel для команды DevOps."
        reason = pain_rejection_reason(
            "DevOps вручную объединяют логи из нескольких систем",
            evidence,
            chat_title="DevOps Engineers RU",
        )
        self.assertIsNone(reason)

    def test_one_off_syntax_question_is_rejected(self):
        reason = pain_rejection_reason(
            "Python разработчик не понимает ошибку синтаксиса",
            "Почему мой код Python не компилируется, syntax error на второй строке?",
            chat_title="Python developers",
        )
        self.assertIsNotNone(reason)

    def test_explicit_payment_and_manual_work_raise_pain_score(self):
        strong = pain_score({
            "original_message": "Каждый день делаю руками, трачу три часа, готов заплатить за помощь",
            "frequency": "daily", "urgency": "high", "repeatability": "high",
            "manual_work_indicator": True, "solution_seeking_indicator": True,
            "explicit_wtp_indicator": True, "potential_wtp": "HIGH", "severity": "high",
            "solvability": "high", "reachability": "medium",
        })
        weak = pain_score({"original_message": "Иногда неудобно", "frequency": "once", "potential_wtp": "LOW"})
        self.assertGreaterEqual(strong, 75)
        self.assertGreater(strong, weak)

    def test_reachability_and_first_money_are_bounded(self):
        reach = reachability_score(6, 17, 9, 80, .4)
        score = first_money_score(88, "HIGH", reach, True, 9, 17)
        self.assertGreaterEqual(reach, 70)
        self.assertGreaterEqual(score, 80)
        self.assertLessEqual(score, 100)

    def test_author_ids_are_stable_but_chat_scoped(self):
        self.assertEqual(anonymize_author(1, 99), anonymize_author(1, 99))
        self.assertNotEqual(anonymize_author(1, 99), anonymize_author(2, 99))

    def test_trend_detects_growth(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
        dates = ["2026-08-07T00:00:00+00:00"] * 5 + ["2026-06-25T00:00:00+00:00"]
        self.assertEqual(trend_from_dates(dates, now), "GROWING")

    def test_provider_mojibake_is_repaired(self):
        self.assertEqual(repair_text("Íåâîçìîæíîñòü"), "Невозможность")

    def test_semantically_unrelated_cluster_is_rejected(self):
        rows = [
            {"problem_title": "Поиск партнера для стартапа", "pain_summary": "Нужен партнер", "category": "business"},
            {"problem_title": "Безопасность прослойки в production", "pain_summary": "Сервис можно взломать", "category": "security"},
        ]
        self.assertFalse(cluster_is_coherent("Невозможно найти партнера для проекта", rows))

    def test_same_infrastructure_pain_is_coherent(self):
        rows = [
            {"problem_title": "VPN не работает из-за блокировки vless", "pain_summary": "РКН блокирует vless", "category": "infrastructure"},
            {"problem_title": "Нет доступа к серверу через vless", "pain_summary": "vless не работает у пользователей", "category": "infrastructure"},
        ]
        self.assertTrue(cluster_is_coherent("Разработчики теряют доступ к VPN из-за блокировки vless", rows))


class ITClusterPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = Config.DB_PATH
        Config.DB_PATH = str(Path(self.tmp.name) / "it.db")
        await Database.init_schema()

    async def asyncTearDown(self):
        Config.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_cluster_persists_independent_evidence_and_first_money_fields(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for index, (chat_id, author, title) in enumerate([
            (101, "usr_a", "DevOps RU"), (202, "usr_b", "SRE Community")
        ], 1):
            pain_id = await Database.execute(
                """INSERT INTO pains
                (chat_id,message_id,problem_title,category,wtp_signal,evidence_quote,evidence_context,
                 source_title,source_username,confidence,author_anonymized_id,profession_segment,
                 pain_summary,current_workaround,frequency,solution_seeking_indicator,
                 explicit_wtp_indicator,potential_wtp,pain_score,msg_at,quality_score,butcher_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Pending')""",
                (chat_id, index, "Инженеры долго диагностируют падения CI/CD", "ci_cd", "HIGH",
                 "Каждый релиз часами ищем причину падения pipeline между GitHub Actions и логами.",
                 "Команда DevOps повторяет диагностику вручную.", title, title.lower().replace(" ", "_"), .9,
                 author, "DevOps/SRE", "Инженеры теряют часы на диагностику CI/CD",
                 "Сверяют логи нескольких систем вручную", "weekly", 1, 1, "HIGH", 82, now, 80),
            )
            rows.append({
                "id": pain_id, "chat_id": chat_id, "author_anonymized_id": author,
                "source_title": title, "source_username": title.lower(), "message_id": index,
                "problem_title": "Инженеры долго диагностируют падения CI/CD", "category": "ci_cd",
                "wtp_signal": "HIGH", "potential_wtp": "HIGH", "pain_score": 82,
                "evidence_quote": "Каждый релиз часами ищем причину падения pipeline между GitHub Actions и логами.",
                "evidence_context": "Команда DevOps повторяет диагностику вручную.", "confidence": .9,
                "msg_at": now, "quality_score": 80, "solution_seeking_indicator": 1,
                "explicit_wtp_indicator": 1, "current_workaround": "Сверяют логи вручную",
            })
        cluster = {
            "core_problem": "DevOps-команды теряют часы на диагностику падений CI/CD",
            "description": "Причина падения распределена между pipeline и несколькими системами логов.",
            "category": "ci_cd", "icp": "DevOps/SRE в командах с частыми релизами",
            "business_impact": "Инженеры тратят оплачиваемые часы и задерживают релиз.",
            "current_workaround": "Ручная сверка логов", "frequency": "weekly",
            "why_business_pain": "Повторяется у независимых команд.",
            "why_not_business_pain": "Может быть закрыто настройкой существующих инструментов.",
            "manual_mvp": "Разобрать один инцидент и отдать карту причины и действий.",
            "manual_48h": True, "offer": "Диагностика одного CI-инцидента за 48 часов",
            "price_hypothesis": "5000–15000 ₽", "first_customer_experiment": "Написать авторам двух сообщений.",
            "acquisition_source": "DevOps-чаты", "next_50_source": "Активные DevOps/SRE и CI/CD чаты",
            "next_experiment": "Предложить пяти инженерам платный разбор.", "main_risk": "Редкие уникальные причины",
            "confidence": .86,
        }
        insight_id = await upsert_insight(cluster, rows)
        async with aiosqlite.connect(Config.DB_PATH) as db:
            async with db.execute(
                """SELECT unique_authors,unique_chats,total_messages,wtp_score,
                          reachability_score,first_money_score,manual_48h,trend
                   FROM verified_insights WHERE id=?""", (insight_id,)
            ) as cursor:
                saved = await cursor.fetchone()
        self.assertEqual(saved[0:4], (2, 2, 2, "HIGH"))
        self.assertGreater(saved[4], 0)
        self.assertGreater(saved[5], 0)
        self.assertEqual(saved[6], 1)
        self.assertIn(saved[7], {"NEW", "STABLE", "GROWING", "DECLINING"})


if __name__ == "__main__":
    unittest.main()
