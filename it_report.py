"""Markdown report for First Money ranking."""

import argparse
import asyncio
import json
from pathlib import Path

import aiosqlite

from config import Config
from database import Database


REPORT_COLUMNS = """
id, core_problem, description, icp, unique_authors, unique_chats, total_messages,
date_from, date_to, example_messages, current_workaround, frequency, pain_score,
wtp_score, reachability_score, confidence, why_business_pain, why_not_business_pain,
manual_mvp, manual_48h, price_hypothesis, next_50_source, next_experiment, offer,
first_customer_experiment, acquisition_source, trend, recent_signals,
first_money_score, main_risk
"""


def _value(value, fallback="неизвестно"):
    return str(value).strip() if value not in (None, "") else fallback


def _examples(raw):
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        values = []
    lines = []
    for item in values[:5]:
        if isinstance(item, dict):
            quote = _value(item.get("quote"), "")
            source = _value(item.get("chat"), "источник")
            date = _value(item.get("date"), "дата неизвестна")
            if quote:
                lines.append(f"- «{quote}» — {source}, {date}")
    return "\n".join(lines) or "- Недостаточно сохранённых цитат."


def format_cluster(row, rank):
    d = dict(row)
    return f"""## #{rank} {_value(d['core_problem'])}

First Money Score: **{int(d['first_money_score'] or 0)}/100**  
Pain Score: **{int(d['pain_score'] or 0)}/100**  
WTP: **{_value(d['wtp_score'], 'LOW')}**  
Reachability / Lead Abundance: **{int(d['reachability_score'] or 0)}/100**  
Independent users: **{int(d['unique_authors'] or 0)}** · Chats: **{int(d['unique_chats'] or 0)}** · Messages: **{int(d['total_messages'] or 0)}** · Recent signals (30d): **{int(d['recent_signals'] or 0)}**  
Trend: **{_value(d['trend'], 'UNKNOWN')}** · Confidence: **{float(d['confidence'] or 0):.2f}**

**Боль:** {_value(d['description'])}

**Кто страдает (ICP):** {_value(d['icp'])}

**Что сейчас делает:** {_value(d['current_workaround'])}

**Частота:** {_value(d['frequency'])}

**Диапазон свидетельств:** {_value(d['date_from'])} — {_value(d['date_to'])}

**Почему это может быть бизнес-болью:** {_value(d['why_business_pain'])}

**Почему это может НЕ быть бизнес-болью:** {_value(d['why_not_business_pain'])}

**Evidence:**

{_examples(d['example_messages'])}

**Manual MVP:** {_value(d['manual_mvp'])}

**Можно отдать вручную за 1–2 дня:** {'да' if d['manual_48h'] else 'нет/не доказано'}

**Что можно продать за 48 часов:** {_value(d['offer'])}

**Цена-гипотеза:** {_value(d['price_hypothesis'])}

**Первый клиентский эксперимент:** {_value(d['first_customer_experiment'])}

**Где взять первые 10 клиентов:** {_value(d['acquisition_source'])}

**Где взять следующие 50:** {_value(d['next_50_source'])}

**Главный риск:** {_value(d['main_risk'])}

**Следующий тест:** {_value(d['next_experiment'])}
"""


async def build_report(limit=10, first_money=True):
    await Database.init_schema()
    where = "WHERE total_messages >= 2" if first_money else ""
    order = "first_money_score DESC, pain_score DESC, reachability_score DESC" if first_money else "pain_score DESC"
    async with aiosqlite.connect(Config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {REPORT_COLUMNS} FROM verified_insights {where} ORDER BY {order} LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    heading = "# IT Pain Radar — First Money Ranking" if first_money else "# IT Pain Radar — Pain Ranking"
    if not rows:
        return heading + "\n\nПока нет кластеров с минимум двумя подтверждающими сообщениями.\n"
    return heading + "\n\n" + "\n---\n\n".join(format_cluster(row, i) for i, row in enumerate(rows, 1))


async def main():
    parser = argparse.ArgumentParser(description="Generate IT Pain Radar Markdown report")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--all", action="store_true", help="rank by pain rather than First Money")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = await build_report(args.limit, first_money=not args.all)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(str(args.output.resolve()))
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())
