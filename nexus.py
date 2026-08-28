import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from config import Config
from logging_utils import log_event
from runtime_settings import speed_label


RADAR_MODE = os.getenv("RADAR_MODE", "it").strip().lower()
CLASSIC_SCRIPTS = ["tg_bot.py", "scout.py", "miner.py", "butcher.py"]
MARGIN_SCRIPTS = ["margin_leak_radar.py"]
SPAIN_SCRIPTS = ["spain_relocation_radar.py", "spain_tg_bot.py"]
if RADAR_MODE in {"margin", "margin_leak", "anti_margin"}:
    SCRIPTS = MARGIN_SCRIPTS
elif RADAR_MODE in {"spain", "spain_relocation", "relocation_spain"}:
    SCRIPTS = SPAIN_SCRIPTS
else:
    SCRIPTS = CLASSIC_SCRIPTS
LOG_DIR = Path("logs") / datetime.now().strftime("%Y-%m-%d")
DB_PATH = Path(
    os.getenv("SPAIN_DB_PATH") or "data/spain_relocation.db"
    if RADAR_MODE in {"spain", "spain_relocation", "relocation_spain"}
    else Config.DB_PATH
)
LOCK_PATH = Path("nexus.lock")
HEARTBEAT_SECONDS = 60
NEXUS_LOG = None
LOCK_FILE = None


def open_log(script):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(script).stem
    return open(LOG_DIR / f"{name}.log", "a", encoding="utf-8", buffering=1)


def nexus_log(message):
    global NEXUS_LOG
    if NEXUS_LOG is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        NEXUS_LOG = open(LOG_DIR / "nexus.log", "a", encoding="utf-8", buffering=1)
    NEXUS_LOG.write(f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] {message}\n")


def acquire_single_instance_lock():
    global LOCK_FILE
    LOCK_FILE = open(LOCK_PATH, "a+", encoding="utf-8")
    LOCK_FILE.seek(0)

    try:
        import msvcrt

        msvcrt.locking(LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, OSError):
        log_event(
            "NEXUS",
            "ALREADY_RUNNING",
            "Nexus уже запущен. Вторую копию не запускаю, чтобы не путать логи и аккаунты",
            lock_path=LOCK_PATH.resolve(),
        )
        LOCK_FILE.close()
        sys.exit(2)
    except ImportError:
        pass

    LOCK_FILE.seek(0)
    LOCK_FILE.truncate()
    LOCK_FILE.write(f"pid={os.getpid()} started_at={datetime.now().isoformat(sep=' ', timespec='seconds')}\n")
    LOCK_FILE.flush()


def release_single_instance_lock():
    global LOCK_FILE
    if LOCK_FILE is None:
        return

    try:
        import msvcrt

        LOCK_FILE.seek(0)
        msvcrt.locking(LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass

    try:
        LOCK_FILE.close()
    finally:
        LOCK_FILE = None


def format_duration(seconds):
    if seconds is None:
        return "неизвестно"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def count_by_status(conn, table, status_column):
    rows = conn.execute(
        f"SELECT COALESCE({status_column}, 'NULL') AS status, COUNT(*) AS total "
        f"FROM {table} GROUP BY {status_column}"
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}


def scalar_count(conn, table):
    row = conn.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()
    return int(row["total"] if row else 0)


def table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn, table, column):
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def format_counts(counts, order):
    parts = []
    seen = set()
    for status in order:
        seen.add(status)
        parts.append(f"{status}={counts.get(status, 0)}")
    for status in sorted(set(counts) - seen):
        parts.append(f"{status}={counts[status]}")
    return ", ".join(parts) if parts else "пусто"


def build_account_report(conn):
    now = int(time.time())
    rows = conn.execute(
        "SELECT session_name, status, cooldown_until FROM accounts ORDER BY session_name"
    ).fetchall()

    ready = 0
    next_ready_in = None
    account_parts = []

    for row in rows:
        name = row["session_name"]
        status = row["status"] or "Unknown"
        cooldown_until = int(row["cooldown_until"] or 0)

        if status != "Banned" and cooldown_until <= now:
            ready += 1
            account_parts.append(f"{name}: готов")
            continue

        if status == "Banned":
            account_parts.append(f"{name}: забанен")
            continue

        wait_seconds = max(0, cooldown_until - now)
        if next_ready_in is None or wait_seconds < next_ready_in:
            next_ready_in = wait_seconds
        if status == "In_Use":
            label = "занят"
        elif status == "Cooldown":
            label = "отдыхает"
        else:
            label = status
        account_parts.append(f"{name}: {label}, будет доступен через {format_duration(wait_seconds)}")

    return {
        "total": len(rows),
        "ready": ready,
        "next_ready_in": next_ready_in,
        "accounts": "; ".join(account_parts) if account_parts else "аккаунтов нет",
    }


