import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


SPEED = os.getenv("RADAR_SPEED", "fast").strip().lower()

PROFILES = {
    "safe": {
        "MINER_MAX_HISTORY": 220,
        "MINER_CHUNK_CHAR_LIMIT": 3000,
        "MINER_CHATS_PER_ACCOUNT": 3,
        "MINER_ACCOUNT_COOLDOWN": 1200,
        "MINER_NO_ACCOUNT_SLEEP": 60,
        "MINER_IDLE_SLEEP": 30,
        "MINER_CHUNK_SLEEP": 2,
        "MINER_JITTER_MIN": 25,
        "MINER_JITTER_MAX": 60,
        "MINER_RECHECK_EMPTY_SECONDS": 21600,
        "MINER_RECHECK_FEW_SECONDS": 10800,
        "MINER_RECHECK_ACTIVE_SECONDS": 5400,
        "SCOUT_REQUESTS_PER_ACCOUNT": 4,
        "SCOUT_ACCOUNT_COOLDOWN": 1800,
        "SCOUT_NO_ACCOUNT_SLEEP": 60,
        "SCOUT_KEYWORD_GEN_SLEEP": 120,
        "SCOUT_SAVED_CHAT_SLEEP": 2,
        "SCOUT_ERROR_SLEEP": 10,
        "SCOUT_JITTER_MIN": 240,
        "SCOUT_JITTER_MAX": 480,
        "BUTCHER_BATCH_LIMIT": 6,
        "BUTCHER_MIN_VALID_ROWS": 3,
        "BUTCHER_EVIDENCE_CHARS": 220,
        "BUTCHER_CONTEXT_CHARS": 220,
        "BUTCHER_EMPTY_SLEEP": 300,
        "BUTCHER_NOT_ENOUGH_SLEEP": 300,
        "BUTCHER_INVALID_RESPONSE_SLEEP": 60,
        "BUTCHER_CYCLE_SLEEP": 60,
    },
    "fast": {
        "MINER_MAX_HISTORY": 280,
        "MINER_CHUNK_CHAR_LIMIT": 3400,
        "MINER_CHATS_PER_ACCOUNT": 3,
        "MINER_ACCOUNT_COOLDOWN": 900,
        "MINER_NO_ACCOUNT_SLEEP": 45,
        "MINER_IDLE_SLEEP": 25,
        "MINER_CHUNK_SLEEP": 1.5,
        "MINER_JITTER_MIN": 18,
        "MINER_JITTER_MAX": 45,
        "MINER_RECHECK_EMPTY_SECONDS": 21600,
        "MINER_RECHECK_FEW_SECONDS": 7200,
        "MINER_RECHECK_ACTIVE_SECONDS": 3600,
        "SCOUT_REQUESTS_PER_ACCOUNT": 6,
        "SCOUT_ACCOUNT_COOLDOWN": 1200,
        "SCOUT_NO_ACCOUNT_SLEEP": 45,
        "SCOUT_KEYWORD_GEN_SLEEP": 60,
        "SCOUT_SAVED_CHAT_SLEEP": 1,
        "SCOUT_ERROR_SLEEP": 5,
        "SCOUT_JITTER_MIN": 120,
        "SCOUT_JITTER_MAX": 240,
        "BUTCHER_BATCH_LIMIT": 8,
        "BUTCHER_MIN_VALID_ROWS": 3,
        "BUTCHER_EVIDENCE_CHARS": 240,
        "BUTCHER_CONTEXT_CHARS": 240,
        "BUTCHER_EMPTY_SLEEP": 120,
        "BUTCHER_NOT_ENOUGH_SLEEP": 90,
        "BUTCHER_INVALID_RESPONSE_SLEEP": 45,
        "BUTCHER_CYCLE_SLEEP": 45,
    },
    "turbo": {
        "MINER_MAX_HISTORY": 350,
        "MINER_CHUNK_CHAR_LIMIT": 3600,
        "MINER_CHATS_PER_ACCOUNT": 5,
        "MINER_ACCOUNT_COOLDOWN": 600,
        "MINER_NO_ACCOUNT_SLEEP": 15,
        "MINER_IDLE_SLEEP": 8,
        "MINER_CHUNK_SLEEP": 0.5,
        "MINER_JITTER_MIN": 8,
        "MINER_JITTER_MAX": 20,
        "MINER_RECHECK_EMPTY_SECONDS": 10800,
        "MINER_RECHECK_FEW_SECONDS": 3600,
        "MINER_RECHECK_ACTIVE_SECONDS": 1800,
        "SCOUT_REQUESTS_PER_ACCOUNT": 8,
        "SCOUT_ACCOUNT_COOLDOWN": 900,
        "SCOUT_NO_ACCOUNT_SLEEP": 15,
        "SCOUT_KEYWORD_GEN_SLEEP": 30,
        "SCOUT_SAVED_CHAT_SLEEP": 0.5,
        "SCOUT_ERROR_SLEEP": 3,
        "SCOUT_JITTER_MIN": 60,
        "SCOUT_JITTER_MAX": 150,
        "BUTCHER_BATCH_LIMIT": 8,
        "BUTCHER_MIN_VALID_ROWS": 3,
        "BUTCHER_EVIDENCE_CHARS": 240,
        "BUTCHER_CONTEXT_CHARS": 240,
        "BUTCHER_EMPTY_SLEEP": 60,
        "BUTCHER_NOT_ENOUGH_SLEEP": 60,
        "BUTCHER_INVALID_RESPONSE_SLEEP": 20,
        "BUTCHER_CYCLE_SLEEP": 15,
    },
}


ACTIVE_PROFILE = PROFILES.get(SPEED, PROFILES["fast"])


def _coerce_value(raw_value, default_value):
    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return int(float(raw_value))
    if isinstance(default_value, float):
        return float(raw_value)
    return raw_value


def setting(name):
    default_value = ACTIVE_PROFILE[name]
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default_value
    try:
        return _coerce_value(raw_value.strip(), default_value)
    except ValueError:
        return default_value


def speed_label():
    return SPEED if SPEED in PROFILES else "fast"


def settings_summary():
    return {name: setting(name) for name in sorted(ACTIVE_PROFILE)}
