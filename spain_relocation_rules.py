"""Deterministic rules for public Spain relocation Telegram research."""

from __future__ import annotations

import hashlib
import re


DISCOVERY_QUERIES = [
    "Испания релокация чат",
    "эмиграция в Испанию",
    "переезд в Испанию",
    "ВНЖ Испании чат",
    "виза Испания чат",
    "Digital Nomad Испания",
    "цифровой кочевник Испания",
    "фриланс Испания ВНЖ",
    "autonomo Испания",
    "консульство Испании СПб",
    "визовый центр Испании СПб",
    "BLS Испания Санкт-Петербург",
    "студенческая виза Испания",
    "non lucrative Испания",
    "стартап виза Испания",
]

KNOWN_PUBLIC_CHATS = [
    "spainchats",
    "digitalnomadspainchat",
    "chatfornomads",
    "spain_migrun",
    "russians_alicante_chat",
    "migranty_ispania",
    "nomadspainchat",
    "Viza_v_Ispaniu",
]

GLOBAL_MESSAGE_QUERIES = [
    "подавалась Испания СПб",
    "подавался Испания СПб",
    "подавались Испания СПб",
    "подача Испания Санкт-Петербург",
    "подача Испания Питер",
    "подали документы Испания СПб",
    "визовый центр Испании СПб",
    "консульство Испании Санкт-Петербург",
    "BLS Испания СПб",
]