def scalar_value(conn, query, params=(), default=0):
    row = conn.execute(query, params).fetchone()
    if not row:
        return default
    value = row[0]
    return default if value is None else value


def build_margin_report(conn):
    now = int(time.time())
    global_pause_until = int(
        scalar_value(
            conn,
            "SELECT value FROM margin_runtime_state WHERE key = 'global_pause_until'",
            default=0,
        )
    )
    rollout_started_at = int(
        scalar_value(
            conn,
            "SELECT value FROM margin_runtime_state WHERE key = 'rollout_started_at'",
            default=now,
        )
    )
    tiers = {
        row["tier"]: {
            "total": int(row["total"]),
            "due": int(row["due"]),
            "next_due": int(row["next_due"] or 0),
        }
        for row in conn.execute(
            """
            SELECT tier,
                   COUNT(*) AS total,
                   SUM(CASE WHEN next_check_at <= ? THEN 1 ELSE 0 END) AS due,
                   MIN(CASE WHEN next_check_at > ? THEN next_check_at END) AS next_due
            FROM margin_radar_state
            GROUP BY tier
            """,
            (now, now),
        ).fetchall()
    }
    live_queue = (
        scalar_count(conn, "margin_live_queue")
        if table_exists(conn, "margin_live_queue")
        else 0
    )
    account_rows = conn.execute(
        """
        SELECT a.session_name, a.status, a.cooldown_until,
               s.role, s.blocked_until, s.last_flood_at, s.next_request_at,
               s.cycle_started_at, s.cycle_limit
        FROM accounts a
        LEFT JOIN margin_account_state s ON s.session_name = a.session_name
        ORDER BY a.session_name
        """
    ).fetchall()
    accounts = []
    for row in account_rows:
        blocked_until = max(
            int(row["cooldown_until"] or 0),
            int(row["blocked_until"] or 0),
        )
        next_request_at = int(row["next_request_at"] or 0)
        hourly = int(
            scalar_value(
                conn,
                """
                SELECT COUNT(*) FROM margin_request_log
                WHERE session_name = ? AND requested_at > ?
                """,
                (row["session_name"], now - 3600),
            )
        )
        daily = int(
            scalar_value(
                conn,
                """
                SELECT COUNT(*) FROM margin_request_log
                WHERE session_name = ? AND requested_at > ?
                """,
                (row["session_name"], now - 86400),
            )
        )
        accounts.append(
            {
                "name": row["session_name"],
                "role": row["role"] or "unknown",
                "status": row["status"] or "Unknown",
                "blocked_until": blocked_until,
                "next_request_at": next_request_at,
                "hourly": hourly,
                "daily": daily,
                "cycle_started_at": int(row["cycle_started_at"] or 0),
                "cycle_limit": int(row["cycle_limit"] or 0),
                "cycle_count": int(
                    scalar_value(
                        conn,
                        """
                        SELECT COUNT(*) FROM margin_request_log
                        WHERE session_name = ? AND requested_at >= ?
                        """,
                        (row["session_name"], int(row["cycle_started_at"] or 0)),
                    )
                ),
            }
        )
    windows = {}
    if table_exists(conn, "margin_screening_events"):
        for seconds, label in ((1200, "20m"), (3600, "1h"), (86400, "24h")):
            modifier = f"-{seconds} seconds"
            counts = {
                row["outcome"]: int(row["total"])
                for row in conn.execute(
                    """
                    SELECT outcome, COUNT(*) AS total
                    FROM margin_screening_events
                    WHERE updated_at >= datetime('now', ?)
                    GROUP BY outcome
                    """,
                    (modifier,),
                ).fetchall()
            }
            candidates = sum(counts.get(key, 0) for key in ("candidate", "strict", "notified"))
            strict = counts.get("strict", 0) + counts.get("notified", 0)
            windows[label] = {
                "screened": counts.get("screened", 0),
                "candidates": candidates,
                "strict": strict,
                "notified": counts.get("notified", 0),
                "conversion": strict / candidates if candidates else 0,
            }
    reasons = {}
    if table_exists(conn, "margin_screening_events"):
        reasons = {
            row["rejection_reason"]: int(row["total"])
            for row in conn.execute(
                """
                SELECT rejection_reason, COUNT(*) AS total
                FROM margin_screening_events
                WHERE rejection_reason IS NOT NULL
                  AND updated_at >= datetime('now', '-1 hour')
                GROUP BY rejection_reason
                ORDER BY total DESC
                """
            ).fetchall()
        }
    live_members = int(
        scalar_value(conn, "SELECT COUNT(*) FROM margin_radar_state WHERE live_member = 1")
    )
    hot_total = int(
        scalar_value(conn, "SELECT COUNT(*) FROM margin_radar_state WHERE tier = 'Hot'")
    )
    hot_due = int(
        scalar_value(
            conn,
            "SELECT COUNT(*) FROM margin_radar_state WHERE tier = 'Hot' AND next_check_at <= ?",
            (now,),
        )
    )
    oldest_hot_due = int(
        scalar_value(
            conn,
            """
            SELECT MIN(next_check_at) FROM margin_radar_state
            WHERE tier = 'Hot' AND next_check_at <= ?
            """,
            (now,),
        )
    )
    top_chats = [
        dict(row)
        for row in conn.execute(
            """
            SELECT t.title, s.tier, s.strict_yield_1000, s.strict_7d, s.messages_7d
            FROM margin_radar_state s
            JOIN target_chats t ON t.chat_id = s.chat_id
            ORDER BY s.priority_score DESC
            LIMIT 5
            """
        ).fetchall()
    ]
    return {
        "now": now,
        "global_pause_until": global_pause_until,
        "rollout_started_at": rollout_started_at,
        "tiers": tiers,
        "live_queue": live_queue,
        "accounts": accounts,
        "windows": windows,
        "reasons": reasons,
        "live_members": live_members,
        "fallback_polling": max(0, sum(item["total"] for item in tiers.values()) - live_members),
        "hot_total": hot_total,
        "hot_due": hot_due,
        "hot_age": max(0, now - oldest_hot_due) if oldest_hot_due else 0,
        "top_chats": top_chats,
    }


