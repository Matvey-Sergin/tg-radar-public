"""One-shot end-to-end runner for a measurable IT Radar pass."""

import argparse
import asyncio
import json
from pathlib import Path

from butcher import run_butcher
from database import Database
from it_report import build_report
from miner import run_miner
from scout import run_scout


async def counts():
    names = ["target_chats", "chat_messages", "pains", "verified_insights"]
    result = {}
    for name in names:
        row = await Database.fetchone(f"SELECT COUNT(*) FROM {name}")
        result[name] = int(row[0] or 0) if row else 0
    return result


async def run(args):
    await Database.init_schema()
    before = await counts()
    discovery = {"skipped": True} if args.skip_discovery else await run_scout(True, args.max_keywords)
    collection = {"skipped": True} if args.skip_collection else await run_miner(True, args.max_chats)
    clustering = await run_butcher(True)
    report = await build_report(args.limit, first_money=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    summary = {
        "pipeline": ["IT_CHAT_DISCOVERY", "MESSAGE_COLLECTION", "PAIN_CANDIDATE_DETECTION",
                     "FILTERING", "STRUCTURED_PAIN_SIGNALS", "CLUSTERING", "SCORING",
                     "LEAD_ABUNDANCE", "FIRST_MONEY_RANKING", "REPORT"],
        "before": before,
        "discovery": discovery,
        "collection": collection,
        "clustering": clustering,
        "after": await counts(),
        "report": str(args.output.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Run one bounded IT Pain Radar pipeline pass")
    parser.add_argument("--max-keywords", type=int, default=2)
    parser.add_argument("--max-chats", type=int, default=2)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-collection", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports") / "it_first_money.md")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
