"""人名库自检：判断给出的名字是不是人名、中国人名还是外国译名。

用法：
    python scripts\\check_names.py
    python scripts\\check_names.py 路明非 "恺撒·加图索" 掌门

不带参数时跑一组内置样例，并把结果写到 backend/data/_names_check.txt（UTF-8）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from services import name_library  # noqa: E402

SAMPLES = [
    # 中国人名
    "路明非", "陈墨瞳", "欧阳锋", "古德里安", "沈砚", "楚子航", "赵孟华", "路明非同学",
    # 外国人名
    "恺撒·加图索", "约翰·史密斯", "冯·施耐德", "诺玛·劳恩斯", "玛丽", "芬格尔",
    "亚纪", "曼施坦因", "龙德施泰特", "玛丽亚·卡拉斯",
    # 称呼
    "老周", "小明", "阿朱",
    # 非人名
    "掌门", "黑衣人", "学院", "了一声", "可以", "学院里", "王教授", "史密斯教授",
]


def main() -> int:
    try:  # cp936 控制台打不出 ✓/✗，尽力而为，别让脚本崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    names = sys.argv[1:] or SAMPLES
    rows = [name_library.lookup(name) for name in names]

    kind_label = {"cn": "中国人名", "foreign": "外国译名", "nickname": "称呼", "unknown": "非人名"}
    width = max(len(item["name"]) for item in rows) + 2

    lines = []
    for item in rows:
        lines.append(
            f"{item['name']:<{width}} {kind_label[item['kind']]:<8} "
            f"score={item['score']:<5} {'✓' if item['plausible'] else '✗'} "
            f"姓={item['surname'] or '-'} 名={item['given'] or '-'} "
            f"{'| ' + '、'.join(item['reasons']) if item['reasons'] else ''}"
        )

    stats = name_library.library_stats()
    header = [
        "人名库规模：" + "、".join(f"{k}={v}" for k, v in stats.items()),
        f"共 {len(rows)} 个名字，判定为人名 {sum(1 for item in rows if item['plausible'])} 个",
        "",
    ]
    report = "\n".join(header + lines)

    print("\n".join(header[:2]))
    for line in lines:
        print(line)

    detail = ROOT / "backend" / "data" / "_names_check.txt"
    detail.parent.mkdir(parents=True, exist_ok=True)
    detail.write_text(report, encoding="utf-8")
    print(f"\ndetail -> {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())