def log_margin_dashboard(report, margin):
    now = report["now"]
    tier_summary = ", ".join(
        f"{tier}={values['total']} (due={values['due']})"
        for tier, values in sorted(report["tiers"].items())
    )
    log_event(
        "NEXUS",
        "MARGIN_STATE",
        "Состояние безопасного радара",
        tiers=tier_summary or "пусто",
        live_queue=report["live_queue"],
        global_pause=(
            format_duration(report["global_pause_until"] - now)
            if report["global_pause_until"] > now
            else "нет"
        ),
        live_members=report["live_members"],
        fallback_polling=report["fallback_polling"],
        hot_queue=f"{report['hot_due']}/{report['hot_total']}",
        hot_age=format_duration(report["hot_age"]),
    )
    for label, values in report["windows"].items():
        log_event(
            "NEXUS",
            "MARGIN_FUNNEL",
            "Воронка LeadRadar",
            window=label,
            screened=values["screened"],
            candidates=values["candidates"],
            strict=values["strict"],
            notified=values["notified"],
            conversion=f"{values['conversion']:.0%}",
        )
    if report["reasons"]:
        log_event(
            "NEXUS",
            "MARGIN_REJECTIONS",
            "Причины отсева за час",
            reasons=", ".join(f"{key}={value}" for key, value in report["reasons"].items()),
        )
    if report["top_chats"]:
        log_event(
            "NEXUS",
            "MARGIN_TOP",
            "Свежий top источников",
            chats="; ".join(
                f"{row['title']} [{row['tier']}] yield={row['strict_yield_1000']:.1f}/1000"
                for row in report["top_chats"]
            ),
        )
    if margin:
        log_event(
            "NEXUS",
            "MARGIN_LEADS",
            "Лиды MarginLeakRadar",
            total=margin["total"],
            strong=margin["strong"],
            review=margin["review"],
            notified=margin["notified"],
            radars=", ".join(
                f"{name}={count}" for name, count in sorted(margin.get("radars", {}).items())
            ) or "n/a",
            temperatures=", ".join(
                f"{name}={count}" for name, count in sorted(margin.get("temperatures", {}).items())
            ) or "n/a",
            feedback=(
                f"{margin.get('feedback_good', 0)}/{margin.get('feedback_total', 0)}"
                if margin.get("feedback_total", 0)
                else "none"
            ),
            good_rate=(
                f"{margin['feedback_good'] / margin['feedback_total']:.0%}"
                if margin.get("feedback_total", 0)
                else "n/a"
            ),
        )

    for account in report["accounts"]:
        waits = []
        if account["blocked_until"] > now:
            waits.append(f"block={format_duration(account['blocked_until'] - now)}")
        if account["next_request_at"] > now:
            waits.append(f"next_request={format_duration(account['next_request_at'] - now)}")
        log_event(
            "NEXUS",
            "MARGIN_ACCOUNT",
            "Состояние аккаунта радара",
            account=account["name"],
            role=account["role"],
            client_status=account["status"],
            requests=f"{account['hourly']}/hour, {account['daily']}/day",
            cycle=f"{account['cycle_count']}/{account['cycle_limit']}",
            wait=", ".join(waits) if waits else "нет",
        )

    due_polling = sum(
        values["due"]
        for tier, values in report["tiers"].items()
        if tier != "Live"
    )
    if report["global_pause_until"] > now:
        log_event(
            "NEXUS",
            "MARGIN_NOW",
            "History polling на общей защитной паузе; live-events могут продолжаться",
            resume_in=format_duration(report["global_pause_until"] - now),
        )
    elif report["live_queue"]:
        log_event(
            "NEXUS",
            "MARGIN_NOW",
            "Обрабатывается сохранённая очередь live-сообщений",
            queued=report["live_queue"],
        )
    elif due_polling:
        log_event(
            "NEXUS",
            "MARGIN_NOW",
            "Есть чаты для безопасной проверки истории; запрос выполнится по бюджету аккаунта",
            chats_due=due_polling,
        )
    else:
        next_due_values = [
            values["next_due"]
            for tier, values in report["tiers"].items()
            if tier != "Live" and values["next_due"] > now
        ]
        next_due = min(next_due_values) if next_due_values else 0
        log_event(
            "NEXUS",
            "MARGIN_NOW",
            "Радар ждёт новые live-сообщения и следующую плановую проверку",
            next_poll_in=format_duration(next_due - now) if next_due else "не запланировано",
        )


