import unittest

from lead_radar_rules import (
    RADAR_FBS_CHAOS,
    RADAR_FROZEN_STOCK,
    detect_radar_type,
    second_layer_score,
    service_offer_rejection_reason,
)
from margin_leak_radar import hard_rejection_reason, verifier_passed


class MarginVerifierTests(unittest.TestCase):
    def setUp(self):
        self.strong_lead = {
            "actor_type": "seller",
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
            "confidence": 0.9,
        }

    def test_strong_personal_loss_passes(self):
        self.assertTrue(verifier_passed(self.strong_lead))
        self.assertEqual(second_layer_score(self.strong_lead), 10)

    def test_missing_personal_case_or_loss_does_not_pass(self):
        self.assertFalse(verifier_passed({**self.strong_lead, "personal_case": False}))
        self.assertFalse(verifier_passed({**self.strong_lead, "money_or_ops_loss": False}))

    def test_low_confidence_caps_second_score_below_strict(self):
        result = {**self.strong_lead, "confidence": 0.70}
        self.assertEqual(second_layer_score(result), 8)
        self.assertFalse(verifier_passed(result))

    def test_ads_and_non_problems_do_not_pass(self):
        self.assertFalse(verifier_passed({**self.strong_lead, "is_ad": True}))
        self.assertFalse(verifier_passed({**self.strong_lead, "current_problem": False}))
        self.assertFalse(verifier_passed({**self.strong_lead, "seeks_solution": False}))
        self.assertFalse(
            verifier_passed(
                {
                    **self.strong_lead,
                    "actor_type": "service_provider",
                    "is_seller": False,
                }
            )
        )

    def test_accounting_and_infographic_offers_are_auto_ignored(self):
        accounting = (
            "Я бухгалтер с опытом 25 лет. Поможем вашему бизнесу снизить налоги "
            "и разобраться с комиссиями Wildberries. Пишите в ЛС."
        )
        infographic = (
            "Сделаю инфографику и дизайн карточек для WB. Скину портфолио, "
            "стоимость от 5000 руб, пишите в личку."
        )
        self.assertIsNotNone(service_offer_rejection_reason(accounting))
        self.assertIsNotNone(service_offer_rejection_reason(infographic))
        self.assertIsNotNone(hard_rejection_reason(accounting, "Чат селлеров"))
        self.assertIsNotNone(hard_rejection_reason(infographic, "Чат селлеров"))

    def test_frozen_stock_case_is_not_locally_rejected(self):
        text = (
            "У меня товар приняли на складе Электросталь, но он заморожен и не в продаже. "
            "Хранение списывают, продажи встали, поддержка молчит. Кто сталкивался?"
        )
        self.assertIsNone(hard_rejection_reason(text, "Чат поставщиков"))
        self.assertEqual(detect_radar_type(text), RADAR_FROZEN_STOCK)

    def test_fbs_case_has_own_radar(self):
        text = (
            "У меня FBS поставка не добавляется в ПВЗ, статус не меняется, "
            "сроки горят и будет штраф. Подскажите, что делать?"
        )
        self.assertIsNone(hard_rejection_reason(text, "Чат селлеров"))
        self.assertEqual(detect_radar_type(text), RADAR_FBS_CHAOS)


if __name__ == "__main__":
    unittest.main()
