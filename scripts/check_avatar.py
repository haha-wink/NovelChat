"""诊断头像爬虫：不开服务器，直接跑一次抓取，把每个数据源的结果打出来。

用法：
    python scripts\\check_avatar.py 路明非
    python scripts\\check_avatar.py 路明非 "龙族"

输出：控制台打印 ASCII 骨架，完整细节（含每个源的错误原因）写到
backend/data/_avatar_check.txt（UTF-8，用记事本/VSCode 打开不会乱码）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from services.avatar_crawler import AvatarCrawler  # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/check_avatar.py <character_name> [work_title]")
        return 2

    name = sys.argv[1]
    work_title = sys.argv[2] if len(sys.argv) > 2 else ""

    crawler = AvatarCrawler(max_candidates=6)
    candidates = await crawler.find(character_name=name, work_title=work_title)

    print(f"character: {name}")
    print(f"work     : {work_title or '(none)'}")
    print(f"requests : {len(crawler.attempts)}")
    for record in crawler.attempts:
        status = "OK " if record["found"] else "FAIL"
        print(
            f"  [{status}] {record['provider']:<18} found={record['found']} "
            f"elapsed={record.get('elapsed')}s"
        )
    print(f"candidates: {len(candidates)}")
    for item in candidates:
        print(f"  - {item['source']} {item['url']}")

    detail = ROOT / "backend" / "data" / "_avatar_check.txt"
    detail.parent.mkdir(parents=True, exist_ok=True)
    detail.write_text(
        json.dumps(
            {
                "character": name,
                "work_title": work_title,
                "attempts": crawler.attempts,
                "explain": crawler.explain(),
                "candidates": candidates,
                "python": sys.version,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"detail -> {detail}")
    return 0 if candidates else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))