def database_dashboard():
    if not DB_PATH.exists():
        log_event("NEXUS", "DASHBOARD", "База еще не создана", db_path=DB_PATH.resolve())
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        if RADAR_MODE in {"spain", "spain_relocation", "relocation_spain"}:
            sources = scalar_count(conn, "sources") if table_exists(conn, "sources") else 0
            confirmed = scalar_value(conn, "SELECT COUNT(*) FROM cases WHERE confirmed=1") if table_exists(conn, "cases") else 0
            candidates = scalar_value(conn, "SELECT COUNT(*) FROM cases WHERE confirmed=0") if table_exists(conn, "cases") else 0
            due = scalar_value(
                conn,
                "SELECT COUNT(*) FROM sources WHERE status='Active' AND next_check_at<=?",
                (int(time.time()),),
            ) if table_exists(conn, "sources") else 0
            log_event(
                "NEXUS", "SPAIN_DASHBOARD", "Spain Relocation Radar",
                sources=sources, confirmed_cases=confirmed, review_candidates=candidates, chats_due=due,
            )
            nexus_log(
                f"spain dashboard sources={sources} confirmed={confirmed} candidates={candidates} due={due}"
            )
            return
        with conn:
            chats = count_by_status(conn, "target_chats", "status")
            pains = count_by_status(conn, "pains", "butcher_status")
            keywords = count_by_status(conn, "keywords", "used")
            insights = scalar_count(conn, "verified_insights")
            trash = scalar_count(conn, "trash_bin")
            accounts = build_account_report(conn)
            margin = None
            margin_report = None
            if table_exists(conn, "margin_leads"):
                has_strict_lead_columns = all(
                    column_exists(conn, "margin_leads", column)
                    for column in ("filter_score", "radar_type", "temperature")
                )
                strong_query = (
                    """
                    SELECT COUNT(*) AS total
                    FROM margin_leads
                    WHERE verifier_passed = 1
                      AND filter_score >= 9
                    """
                    if has_strict_lead_columns
                    else
                    """
                    SELECT COUNT(*) AS total
                    FROM margin_leads
                    WHERE verifier_passed = 1
                    """
                )
                margin = {
                    "total": scalar_count(conn, "margin_leads"),
                    "notified": conn.execute("SELECT COUNT(*) AS total FROM margin_leads WHERE notified = 1").fetchone()["total"],
                    "strong": conn.execute(strong_query).fetchone()["total"],
                    "screened_out": conn.execute(
                        """
                        SELECT COUNT(*) AS total
                        FROM margin_leads
                        WHERE verifier_json IS NOT NULL
                          AND COALESCE(verifier_passed, 0) = 0
                        """
                    ).fetchone()["total"],
                    "review": conn.execute("SELECT COUNT(*) AS total FROM margin_leads WHERE verifier_passed = 0").fetchone()["total"],
                }
                if column_exists(conn, "margin_leads", "feedback"):
                    feedback = conn.execute(
                        """
                        SELECT COUNT(*) AS total,
                               SUM(CASE WHEN feedback = 'good' THEN 1 ELSE 0 END) AS good
                        FROM margin_leads
                        WHERE feedback IN ('good', 'trash')
                        """
                    ).fetchone()
                    margin["feedback_total"] = int(feedback["total"] or 0)
                    margin["feedback_good"] = int(feedback["good"] or 0)
                if has_strict_lead_columns:
                    margin["radars"] = {
                        row["radar_type"]: int(row["total"])
                        for row in conn.execute(
                            """
                            SELECT radar_type, COUNT(*) AS total
                            FROM margin_leads
                            WHERE verifier_passed = 1 AND filter_score >= 9
                            GROUP BY radar_type
                            """
                        ).fetchall()
                    }
                    margin["temperatures"] = {
                        row["temperature"]: int(row["total"])
                        for row in conn.execute(
                            """
                            SELECT temperature, COUNT(*) AS total
                            FROM margin_leads
                            WHERE verifier_passed = 1 AND filter_score >= 9
                            GROUP BY temperature
                            """
                        ).fetchall()
                    }
            if (
                RADAR_MODE in {"margin", "margin_leak", "anti_margin"}
                and table_exists(conn, "margin_radar_state")
                and table_exists(conn, "margin_account_state")
            ):
                margin_report = build_margin_report(conn)
    except sqlite3.Error as exc:
        log_event("NEXUS", "DASHBOARD_ERROR", "Не смог прочитать состояние базы", error=exc)
        return
    finally:
        if conn is not None:
            conn.close()

    chats_summary = format_counts(chats, ["New", "Processed", "Rejected", "Invalid"])
    pains_summary = format_counts(pains, ["Pending", "Processed", "LowSignal", "Rejected"])
    unused_keywords = keywords.get("0", 0)
    used_keywords = keywords.get("1", 0)

    if margin_report is not None:
        log_margin_dashboard(margin_report, margin)
        nexus_log(
            "margin dashboard "
            f"leads={margin['total'] if margin else 0} "
            f"notified={margin['notified'] if margin else 0} "
            f"live_queue={margin_report['live_queue']}"
        )
        return

    log_event(
        "NEXUS",
        "QUEUE",
        "Очередь и результаты",
        chats=chats_summary,
        pains=pains_summary,
        insights=insights,
        trash=trash,
        keywords=f"unused={unused_keywords}, used={used_keywords}",
    )
    if margin:
        log_event(
            "NEXUS",
            "MARGIN",
            "MarginLeakRadar leads",
            total=margin["total"],
            verified_strong=margin["strong"],
            screened_out=margin["screened_out"],
            review=margin["review"],
            notified=margin["notified"],
            feedback=(
                f"{margin.get('feedback_good', 0)}/{margin.get('feedback_total', 0)}"
                if margin.get("feedback_total", 0)
                else "none"
            ),
            good_rate=(
                f"{margin['feedback_good'] / margin['feedback_total']:.0%}"
                if margin.get("feedback_total", 0)
                else "n/a"
            ),
        )
    log_event(
        "NEXUS",
        "ACCOUNTS",
        "Telegram-аккаунты",
        ready=f"{accounts['ready']}/{accounts['total']}",
        details=accounts["accounts"],
    )

    chats_new = chats.get("New", 0)
    pains_pending = pains.get("Pending", 0)

    if accounts["ready"] == 0:
        if accounts["next_ready_in"] is None:
            log_event(
                "NEXUS",
                "WHY_IDLE",
                "Пауза: нет доступных Telegram-аккаунтов. Проверь accounts.json/БД",
            )
        else:
            log_event(
                "NEXUS",
                "WHY_IDLE",
                "Пауза не ошибка: Telegram-аккаунты заняты или отдыхают",
                next_account_ready_in=format_duration(accounts["next_ready_in"]),
            )
    elif chats_new > 0:
        log_event(
            "NEXUS",
            "WHAT_NOW",
            "Работает: miner должен читать новые чаты, scout параллельно ищет новые группы",
            chats_waiting=chats_new,
        )
    elif pains_pending > 0:
        log_event(
            "NEXUS",
            "WHAT_NOW",
            "Работает: butcher должен склеивать подтвержденные боли в инсайты",
            pains_waiting=pains_pending,
        )
    elif unused_keywords > 0:
        log_event(
            "NEXUS",
            "WHAT_NOW",
            "Работает: scout ищет новые группы по оставшимся запросам",
            keywords_waiting=unused_keywords,
        )
    else:
        log_event(
            "NEXUS",
            "WHAT_NOW",
            "Сейчас нечего обрабатывать: новых чатов и новых болей нет",
        )

    nexus_log(
        "dashboard "
        f"chats=({chats_summary}) pains=({pains_summary}) "
        f"insights={insights} trash={trash} accounts={accounts['ready']}/{accounts['total']}"
    )


