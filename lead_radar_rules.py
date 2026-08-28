import re

from quality_rules import normalize_text


ACTOR_SELLER = "seller"
ACTOR_SERVICE_PROVIDER = "service_provider"
ACTOR_UNKNOWN = "unknown"

RADAR_MARGIN_LEAK = "margin_leak"
RADAR_FROZEN_STOCK = "frozen_stock"
RADAR_FBS_CHAOS = "fbs_chaos"

TEMPERATURE_RED = "red"
TEMPERATURE_YELLOW = "yellow"
TEMPERATURE_GREEN = "green"

RADAR_LABELS = {
    RADAR_MARGIN_LEAK: "MarginLeakRadar",
    RADAR_FROZEN_STOCK: "FrozenStockRadar",
    RADAR_FBS_CHAOS: "FBSChaosRadar",
}

TEMPERATURE_LABELS = {
    TEMPERATURE_RED: "🔴 красный",
    TEMPERATURE_YELLOW: "🟡 жёлтый",
    TEMPERATURE_GREEN: "🟢 зелёный",
}

REJECTION_PRIORITY = [
    "duplicate",
    "old_message",
    "ad_detected",
    "unsupported_topic",
    "not_seller_case",
    "no_money_risk",
    "no_solution_search",
    "too_generic",
    "low_second_score",
]

SERVICE_OFFER_PATTERNS = [
    r"\b(?:оказываю|оказываем|предлагаю|предлагаем)\s+.{0,40}\bуслуг",
    r"\b(?:я\s+бухгалтер|мы\s+(?:бухгалтерская|маркетинговая|дизайн)\s+(?:компания|команда|студия|агентство))\b",
    r"\b(?:беру|возьму)\s+(?:селлеров?|клиентов?|бизнес)\s+на\s+(?:ведение|сопровождение)",
    r"\b(?:поможем|помогу)\s+(?:вам|вашему|селлерам?|бизнесу)\b",
    r"\b(?:скину|покажу|смотрите)\s+(?:сво[её]\s+)?портфолио\b",
    r"\b(?:опыт|на\s+рынке)\s+(?:более\s+|свыше\s+|от\s+)?\d{1,2}\s+лет\b",
    r"\b(?:стоимость|цена|тариф)\s+(?:от\s+)?\d[\d\s]*(?:₽|руб)",
    r"\b(?:записывайтесь|запишитесь)\s+на\s+(?:бесплатную\s+)?(?:консультацию|аудит|разбор)",
    r"\b(?:инфографик|дизайн\s+карточ|rich[- ]?контент)\b.{0,180}\b(?:сделаю|заказать|стоимость|цена|портфолио|пишите|лс|личк)",
    r"\b(?:сделаю|заказать|стоимость|цена|портфолио)\b.{0,180}\b(?:инфографик|дизайн\s+карточ|rich[- ]?контент)",
    r"\b(?:бухгалтерск|налогов|уч[её]т)\b.{0,180}\b(?:сопровождение|консультация|услуги|записывайтесь|пишите\s+в\s+лс)",
]

CALL_TO_ACTION_PATTERNS = [
    r"\b(?:пишите|напишите|обращайтесь|жду)\s+(?:мне\s+)?(?:в\s+)?(?:лс|личк|директ)\b",
    r"\bсвязаться\s+(?:со\s+мной|с\s+нами)\b",
    r"\bместа\s+ограничены\b",
]

PRICE_PATTERN = re.compile(r"(?:\d[\d\s]{2,}\s*(?:₽|руб)|(?:от|всего)\s+\d[\d\s]*)", re.IGNORECASE)
EMOJI_PATTERN = re.compile(r"[🔥✅🚀💰🎯📈💼⚡️⭐🔹🔸]{2,}")

FROZEN_STOCK_SIGNALS = [
    "заморож",
    "товар завис",
    "завис на склад",
    "не в продаже",
    "нет в продаже",
    "не появился в продаже",
    "не отображается в продаже",
    "продажи встали",
    "товар не продается",
    "товар не продаётся",
    "приняли товар",
    "товар приняли",
    "хранение списывают",
    "числится на складе",
    "поддержка молчит",
    "поддержка шаблон",
]

