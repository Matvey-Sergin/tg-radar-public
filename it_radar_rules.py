"""Domain rules and deterministic scoring for IT Pain Radar.

The LLM extracts semantics; this module keeps ranking explainable and prevents
keywords or a single prolific author from becoming the evidence unit.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone


IT_SEGMENTS = [
    "Backend", "Frontend", "Fullstack", "Python", "Java", "Go", "Node.js",
    "C#/.NET", "React", "DevOps", "SRE", "QA", "QA Automation",
    "Data Engineering", "Data Science", "ML Engineering", "AI Engineering",
    "Mobile", "Android", "iOS", "System Administration", "Tech Lead",
    "Team Lead", "Engineering Management", "Software Architecture", "Senior Developers",
]

IT_SEARCH_QUERIES = [
    "чат backend разработчиков", "чат frontend разработчиков", "чат fullstack разработчиков",
    "чат python разработчиков", "чат java разработчиков", "чат golang разработчиков",
    "чат node js разработчиков", "чат c# разработчиков", "чат react разработчиков",
    "чат devops", "чат sre", "чат qa инженеров", "чат автоматизаторов qa",
    "чат data engineers", "чат data scientists", "чат machine learning инженеров",
    "чат ai разработчиков", "чат android разработчиков", "чат ios разработчиков",
    "чат тимлидов", "чат tech lead", "чат engineering managers",
    "чат senior разработчиков", "чат архитекторов ПО", "чат сисадминов",
    "чат программистов удаленная работа", "чат разработчиков релокация",
    "чат программистов поиск работы", "чат разработчиков собеседования",
    "чат программистов freelancing", "чат разработчиков AI tools",
    "чат kubernetes devops", "чат ci cd", "чат observability",
    "чат code review", "чат engineering management", "чат разработчиков legacy",
]

PAIN_CATEGORIES = [
    "manual_repetitive_work", "time_loss", "tool_gap", "solution_seeking",
    "homemade_workaround", "deployment", "debugging", "ci_cd", "environment_setup",
    "infrastructure", "testing", "monitoring", "documentation", "dependencies",
    "permissions", "cloud_costs", "migrations", "codebase_search", "code_review",
    "onboarding", "task_management", "logs", "career", "hiring", "other",
]

PAIN_MARKERS = [
    "бесит", "задолбал", "задолбался", "достало", "неудобно", "долго", "вручную",
    "руками", "каждый раз", "постоянно", "не могу", "не получается", "проблем",
    "боль", "костыл", "скрипт", "автоматиз", "есть сервис", "какой сервис",
    "есть инструмент", "чем пользуетесь", "чем заменить", "кто решил",
    "как вы решаете", "как сделать", "как найти", "как проверить", "подскажите",
    "посоветуйте", "помогите", "кто может", "ищу человека", "готов заплатить",
    "оплачу", "сколько стоит", "трачу время", "час", "целый день",
]

IT_TOPIC_SIGNALS = [
    "backend", "frontend", "fullstack", "python", "java", "golang", " go ", "node",
    "javascript", "typescript", "c#", ".net", "react", "vue", "angular", "devops", "девопс", "sre", "сре",
    "qa", "тестиров", "data engineer", "data scien", "machine learning", " ml ", " ai ",
    "android", "ios", "mobile", "разработ", "программист", "айти", " it ", "tech lead",
    "тимлид", "team lead", "engineering manager", "архитектор", "сисадмин", "kubernetes",
    "docker", "linux", "cloud", "ci/cd", "github", "gitlab", "код", "инженер",
    "devsecops", "security", "инфобез", "безопасност",
    "релокац", "remote", "удален", "удалён", "собеседован", "карьер", "фриланс",
]

PROFESSIONAL_CONTEXT_SIGNALS = [
    "deploy", "депло", "debug", "отлад", "ci", "pipeline", "пайплайн", "monitor",
    "observability", "лог", "инфраструкт", "кластер", "прод", "production", "релиз",
    "тест", "code review", "ревью", "документац", "dependency", "зависимост",
    "миграц", "legacy", "легаси", "репозитор", "codebase", "команд", "спринт",
    "onboarding", "онбординг", "облако", "aws", "gcp", "azure", "kubernetes",
    "docker", "terraform", "ansible", "jira", "github", "gitlab", "api", "сервер",
]

WTP_HIGH_MARKERS = [
    "готов заплатить", "готов платить", "оплачу", "сколько стоит", "платный сервис",
    "ищу специалист", "ищу человека", "нужен специалист", "кто может сделать", "нанять",
]

WTP_MEDIUM_MARKERS = [
    "написал скрипт", "написала скрипт", "свой скрипт", "костыл", "таблиц",
    "workflow", "воркфлоу", "каждый раз", "регулярно", "постоянно", "час",
    "вручную", "руками", "пробовал", "пробовала", "не устраивает",
]

FALSE_PAIN_PATTERNS = [
    r"(?:домашн(?:ее|яя) задани|лабораторн(?:ая|ую)|курсов(?:ая|ую))",
    r"(?:почему|как)\s+.{0,45}(?:не компилируется|syntax error|синтаксис)",
    r"(?:объясните|что такое)\s+.{0,55}$",
    r"\b(?:начинающ|нович(?:ок|ку|ка))\b.{0,80}\b(?:как|куда|совет|начать|изучать)\b",
    r"\b(?:как|с чего)\s+(?:лучше\s+)?(?:начать|изучить|выучить)\b",
    r"\b(?:курс|урок|учебн|видео)\b.{0,100}\b(?:ссылк|материал|домашк)\b",
    r"\b(?:ссылк[аи]?\s+не\s+работа|как\s+найти\s+материал)\b",
    r"^\s*(?:спасибо|ок|понял|поняла|\+|да|нет)[.!?\s]*$",
]


def normalize_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def has_any(value, signals) -> bool:
    clean = normalize_text(value)
    return any(signal in clean for signal in signals)


def anonymize_author(chat_id, author_id) -> str | None:
    if author_id in (None, ""):
        return None
    raw = f"{chat_id}:{author_id}".encode("utf-8")
    return "usr_" + hashlib.sha256(raw).hexdigest()[:16]


def normalize_wtp(value, text="") -> str:
    clean_value = normalize_text(value)
    clean_text = normalize_text(text)
    if clean_value in {"high", "высокий", "высокая"} or has_any(clean_text, WTP_HIGH_MARKERS):
        return "HIGH"
    if clean_value in {"medium", "средний", "средняя"} or has_any(clean_text, WTP_MEDIUM_MARKERS):
        return "MEDIUM"
    return "LOW"


def _level(value, mapping, default=0.0) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return max(0.0, min(1.0, number / 100 if number > 1 else number))
    return mapping.get(normalize_text(value), default)


LEVELS = {
    "unknown": 0.0, "неизвестно": 0.0, "none": 0.0,
    "low": 0.25, "низкая": 0.25, "низкий": 0.25,
    "medium": 0.6, "средняя": 0.6, "средний": 0.6,
    "high": 0.9, "высокая": 0.9, "высокий": 0.9,
    "daily": 1.0, "ежедневно": 1.0, "weekly": 0.75, "еженедельно": 0.75,
    "monthly": 0.45, "ежемесячно": 0.45, "once": 0.1, "разово": 0.1,
}


def pain_score(signal: dict) -> int:
    """Return an explainable 0..100 score without inventing unknown values."""
    text = " ".join(str(signal.get(key) or "") for key in (
        "original_message", "pain_summary", "current_workaround", "estimated_time_cost",
        "estimated_money_cost", "potential_wtp",
    ))
    wtp = normalize_wtp(signal.get("potential_wtp"), text)
    explicit_wtp = bool(signal.get("explicit_wtp_indicator")) or wtp == "HIGH"
    manual = bool(signal.get("manual_work_indicator")) or has_any(text, WTP_MEDIUM_MARKERS)
    tool_failure = bool(signal.get("existing_tool_failure"))
    solution_seeking = bool(signal.get("solution_seeking_indicator"))
    components = {
        "frequency": _level(signal.get("frequency"), LEVELS),
        "severity": _level(signal.get("severity") or signal.get("emotional_intensity"), LEVELS),
        "time": _level(signal.get("time_cost_score") or signal.get("estimated_time_cost"), LEVELS, 0.55 if "час" in normalize_text(text) else 0),
        "money": _level(signal.get("money_cost_score") or signal.get("estimated_money_cost"), LEVELS),
        "urgency": _level(signal.get("urgency"), LEVELS),
        "effort": max(float(manual) * 0.75, float(tool_failure) * 0.65, float(solution_seeking) * 0.55),
        "wtp": 1.0 if explicit_wtp else 0.65 if wtp == "MEDIUM" else 0.2 if wtp == "LOW" else 0,
        "repeatability": _level(signal.get("repeatability"), LEVELS),
        "solvability": _level(signal.get("solvability"), LEVELS, 0.5 if manual else 0),
        "reachability": _level(signal.get("reachability"), LEVELS),
    }
    weights = {"frequency": 12, "severity": 12, "time": 10, "money": 8, "urgency": 10,
               "effort": 10, "wtp": 15, "repeatability": 10, "solvability": 6, "reachability": 7}
    return max(0, min(100, round(sum(components[key] * weights[key] for key in weights))))


def reachability_score(unique_chats: int, unique_authors: int, recent_signals: int,
                       source_quality: float = 0, intent_ratio: float = 0,
                       active_audience: int = 0, activity: float = 0) -> int:
    chats = min(1.0, math.log1p(max(0, unique_chats)) / math.log(8))
    authors = min(1.0, math.log1p(max(0, unique_authors)) / math.log(21))
    recent = min(1.0, max(0, recent_signals) / 10)
    quality = max(0.0, min(1.0, float(source_quality or 0) / 100))
    intent = max(0.0, min(1.0, float(intent_ratio or 0)))
    base = 100 * (0.25 * chats + 0.25 * authors + 0.2 * recent + 0.15 * quality + 0.15 * intent)
    audience = min(1.0, math.log1p(max(0, active_audience)) / math.log(5001))
    activity_factor = max(0.0, min(1.0, float(activity or 0) / 100))
    # Audience/activity are supporting evidence; they can boost but never hide a
    # lack of independent chats and authors.
    return round(min(100, base + 10 * (0.6 * audience + 0.4 * activity_factor)))


def first_money_score(pain: int, wtp: str, reachability: int, manual_48h: bool,
                      recent_signals: int, unique_authors: int) -> int:
    wtp_points = {"LOW": 20, "MEDIUM": 65, "HIGH": 100}.get(normalize_wtp(wtp), 20)
    freshness = min(100, max(0, recent_signals) * 12)
    independent = min(100, max(0, unique_authors) * 8)
    return round(0.25 * pain + 0.2 * wtp_points + 0.2 * reachability +
                 0.15 * (100 if manual_48h else 0) + 0.1 * freshness + 0.1 * independent)


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def trend_from_dates(values, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    ages = [(now - dt).days for dt in (parse_datetime(value) for value in values) if dt and dt <= now]
    if not ages:
        return "UNKNOWN"
    last_30 = sum(age <= 30 for age in ages)
    prior_30 = sum(30 < age <= 60 for age in ages)
    if last_30 and not prior_30:
        return "NEW"
    if prior_30 == 0:
        return "STABLE"
    ratio = last_30 / prior_30
    if ratio >= 1.35:
        return "GROWING"
    if ratio <= 0.65:
        return "DECLINING"
    return "STABLE"