def start_script(script):
    log_file = open_log(script)
    log_path = Path(log_file.name).resolve()
    log_file.write(f"\n\n[{datetime.now().isoformat(sep=' ', timespec='seconds')}] starting {script}\n")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("RADAR_SPEED", speed_label())
    env.setdefault("DB_LOGS", "0")
    process = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    log_event("NEXUS", "START_DAEMON", "Модуль запущен", script=script, pid=process.pid, log_path=log_path)
    nexus_log(f"started {script} pid={process.pid} log={log_path}")
    return {"process": process, "log_file": log_file, "log_path": log_path, "started_at": time.time()}


def stop_script(script, state):
    process = state["process"]
    log_event("NEXUS", "STOP_DAEMON", "Останавливаю модуль", script=script, pid=process.pid)
    nexus_log(f"stopping {script} pid={process.pid}")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log_event("NEXUS", "KILL_DAEMON", "Модуль не остановился сам, убиваю принудительно", script=script, pid=process.pid)
        process.kill()
    state["log_file"].close()


def heartbeat(active_processes):
    for script in SCRIPTS:
        state = active_processes.get(script)
        if not state:
            log_event("NEXUS", "HEARTBEAT", "Модуль отсутствует", script=script, status="нет")
            continue
        process = state["process"]
        status = "работает" if process.poll() is None else f"упал:{process.returncode}"
        uptime = int(time.time() - state["started_at"])
        log_event(
            "NEXUS",
            "HEARTBEAT",
            "Модуль жив",
            script=script,
            pid=process.pid,
            status=status,
            uptime_seconds=uptime,
            log_path=state["log_path"],
        )
    database_dashboard()
    nexus_log("heartbeat complete")


