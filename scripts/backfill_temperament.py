"""回填角色性格五维（temperament）。

## 为什么需要回填

选人逻辑按「用户情绪 x 角色性格」算适配度（`services/dispatch.py`），
但老角色建库时还没有 `temperament` 字段，只能靠关键词临时推断——
而抽取出来的性格多是风格描述（"话多，动辄长篇"），温柔的信号很稀薄。

这个脚本把五维**算一次存进库里**，之后选人直接读，不再每次推断。

## 用法

    python scripts/backfill_temperament.py            # 只补空白的
    python scripts/backfill_temperament.py --force    # 全部重算
    python scripts/backfill_temperament.py --dry-run  # 只看会改哪些

幂等：已经有非空 temperament 的角色默认跳过。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "app"))

from core import database as db  # noqa: E402
from services import persona  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="回填角色性格五维")
    parser.add_argument("--force", action="store_true", help="已有值也重算")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = parser.parse_args()

    db.init_db()
    total = updated = skipped = 0

    for library in db.list_libraries():
        for shelf in db.list_shelves(library["id"]):
            for book in db.list_books(shelf_id=shelf["id"]):
                for character in db.list_characters(book["id"]):
                    total += 1
                    current = character.get("temperament") or {}
                    if current and not args.force:
                        skipped += 1
                        continue

                    traits = persona.snapshot(character)
                    name = character.get("name") or character.get("id")
                    if args.dry_run:
                        print(f"  [dry] {name:<10} {traits}")
                        updated += 1
                        continue

                    db.update_character(character["id"], temperament=traits)
                    updated += 1
                    print(f"  {name:<10} {traits}")

    print(f"\n角色 {total} 个，更新 {updated} 个，跳过 {skipped} 个"
          f"{'（dry-run，未写库）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())