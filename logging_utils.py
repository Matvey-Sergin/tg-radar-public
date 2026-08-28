import sys
from datetime import datetime


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


SECRET_KEYS = {
    "api_hash",
    "api_key",
    "authorization",
    "bot_token",
    "password",
    "session",
    "session_string",
    "token",
}


def _is_secret_key(key):
    key = key.lower()
    if key in SECRET_KEYS:
        return True
    return key.endswith("_token") or key.endswith("_secret") or key.endswith("_password")


def _compact(value, limit=300):
    if value is None:
        return "None"
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def log_event(module, stage, message, **fields):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    parts = [f"[{timestamp}]", f"[{module}]", f"[{stage}]", message]
    safe_fields = []
    for key, value in fields.items():
        if _is_secret_key(key):
            value = "***"
        safe_fields.append(f"{key}={_compact(value)}")
    if safe_fields:
        parts.append("| " + " ".join(safe_fields))
    line = " ".join(parts)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)