FBS_CHAOS_SIGNALS = [
    "fbs",
    "фбс",
    "поставка",
    "отгруз",
    "недовоз",
    "пвз",
    "не добавляется",
    "не добавляются заказы",
    "статус поставки",
    "сроки горят",
    "не могу создать поставку",
    "не принимают поставку",
]

MARGIN_LEAK_SIGNALS = [
    "марж",
    "комисси",
    "в минус",
    "прибыли нет",
    "съел прибыль",
    "съедает прибыль",
    "реклама не окуп",
    "логистика съел",
    "не понимаю прибыль",
    "хочу закрыть лк",
    "хочу всё продать",
    "хочу все продать",
]

RED_SIGNALS = [
    "продажи встали",
    "в минус",
    "деньги списывают",
    "списывают хранение",
    "поддержка молчит",
    "не понимаю, что делать",
    "сроки горят",
    "штраф",
    "заморож",
    "товар завис",
    "продаж нет",
    "не выплат",
]

GREEN_SIGNALS = [
    "масштабировать",
    "сколько дозаказать",
    "товар хорошо идет",
    "товар хорошо идёт",
    "как понять, что карточка прибыльная",
]


def service_offer_rejection_reason(text):
    clean = normalize_text(text)
    if any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in SERVICE_OFFER_PATTERNS):
        return "service provider offer"
    has_cta = any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in CALL_TO_ACTION_PATTERNS)
    if has_cta and (PRICE_PATTERN.search(clean) or "услуг" in clean or "портфолио" in clean):
        return "service provider call-to-action"
    if has_cta and EMOJI_PATTERN.search(text or "") and PRICE_PATTERN.search(clean):
        return "promotional offer"
    return None


def local_rejection_code(reason):
    clean = normalize_text(reason)
    if not clean:
        return None
    if any(signal in clean for signal in ("promo", "advert", "service provider", "реклам", "услуг", "vacancy")):
        return "ad_detected"
    if any(signal in clean for signal in ("duplicate", "дублик")):
        return "duplicate"
    if any(signal in clean for signal in ("old message", "стар")):
        return "old_message"
    if any(signal in clean for signal in ("too short", "expert/advice", "слишком корот")):
        return "too_generic"
    return "unsupported_topic"


def verifier_rejection_code(result):
    if bool(result.get("is_ad")):
        return "ad_detected"
    if (
        normalize_actor_type(result.get("actor_type"), result.get("is_seller")) != ACTOR_SELLER
        or not bool(result.get("is_seller"))
        or not bool(result.get("personal_case"))
    ):
        return "not_seller_case"
    if not bool(result.get("money_or_ops_loss")):
        return "no_money_risk"
    if not bool(result.get("seeks_solution")):
        return "no_solution_search"
    if not bool(result.get("current_problem")) or not bool(result.get("has_specificity")):
        return "too_generic"
    if any(bool(result.get(key)) for key in ("is_review", "is_news", "is_expert_reply")):
        return "unsupported_topic"
    return "low_second_score"


def detect_radar_type(text, fallback_types=None):
    clean = normalize_text(text)
    if any(signal in clean for signal in FROZEN_STOCK_SIGNALS):
        return RADAR_FROZEN_STOCK
    if any(signal in clean for signal in FBS_CHAOS_SIGNALS):
        return RADAR_FBS_CHAOS
    if any(signal in clean for signal in MARGIN_LEAK_SIGNALS):
        return RADAR_MARGIN_LEAK

    fallback = {normalize_text(value) for value in (fallback_types or [])}
    if {"fbs", "логистика"} & fallback:
        return RADAR_FBS_CHAOS
    return RADAR_MARGIN_LEAK


