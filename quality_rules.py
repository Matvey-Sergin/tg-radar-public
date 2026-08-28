"""Deterministic guardrails for Russian-speaking professional IT chats."""

import re

from it_radar_rules import (
    FALSE_PAIN_PATTERNS,
    IT_TOPIC_SIGNALS,
    PAIN_MARKERS,
    PROFESSIONAL_CONTEXT_SIGNALS,
    WTP_HIGH_MARKERS,
    WTP_MEDIUM_MARKERS,
    has_any,
    normalize_text,
)


# Backward-compatible name used by old maintenance code and log fields.
SELLER_SIGNALS = IT_TOPIC_SIGNALS
PAIN_SIGNALS = PAIN_MARKERS + PROFESSIONAL_CONTEXT_SIGNALS

CHAT_REJECT_SIGNALS = [
    "крипт", "crypto", "casino", "казино", "ставки на спорт", "мемы", "мемас",
    "анекдот", "знакомства", "барахолка", "промокод", "скидки и акции", "новости it",
    "дайджест", "вакансии без флуда", "только вакансии", "резюме и вакансии",
]

PROMO_PATTERNS = [
    r"\b(?:продаю|предлагаю)\b.{0,80}\b(?:курс|обучение|наставничество|услуг)",
    r"\b(?:пишите|напишите)\s+(?:мне\s+)?(?:в\s+)?(?:лс|личк)\b.{0,80}\b(?:цен|прайс|портфолио)",
    r"\b(?:скидк|промокод)\b.{0,80}\b(?:курс|интенсив|вебинар)",
]

VACANCY_AD_PATTERNS = [
    r"^.{0,50}\bваканси[яи]\b.{0,100}\b(?:зарплат|оклад|вилка|отклик|резюме|hr)\b",
    r"\bищем\s+(?:senior|middle|junior|разработчик|инженер|devops|qa|аналитик)\b",
    r"\b(?:отклик|резюме)\s+(?:в|на)\s+(?:лс|почт|hh)\b",
]

NOISE_PATTERNS = [
    r"^\s*(?:спасибо|спс|ок|ага|понял|поняла|\+|\+1|да|нет|лол|кек)[.!?\s]*$",
    r"\b(?:usdt|airdrop|токенсейл|казино|букмекер)\b",
]

GENERIC_CORE_PROBLEMS = {
    "разработчикам сложно программировать", "проблемы разработчиков", "сложности в it",
    "нехватка инструментов", "рутинные задачи", "низкая зарплата", "сложная разработка",
}


def has_pattern(text, patterns):
    clean = normalize_text(text)
    return any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in patterns)


def clip_text(value, limit=700):
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def noise_rejection_reason(text, chat_title=""):
    clean = normalize_text(text)
    if not clean:
        return None
    if has_pattern(clean, NOISE_PATTERNS):
        return "low-context/noise message"
    if has_pattern(clean, PROMO_PATTERNS):
        return "service/course/promo offer"
    if has_pattern(clean, VACANCY_AD_PATTERNS):
        return "vacancy advertisement, not an affected person's pain"
    return None


def title_rejection_reason(title):
    clean = f" {normalize_text(title)} "
    if has_any(clean, CHAT_REJECT_SIGNALS):
        return "title indicates news/jobs/crypto/entertainment noise"
    if not has_any(clean, IT_TOPIC_SIGNALS):
        return "title has no professional IT signal"
    return None


def is_seller_chat_title(title):
    """Compatibility alias: now means a suitable professional IT chat title."""
    return title_rejection_reason(title) is None


def message_quality_counts(texts):
    result = {"total": len(texts), "it": 0, "seller": 0, "pain": 0, "reject": 0,
              "noise": 0, "job": 0, "logistics_order": 0, "substantive": 0}
    for text in texts:
        clean = f" {normalize_text(text)} "
        it_hit = has_any(clean, IT_TOPIC_SIGNALS + PROFESSIONAL_CONTEXT_SIGNALS)
        pain_hit = has_any(clean, PAIN_MARKERS + WTP_HIGH_MARKERS + WTP_MEDIUM_MARKERS)
        reason = noise_rejection_reason(clean)
        if it_hit:
            result["it"] += 1
            result["seller"] += 1
        if pain_hit:
            result["pain"] += 1
        if len(clean.split()) >= 6 and reason is None:
            result["substantive"] += 1
        if reason:
            result["noise"] += 1
            result["reject"] += 1
            if reason.startswith("vacancy"):
                result["job"] += 1
    return result


