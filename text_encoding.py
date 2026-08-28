"""Small defensive repair for occasional provider mojibake in Cyrillic JSON."""

import re


def _score(value):
    if not value:
        return -1000
    cyr = len(re.findall(r"[А-Яа-яЁё]", value))
    latin_extended = len(re.findall(r"[À-ÿ]", value))
    known_bad = sum(value.count(marker) for marker in ("Ð", "Ñ", "Рµ", "Р°", "С‚", "РЅ"))
    words = len(re.findall(r"\b(?:не|на|для|это|что|как|из|за|при|или|и|в|с)\b", value.lower()))
    return cyr * 2 + words * 5 - latin_extended * 3 - known_bad * 2 - len(value) * .001


def repair_text(value):
    if not isinstance(value, str) or not value:
        return value
    candidates = [value]
    transforms = [
        ("latin1", "utf-8"), ("latin1", "cp1251"), ("cp1251", "utf-8"),
    ]
    frontier = [value]
    for _ in range(2):
        new_frontier = []
        for item in frontier:
            for source, target in transforms:
                try:
                    candidate = item.encode(source).decode(target)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if candidate not in candidates:
                    candidates.append(candidate)
                    new_frontier.append(candidate)
        frontier = new_frontier
    return max(candidates, key=_score)


def repair_json_strings(value):
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, list):
        return [repair_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_json_strings(item) for key, item in value.items()}
    return value
