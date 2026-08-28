import hashlib
import random
import re
from dataclasses import dataclass


TIER_LIVE = "Live"
TIER_HOT = "Hot"
TIER_WARM = "Warm"
TIER_COLD = "Cold"
TIER_QUARANTINE = "Quarantine"

ROLE_PRIMARY = "primary"
ROLE_RESERVE = "reserve"

RESERVE_ENABLE_AFTER_SECONDS = 72 * 3600
RESERVE_AFTER_PRIMARY_FLOOD_SECONDS = 6 * 3600

SPEED_PROFILES = {
    "safe": {
        "history_limit": 20,
        "primary_daily": 64,
        "reserve_hourly": 3,
        "reserve_daily": 16,
        "primary_interval": (8 * 60, 18 * 60),
        "reserve_interval": (20 * 60, 40 * 60),
    },
    "fast": {
        "history_limit": 50,
        "primary_hourly": 21,
        "primary_daily": 240,
        "reserve_hourly": 21,
        "reserve_daily": 240,
        "primary_interval": (90, 260),
        "reserve_interval": (90, 260),
    },
    "turbo": {
        "history_limit": 100,
        "primary_hourly": 30,
        "primary_daily": 400,
        "reserve_hourly": 15,
        "reserve_daily": 200,
        "primary_interval": (60, 110),
        "reserve_interval": (2 * 60, 4 * 60),
    },
}


@dataclass(frozen=True)
class FloodDecision:
    account_block_seconds: int
    global_pause_seconds: int
    stop_polling_for_day: bool


@dataclass(frozen=True)
class HistoryPageDecision:
    high_watermark: int
    next_offset_id: int
    committed_last_seen: int | None
    continue_backfill: bool
    new_message_count: int


def speed_profile(speed):
    return SPEED_PROFILES.get((speed or "fast").strip().lower(), SPEED_PROFILES["fast"])


def history_limit(speed):
    return int(speed_profile(speed)["history_limit"])


def rollout_hourly_limit(rollout_started_at, now, speed="safe"):
    profile = speed_profile(speed)
    if "primary_hourly" in profile:
        return int(profile["primary_hourly"])
    age = max(0, int(now) - int(rollout_started_at or now))
    if age < 24 * 3600:
        return 2
    if age < 48 * 3600:
        return 4
    return 5


def role_budget(role, rollout_started_at, now, speed="safe"):
    profile = speed_profile(speed)
    if role == ROLE_RESERVE:
        return int(profile["reserve_hourly"]), int(profile["reserve_daily"])
    return (
        rollout_hourly_limit(rollout_started_at, now, speed),
        int(profile["primary_daily"]),
    )


def budget_available(role, hourly_count, daily_count, rollout_started_at, now, speed="safe"):
    hourly_limit, daily_limit = role_budget(role, rollout_started_at, now, speed)
    return hourly_count < hourly_limit and daily_count < daily_limit


def request_interval_seconds(role, speed="safe", rng=None):
    rng = rng or random
    profile = speed_profile(speed)
    low, high = profile["reserve_interval" if role == ROLE_RESERVE else "primary_interval"]
    return int(rng.uniform(low, high))


def schedule_delay_seconds(tier, had_new_messages=False, rng=None):
    rng = rng or random
    if tier == TIER_HOT:
        return int(rng.uniform(20 * 60, 30 * 60))
    if tier == TIER_LIVE:
        return 60 * 60
    if tier == TIER_QUARANTINE:
        return 72 * 3600
    if tier == TIER_COLD:
        return 12 * 3600
    low, high = (110 * 60, 130 * 60) if had_new_messages else (220 * 60, 260 * 60)
    return int(rng.uniform(low, high))


def chat_poll_delay_seconds(tier, live_member=False, had_new_messages=False, rng=None):
    if tier == TIER_HOT and live_member:
        return 60 * 60
    return schedule_delay_seconds(tier, had_new_messages, rng)


def hourly_cycle_limit(rng=None):
    rng = rng or random
    return int(rng.randint(15, 21))


def flood_decision(wait_seconds, flood_events_last_24h):
    wait_seconds = max(1, int(wait_seconds))
    if wait_seconds < 60:
        account_block = int(wait_seconds * 1.5)
        global_pause = 30 * 60
    elif wait_seconds <= 3600:
        account_block = int(wait_seconds * 1.5)
        global_pause = 2 * 3600
    else:
        account_block = int(wait_seconds * 1.25)
        global_pause = 6 * 3600

    stop_for_day = int(flood_events_last_24h) >= 2
    if stop_for_day:
        global_pause = max(global_pause, 24 * 3600)
    return FloodDecision(account_block, global_pause, stop_for_day)


def reserve_allowed(
    *,
    now,
    rollout_started_at,
    primary_blocked_until,
    primary_last_flood_at,
    reserve_blocked_until,
):
    if int(now) - int(rollout_started_at or now) < RESERVE_ENABLE_AFTER_SECONDS:
        return False
    if int(primary_blocked_until or 0) <= int(now):
        return False
    if int(primary_last_flood_at or 0) <= 0:
        return False
    if int(now) - int(primary_last_flood_at) < RESERVE_AFTER_PRIMARY_FLOOD_SECONDS:
        return False
    return int(reserve_blocked_until or 0) <= int(now)


def normalized_text_hash(text):
    clean = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def jittered_timestamp(now, delay_seconds, rng=None):
    rng = rng or random
    factor = rng.uniform(0.7, 1.3)
    return int(now) + max(60, int(delay_seconds * factor))


def history_page_decision(baseline, previous_high_watermark, message_ids, limit):
    baseline = max(0, int(baseline or 0))
    previous_high_watermark = max(baseline, int(previous_high_watermark or 0))
    ids = [int(message_id) for message_id in message_ids]
    new_ids = [message_id for message_id in ids if message_id > baseline]
    high_watermark = max([previous_high_watermark, *new_ids])
    reached_cursor = any(message_id <= baseline for message_id in ids)
    continue_backfill = (
        len(ids) >= int(limit)
        and bool(new_ids)
        and not reached_cursor
    )
    if continue_backfill:
        return HistoryPageDecision(
            high_watermark=high_watermark,
            next_offset_id=min(new_ids),
            committed_last_seen=None,
            continue_backfill=True,
            new_message_count=len(new_ids),
        )
    return HistoryPageDecision(
        high_watermark=high_watermark,
        next_offset_id=0,
        committed_last_seen=high_watermark,
        continue_backfill=False,
        new_message_count=len(new_ids),
    )