def chat_activity_rejection_reason(title, texts):
    title_reason = title_rejection_reason(title)
    if title_reason:
        return title_reason
    counts = message_quality_counts(texts)
    total = max(counts["total"], 1)
    if counts["total"] < 15:
        return "not enough real messages"
    if counts["noise"] / total > 0.35:
        return "too much advertising, vacancy or low-context noise"
    if counts["substantive"] / total < 0.25:
        return "too few substantive discussions"
    if counts["it"] / total < 0.08 and counts["pain"] / total < 0.08:
        return "history does not look like a professional IT discussion"
    return None


def pain_rejection_reason(title, evidence, context="", chat_title=""):
    clean_title = normalize_text(title)
    clean_evidence = normalize_text(evidence)
    combined = f" {normalize_text(f'{chat_title} {title} {evidence} {context}')} "
    if len(clean_title) < 18 or len(clean_title.split()) < 3:
        return "pain summary too short or generic"
    if len(clean_evidence.split()) < 6:
        return "evidence too short"
    reason = noise_rejection_reason(f"{evidence} {context}", chat_title)
    if reason:
        return reason
    if has_pattern(clean_evidence, FALSE_PAIN_PATTERNS):
        return "one-off/basic technical question"
    if not has_any(combined, IT_TOPIC_SIGNALS + PROFESSIONAL_CONTEXT_SIGNALS):
        return "no professional IT context"
    # Markers are retrieval aids, not a hard requirement. A semantic LLM summary
    # that explicitly describes recurring cost/obstacle is also sufficient.
    semantic_pain = has_any(clean_title, [
        "теряют время", "приходится", "повтор", "не могут", "мешает", "дорого",
        "нет инструмента", "неудоб", "сложно", "ручн", "ошиб", "срыв", "риск",
    ])
    if not semantic_pain and not has_any(combined, PAIN_MARKERS + WTP_HIGH_MARKERS + WTP_MEDIUM_MARKERS):
        return "no repeatable obstacle or cost evidence"
    if clean_title in GENERIC_CORE_PROBLEMS:
        return "pain summary is too broad"
    return None


def is_quality_pain(title, evidence, context="", chat_title=""):
    return pain_rejection_reason(title, evidence, context, chat_title) is None


def source_key(row):
    getter = row.get if hasattr(row, "get") else lambda key, default=None: row[key] if key in row.keys() else default
    return normalize_text(getter("source_username") or getter("source_title") or getter("chat_id") or "")


def distinct_source_count(source_rows):
    return len({source_key(row) for row in source_rows if source_key(row)})


def insight_rejection_reason(core_problem, business_impact="", source_rows=None):
    source_rows = source_rows or []
    clean_core = normalize_text(core_problem)
    combined = " ".join(
        f"{row.get('source_title', '')} {row.get('problem_title', '')} {row.get('evidence_quote', '')}"
        for row in source_rows
    )
    if len(clean_core) < 18 or len(clean_core.split()) < 3:
        return "cluster title too short"
    if clean_core in GENERIC_CORE_PROBLEMS:
        return "cluster is too broad to validate or sell against"
    if not has_any(f"{core_problem} {business_impact} {combined}", IT_TOPIC_SIGNALS + PROFESSIONAL_CONTEXT_SIGNALS):
        return "cluster has no professional IT context"
    return None


def insight_quality_grade(core_problem, business_impact="", source_rows=None):
    source_rows = source_rows or []
    reason = insight_rejection_reason(core_problem, business_impact, source_rows)
    if reason:
        return "Trash", reason
    authors = {
        row.get("author_anonymized_id") or
        (f"{row.get('source_username')}:{row.get('author_id')}" if row.get("author_id") else None)
        for row in source_rows
    }
    authors.discard(None)
    chats = distinct_source_count(source_rows)
    if len(authors) >= 2 and chats >= 2:
        return "Gold", None
    return "Silver", "needs independent confirmation across more chats/authors"
