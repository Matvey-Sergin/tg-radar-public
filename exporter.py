"""Compatibility entry point for exporting the current IT First Money report."""

import asyncio

from it_report import build_report


async def export_all_insights():
    print(await build_report(limit=100, first_money=True))


if __name__ == "__main__":
    asyncio.run(export_all_insights())