SPAIN_MARKERS = (
    "испания", "испании", "испанск", "spain", "españa", "барселон", "мадрид",
    "аликант", "валенси", "малаг", "тенериф", "канар",
)
RELOCATION_MARKERS = (
    "релокац", "эмиграц", "иммиграц", "переезд", "переех", "внж", "резиденц",
    "виз", "digital nomad", "digital-nomad", "цифровой кочевник", "номад",
    "фриланс", "freelance", "autonomo", "autónomo", "консульств", "bls",
    "стартап", "startup", "non lucrative", "no lucrativa", "студенческ",
)
SPB_MARKERS = (
    "санкт-петербург", "санкт петербург", "с.-петербург", "петербург", "в спб",
    "из спб", "спб ", " спб", "в питере", "из питера", "питерский", "питер",
    "bls spb", "bls спб",
)
APPLICATION_PATTERNS = [
    r"\bподава(?:л|ла|ли|лся|лась|лись)\b",
    r"\bподал(?:а|и)?\b",
    r"\bподали\s+(?:документ|заявлен|анкет)",
    r"\bсдава(?:л|ла|ли)\s+документ",
    r"\bсдал(?:а|и)?\s+документ",
    r"\bзаписал(?:ся|ась|ись)\s+(?:в|на)\s+(?:bls|визов|консульств|подач)",
    r"\bбыл(?:а|и)?\s+(?:на\s+)?подач",
    r"\bполучил(?:а|и)?\s+(?:виз|одобр|резиденц|внж)",
    r"\bдокументы\s+(?:приняли|сдали|поданы)\b",
]
FIRST_PERSON_PATTERNS = [
    r"\bя\b", r"\bмы\b", r"\bу меня\b", r"\bу нас\b", r"\bмуж\b", r"\bжена\b",
    r"\bмой\b", r"\bмоя\b", r"\bнаша\b", r"\bнаш\s+(?:кейс|пакет|документ)",
]
SEEKER_PATTERNS = [
    r"\bкто\s+(?:нибудь\s+)?подава",
    r"\bесть\s+(?:ли\s+)?кто",
    r"\bподскажите\b.{0,100}\bпода",
    r"\bкто\s+знает\b",
    r"\bотзовитесь\b",
]
SPB_VENUE = r"(?:санкт[- ]петербург(?:е|а)?|петербург(?:е|а)?|спб|питер(?:е|а)?)"
APPLICATION_ACTION = r"(?:подава(?:л|ла|ли|лся|лась|лись)|подал(?:а|и)?|подач[аиу]|сдава(?:л|ла|ли)|сдал(?:а|и)?)"
DIRECT_SPB_APPLICATION_PATTERNS = [
    rf"\b{APPLICATION_ACTION}\b.{{0,100}}\b{SPB_VENUE}\b",
    rf"\b{SPB_VENUE}\b.{{0,100}}\b{APPLICATION_ACTION}\b",
]
PAST_APPLICATION_ACTION = r"(?:подава(?:л|ла|ли|лся|лась|лись)|подал(?:а|и)?|сдава(?:л|ла|ли)|сдал(?:а|и)?|получа(?:л|ла|ли)|получил(?:а|и)?)"
PERSONAL_SPB_APPLICATION_PATTERNS = [
    rf"\b(?:я|мы|муж|жена|супруг|супруга)\b.{{0,140}}\b{PAST_APPLICATION_ACTION}\b.{{0,100}}\b{SPB_VENUE}\b",
    rf"\b(?:я|мы|муж|жена|супруг|супруга)\b.{{0,140}}\b{SPB_VENUE}\b.{{0,100}}\b{PAST_APPLICATION_ACTION}\b",
    rf"(?:^|[.!?]\s+)\s*(?:сегодня\s+)?\b{PAST_APPLICATION_ACTION}\b.{{0,80}}\b{SPB_VENUE}\b",
    rf"(?:^|[.!?]\s+)\s*(?:сегодня\s+)?\b{SPB_VENUE}\b.{{0,80}}\b{PAST_APPLICATION_ACTION}\b",
    rf"#статистика.{{0,120}}\b(?:подач[аиу]|{PAST_APPLICATION_ACTION})\b.{{0,80}}\b{SPB_VENUE}\b",
    rf"#статистика.{{0,120}}\b{SPB_VENUE}\b.{{0,80}}\b(?:подач[аиу]|{PAST_APPLICATION_ACTION})\b",
]
THIRD_PARTY_ACTION_PATTERNS = [
    rf"\b(?:клиент|клиентка|клиенты|заявитель|заявительница|знакомый|знакомая|при мне)\b.{{0,80}}\b{PAST_APPLICATION_ACTION}\b",
]
CLOSE_RELATIVE_PATTERNS = [
    rf"\b(?:муж|жена|супруг|супруга|сын|дочь)\b.{{0,100}}\b{APPLICATION_ACTION}\b.{{0,100}}\b{SPB_VENUE}\b",
    rf"\b(?:муж|жена|супруг|супруга|сын|дочь)\b.{{0,100}}\b{SPB_VENUE}\b.{{0,100}}\b{APPLICATION_ACTION}\b",
]
PROFESSIONAL_CASE_PATTERNS = [
    rf"\b(?:клиент|клиенту|клиента|клиенты|кейс|кейсы|у нас в работе)\b.{{0,220}}\b(?:одобрен|одобрение|получил|получили|подал|подали|подач)\b.{{0,220}}\b{SPB_VENUE}\b",
    rf"\b(?:одобрение|одобрили|получили)\b.{{0,160}}\b(?:виз|внж|резиденц)\w*\b.{{0,160}}\b{SPB_VENUE}\b",
    rf"\b{SPB_VENUE}\b.{{0,160}}\b(?:одобрение|одобрили|получили)\b.{{0,160}}\b(?:виз|внж|резиденц)\w*\b",
]


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def has_any(value: object, markers: tuple[str, ...]) -> bool:
    text = normalize_text(value)
    return any(marker in text for marker in markers)