def manage_processes(active_processes):
    for script in SCRIPTS:
        state = active_processes.get(script)
        if state and state["process"].poll() is None:
            continue

        if state:
            exit_code = state["process"].returncode
            state["log_file"].write(f"[NEXUS] {script} exited with code {exit_code}; restarting.\n")
            state["log_file"].close()
            log_event("NEXUS", "RESTART_DAEMON", "Модуль упал, перезапускаю", script=script, exit_code=exit_code)
            nexus_log(f"restarting {script} exit_code={exit_code}")

        active_processes[script] = start_script(script)
        time.sleep(2)


def main():
    acquire_single_instance_lock()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_event(
        "NEXUS",
        "START",
        "24/7 оркестратор запускается",
        log_dir=LOG_DIR.resolve(),
        lock_path=LOCK_PATH.resolve(),
        mode=RADAR_MODE,
        scripts=",".join(SCRIPTS),
        speed=speed_label(),
    )
    nexus_log("orchestrator started")

    processes = {}

    try:
        while True:
            manage_processes(processes)
            heartbeat(processes)
            time.sleep(HEARTBEAT_SECONDS)
    except KeyboardInterrupt:
        log_event("NEXUS", "SHUTDOWN", "Остановка по Ctrl+C")
        nexus_log("shutdown requested")
    except Exception as exc:
        log_event("NEXUS", "CRASH", "Nexus упал; останавливаю модули", error=exc)
        nexus_log(f"crash error={exc}")
        raise
    finally:
        for script, state in list(processes.items()):
            stop_script(script, state)
        nexus_log("all instances stopped")
        if NEXUS_LOG:
            NEXUS_LOG.close()
        release_single_instance_lock()


if __name__ == "__main__":
    main()
