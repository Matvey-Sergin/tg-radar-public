import random
import unittest

from margin_radar_policy import (
    ROLE_PRIMARY,
    ROLE_RESERVE,
    TIER_COLD,
    TIER_HOT,
    TIER_LIVE,
    TIER_QUARANTINE,
    TIER_WARM,
    budget_available,
    flood_decision,
    history_limit,
    history_page_decision,
    hourly_cycle_limit,
    normalized_text_hash,
    reserve_allowed,
    role_budget,
    rollout_hourly_limit,
    schedule_delay_seconds,
    chat_poll_delay_seconds,
)


class MarginRadarPolicyTests(unittest.TestCase):
    def test_staged_primary_rollout(self):
        now = 1_000_000
        self.assertEqual(rollout_hourly_limit(now, now), 2)
        self.assertEqual(rollout_hourly_limit(now - 24 * 3600, now), 4)
        self.assertEqual(rollout_hourly_limit(now - 48 * 3600, now), 5)
        self.assertEqual(role_budget(ROLE_PRIMARY, now, now), (2, 64))
        self.assertEqual(role_budget(ROLE_RESERVE, now, now), (3, 16))

    def test_fast_profile_is_materially_faster(self):
        now = 1_000_000
        self.assertEqual(rollout_hourly_limit(now, now, "fast"), 21)
        self.assertEqual(role_budget(ROLE_PRIMARY, now, now, "fast"), (21, 240))
        self.assertEqual(role_budget(ROLE_RESERVE, now, now, "fast"), (21, 240))
        self.assertEqual(history_limit("fast"), 50)
        self.assertEqual(history_limit("turbo"), 100)

    def test_budgets_are_strict(self):
        now = 1_000_000
        self.assertTrue(budget_available(ROLE_PRIMARY, 1, 63, now, now))
        self.assertFalse(budget_available(ROLE_PRIMARY, 2, 63, now, now))
        self.assertFalse(budget_available(ROLE_PRIMARY, 1, 64, now, now))
        self.assertTrue(budget_available(ROLE_PRIMARY, 20, 239, now, now, "fast"))
        self.assertFalse(budget_available(ROLE_PRIMARY, 21, 239, now, now, "fast"))

    def test_flood_circuit_breaker(self):
        self.assertEqual(flood_decision(30, 1).global_pause_seconds, 30 * 60)
        self.assertEqual(flood_decision(600, 1).global_pause_seconds, 2 * 3600)
        self.assertEqual(flood_decision(7200, 1).global_pause_seconds, 6 * 3600)
        self.assertTrue(flood_decision(30, 2).stop_polling_for_day)
        self.assertEqual(flood_decision(30, 2).global_pause_seconds, 24 * 3600)

    def test_reserve_requires_age_and_flood_delay(self):
        now = 1_000_000
        kwargs = {
            "now": now,
            "rollout_started_at": now - 72 * 3600,
            "primary_blocked_until": now + 3600,
            "primary_last_flood_at": now - 6 * 3600,
            "reserve_blocked_until": 0,
        }
        self.assertTrue(reserve_allowed(**kwargs))
        self.assertFalse(
            reserve_allowed(**{**kwargs, "rollout_started_at": now - 71 * 3600})
        )
        self.assertFalse(
            reserve_allowed(**{**kwargs, "primary_last_flood_at": now - 5 * 3600})
        )

    def test_tier_intervals(self):
        rng = random.Random(7)
        self.assertEqual(schedule_delay_seconds(TIER_LIVE, rng=rng), 3600)
        self.assertTrue(20 * 60 <= schedule_delay_seconds(TIER_HOT, rng=rng) <= 30 * 60)
        self.assertEqual(chat_poll_delay_seconds(TIER_HOT, live_member=True, rng=rng), 3600)
        self.assertEqual(
            schedule_delay_seconds(TIER_QUARANTINE, rng=rng), 72 * 3600
        )
        self.assertTrue(
            schedule_delay_seconds(TIER_COLD, rng=rng) == 12 * 3600
        )
        self.assertTrue(
            220 * 60 <= schedule_delay_seconds(TIER_WARM, rng=rng) <= 260 * 60
        )
        self.assertTrue(
            110 * 60
            <= schedule_delay_seconds(TIER_WARM, had_new_messages=True, rng=rng)
            <= 130 * 60
        )

    def test_random_hourly_cycle_limit_is_between_15_and_21(self):
        rng = random.Random(17)
        values = {hourly_cycle_limit(rng) for _ in range(100)}
        self.assertTrue(values.issubset(set(range(15, 22))))
        self.assertGreater(len(values), 1)

    def test_history_cursor_is_not_committed_until_backfill_finishes(self):
        first_page = history_page_decision(
            baseline=100,
            previous_high_watermark=100,
            message_ids=list(range(140, 120, -1)),
            limit=20,
        )
        self.assertTrue(first_page.continue_backfill)
        self.assertEqual(first_page.next_offset_id, 121)
        self.assertEqual(first_page.high_watermark, 140)
        self.assertIsNone(first_page.committed_last_seen)

        final_page = history_page_decision(
            baseline=100,
            previous_high_watermark=140,
            message_ids=list(range(120, 99, -1)),
            limit=20,
        )
        self.assertFalse(final_page.continue_backfill)
        self.assertEqual(final_page.committed_last_seen, 140)
        self.assertEqual(final_page.next_offset_id, 0)

    def test_history_page_stops_when_cursor_is_seen_at_limit_boundary(self):
        page = history_page_decision(
            baseline=100,
            previous_high_watermark=100,
            message_ids=list(range(119, 99, -1)),
            limit=20,
        )
        self.assertFalse(page.continue_backfill)
        self.assertEqual(page.committed_last_seen, 119)

    def test_normalized_hash_deduplicates_whitespace_and_case(self):
        self.assertEqual(
            normalized_text_hash("  Комиссия   выросла "),
            normalized_text_hash("комиссия выросла"),
        )


if __name__ == "__main__":
    unittest.main()
