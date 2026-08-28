import os
from urllib.parse import unquote, urlparse


def _parse_proxy_url(proxy_url):
    value = (proxy_url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"

    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "socks4", "socks5"}:
        return None

    proxy = {
        "scheme": scheme,
        "hostname": parsed.hostname,
        "port": parsed.port,
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _windows_system_proxy_url():
    if os.name != "nt":
        return ""

    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            if not enabled:
                return ""
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except (OSError, ValueError):
        return ""

    if ";" not in raw and "=" not in raw:
        return raw

    entries = {}
    for item in raw.split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        entries[name.strip().lower()] = value.strip()
    return entries.get("https") or entries.get("http") or entries.get("socks") or ""


def telegram_proxy_url():
    """Return the active OS proxy URL without project-specific settings."""
    system_proxy = _windows_system_proxy_url()
    explicit = (
        os.getenv("ALL_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("all_proxy")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
    )
    value = (system_proxy or explicit or "").strip()
    if value and "://" not in value:
        value = f"http://{value}"
    return value or None


def telegram_proxy():
    """Return Pyrogram proxy settings from the active OS proxy configuration."""
    return _parse_proxy_url(telegram_proxy_url())