def normalize_actor_type(value, is_seller=False):
    clean = normalize_text(value).replace(" ", "_")
    aliases = {
        "seller": ACTOR_SELLER,
        "селлер": ACTOR_SELLER,
        "продавец": ACTOR_SELLER,
        "service_provider": ACTOR_SERVICE_PROVIDER,
        "provider": ACTOR_SERVICE_PROVIDER,
        "исполнитель": ACTOR_SERVICE_PROVIDER,
        "агентство": ACTOR_SERVICE_PROVIDER,
    }
    if clean in aliases:
        return aliases[clean]
    return ACTOR_SELLER if is_seller else ACTOR_UNKNOWN


def normalize_radar_type(value, text="", fallback_types=None):
    clean = normalize_text(value).replace(" ", "_")
    aliases = {
        "margin": RADAR_MARGIN_LEAK,
        "margin_leak": RADAR_MARGIN_LEAK,
        "marginleakradar": RADAR_MARGIN_LEAK,
        "маржа": RADAR_MARGIN_LEAK,
        "frozen_stock": RADAR_FROZEN_STOCK,
        "frozenstockradar": RADAR_FROZEN_STOCK,
        "замороженный_товар": RADAR_FROZEN_STOCK,
        "fbs_chaos": RADAR_FBS_CHAOS,
        "fbschaosradar": RADAR_FBS_CHAOS,
        "fbs": RADAR_FBS_CHAOS,
    }
    return aliases.get(clean) or detect_radar_type(text, fallback_types)


def detect_temperature(text, score=0):
    clean = normalize_text(text)
    if any(signal in clean for signal in RED_SIGNALS):
        return TEMPERATURE_RED
    if any(signal in clean for signal in GREEN_SIGNALS):
        return TEMPERATURE_GREEN
    return TEMPERATURE_RED if int(score or 0) >= 7 else TEMPERATURE_YELLOW


def normalize_temperature(value, text="", score=0):
    clean = normalize_text(value)
    aliases = {
        "red": TEMPERATURE_RED,
        "красный": TEMPERATURE_RED,
        "yellow": TEMPERATURE_YELLOW,
        "жёлтый": TEMPERATURE_YELLOW,
        "желтый": TEMPERATURE_YELLOW,
        "green": TEMPERATURE_GREEN,
        "зелёный": TEMPERATURE_GREEN,
        "зеленый": TEMPERATURE_GREEN,
    }
    return aliases.get(clean) or detect_temperature(text, score)


def second_layer_score(result):
    forbidden = ["is_ad", "is_review", "is_news", "is_expert_reply"]
    if any(bool(result.get(key)) for key in forbidden):
        return 0
    if normalize_actor_type(result.get("actor_type"), result.get("is_seller")) == ACTOR_SERVICE_PROVIDER:
        return 0

    score = 0
    score += 2 if bool(result.get("is_seller")) else 0
    score += 2 if bool(result.get("personal_case")) else 0
    score += 2 if bool(result.get("current_problem")) else 0
    score += 2 if bool(result.get("money_or_ops_loss")) else 0
    score += 1 if bool(result.get("seeks_solution")) else 0
    score += 1 if bool(result.get("has_specificity")) else 0
    score += 1 if bool(result.get("helplessness")) else 0
    score += 1 if bool(result.get("urgent")) else 0
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.80:
        score = min(score, 8)
    return min(10, score)


def verifier_passed(result, min_confidence=0.80):
    actor_type = normalize_actor_type(result.get("actor_type"), result.get("is_seller"))
    required = [
        "is_seller",
        "personal_case",
        "current_problem",
        "money_or_ops_loss",
        "seeks_solution",
    ]
    forbidden = ["is_ad", "is_review", "is_news", "is_expert_reply"]
    return (
        actor_type == ACTOR_SELLER
        and all(bool(result.get(key)) for key in required)
        and not any(bool(result.get(key)) for key in forbidden)
        and second_layer_score(result) >= 9
    )


def verifier_labels(result, text="", fallback_types=None, score=0):
    return {
        "actor_type": normalize_actor_type(result.get("actor_type"), result.get("is_seller")),
        "radar_type": normalize_radar_type(result.get("radar_type"), text, fallback_types),
        "temperature": normalize_temperature(result.get("temperature"), text, score),
        "filter_score": second_layer_score(result),
    }