def looks_like_spain_relocation_source(title: str, description: str = "") -> bool:
    text = f"{title} {description}"
    return has_any(text, SPAIN_MARKERS) and has_any(text, RELOCATION_MARKERS)


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_spb_spain_case(text: str, source_is_spain_specific: bool = False) -> dict:
    clean = normalize_text(text)
    has_spb = has_any(clean, SPB_MARKERS)
    has_spain = has_any(clean, SPAIN_MARKERS) or source_is_spain_specific
    has_application = _has_pattern(clean, APPLICATION_PATTERNS)
    first_person = _has_pattern(clean, FIRST_PERSON_PATTERNS)
    seeker_only = _has_pattern(clean, SEEKER_PATTERNS) and not (
        first_person and re.search(r"\b(?:я|мы)\s+(?:уже\s+)?подава|\b(?:я|мы)\s+подал", clean)
    )
    direct_venue_match = _has_pattern(clean, DIRECT_SPB_APPLICATION_PATTERNS)
    personal_venue_match = _has_pattern(clean, PERSONAL_SPB_APPLICATION_PATTERNS)
    third_party_action = _has_pattern(clean, THIRD_PARTY_ACTION_PATTERNS)
    close_relative = _has_pattern(clean, CLOSE_RELATIVE_PATTERNS)
    professional_case = _has_pattern(clean, PROFESSIONAL_CASE_PATTERNS)
    direct_applicant = bool(personal_venue_match and not third_party_action and not seeker_only)
    if professional_case and has_any(clean, ("клиент", "кейс", "у нас в работе")) and not seeker_only:
        evidence_kind = "professional_case_report"
    elif direct_applicant:
        evidence_kind = "direct_applicant"
    elif close_relative and not seeker_only:
        evidence_kind = "close_relative"
    elif professional_case and not seeker_only:
        evidence_kind = "professional_case_report"
    else:
        evidence_kind = "candidate"
    confirmed = evidence_kind != "candidate"
    candidate = bool(has_spb and has_spain and (has_application or "подач" in clean or "bls" in clean))
    return {
        "confirmed": confirmed,
        "candidate": candidate,
        "has_spb": has_spb,
        "has_spain": has_spain,
        "has_application": has_application,
        "first_person": first_person,
        "seeker_only": seeker_only,
        "direct_venue_match": direct_venue_match,
        "evidence_kind": evidence_kind,
        "visa_type": detect_visa_type(clean),
    }


def detect_visa_type(text: str) -> str:
    clean = normalize_text(text)
    variants = [
        ("Digital Nomad", ("digital nomad", "digital-nomad", "цифровой кочевник", "номад")),
        ("Student", ("студенческ", "учебн", "student")),
        ("Non-lucrative", ("non lucrative", "no lucrativa", "без права на работу")),
        ("Startup", ("startup", "стартап")),
        ("Schengen", ("шенген", "schengen", "туристическ")),
        ("Family", ("воссоединен", "семейн", "pareja", "парех")),
    ]
    for label, markers in variants:
        if any(marker in clean for marker in markers):
            return label
    return "Unspecified"


def stable_case_key(chat_id: object, message_id: object) -> str:
    raw = f"{chat_id}:{message_id}".encode("utf-8")
    return "case_" + hashlib.sha256(raw).hexdigest()[:12]


def short_quote(value: str, max_words: int = 24) -> str:
    words = re.sub(r"\s+", " ", str(value or "")).strip().split(" ")
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(".,;:") + "…"


def relevant_excerpt(value: str, max_words: int = 24) -> str:
    """Return a short excerpt centered on the Saint Petersburg evidence."""
    words = re.sub(r"\s+", " ", str(value or "")).strip().split(" ")
    marker_index = 0
    for index, word in enumerate(words):
        clean = normalize_text(word).strip(".,;:!?()[]{}#")
        if clean in {"спб", "питер", "питере", "питера"} or "петербург" in clean:
            marker_index = index
            break
    start = max(0, marker_index - 9)
    end = min(len(words), start + max_words)
    if end - start < max_words:
        start = max(0, end - max_words)
    excerpt = " ".join(words[start:end])
    if start:
        excerpt = "…" + excerpt
    if end < len(words):
        excerpt = excerpt.rstrip(".,;:") + "…"
    return excerpt
