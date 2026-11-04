"""命令行诊断：看看算法在一本小说里挖到了谁、丢了谁。

用法（在项目根目录运行）：

    python scripts/analyze_novel.py 小说.txt
    python scripts/analyze_novel.py 小说.txt --max 30 --min-mentions 2
    python scripts/analyze_novel.py 小说.txt --json out.json   # 结果写文件，方便贴出来
    python scripts/analyze_novel.py 小说.txt --hybrid          # 额外用模型补全（1 次调用，花钱）

默认只跑纯算法（零成本）。--json 输出是 UTF-8，控制台中文乱码时看文件即可。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.config import get_settings  # noqa: E402
from readers.document import DocumentError, extract_text  # noqa: E402
from services import character_miner as miner  # noqa: E402
from services import extractor  # noqa: E402


def _safe(text: str, limit: int = 40) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def print_records(records: list[dict]) -> None:
    print(f"\n识别到 {len(records)} 个角色：\n")
    print(f"{'名字':<8}{'出场':<6}{'台词':<6}{'别名':<18}性格 / 说话风格")
    print("-" * 96)
    for record in records:
        aliases = "、".join(record.get("aliases") or []) or "-"
        traits = "、".join((record.get("personality") or [])[:3]) or "-"
        style = _safe(record.get("speech_style") or "", 34)
        print(
            f"{record['name']:<8}{record.get('appearances', 0):<6}"
            f"{len(record.get('quotes') or []):<6}{aliases:<18}{traits} / {style}"
        )


def print_dropped(diagnostic: dict) -> None:
    dropped = diagnostic.get("dropped") or []
    if not dropped:
        return
    print(f"\n被丢弃的候选（{len(dropped)} 个）：")
    for item in dropped[:25]:
        print(f"  - {item.get('name')}：{item.get('reason')}")

    validate_rejected = diagnostic.get("validate_rejected") or []
    if validate_rejected:
        print(f"\n最终校验丢弃（{len(validate_rejected)} 个）：")
        for item in validate_rejected[:25]:
            print(f"  - {item.get('name')}：{item.get('reason')}")


async def run_hybrid(records: list[dict], text: str) -> tuple[list[dict], dict]:
    settings = get_settings()
    if not settings.deepseek_api_key.strip():
        print("\n[跳过 hybrid] 未配置 DEEPSEEK_API_KEY")
        return records, {}

    excerpts = miner.pick_reference_excerpts(
        extractor.chunk_text(text, chunk_chars=settings.chunk_chars, overlap=settings.chunk_overlap),
        limit=4,
        chars=max(200, settings.hybrid_reference_chars // 4),
    )
    enriched, report = await extractor.enrich_characters(
        records,
        api_key=settings.deepseek_api_key.strip(),
        model=settings.extraction_model_name,
        book_title="（命令行分析）",
        text=text,
        reference_excerpts=excerpts,
        quote_samples=settings.hybrid_quote_samples,
        narrative_samples=settings.hybrid_narrative_samples,
    )
    enriched, rejected = miner.validate_characters(enriched, text, min_mentions=1)
    if rejected:
        report["validate_rejected"] = [
            {"name": name, "reason": reason} for name, reason in rejected
        ]
    return enriched, report


def main() -> int:
    parser = argparse.ArgumentParser(description="小说角色抽取诊断")
    parser.add_argument("file", help="小说文件（txt / md / pdf）")
    parser.add_argument("--max", type=int, default=20, help="最多输出几个角色")
    parser.add_argument("--min-mentions", type=int, default=3, help="名字至少出现几次")
    parser.add_argument("--hybrid", action="store_true", help="额外调用一次模型补全（花钱）")
    parser.add_argument("--json", dest="json_out", help="把结果写到这个 JSON 文件")
    args = parser.parse_args()

    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1

    try:
        text, kind = extract_text(path.name, path.read_bytes())
    except DocumentError as exc:
        print(f"解析失败：{exc}")
        return 1

    print(f"文件：{path.name}（{kind}）｜正文 {len(text)} 字")
    records, diagnostic = miner.mine_characters(
        text, max_characters=args.max, min_mentions=args.min_mentions
    )
    print(
        f"候选 {diagnostic.get('candidates', 0)} 个｜"
        f"台词 {diagnostic.get('quotes_total', 0)} 句，归属 {diagnostic.get('quotes_attributed', 0)} 句｜"
        f"置信度 {diagnostic.get('attribution')}"
    )
    print_records(records)
    print_dropped(diagnostic)

    result = {"file": path.name, "chars": len(text), "algo": records, "diagnostic": diagnostic}

    if args.hybrid:
        enriched, report = asyncio.run(run_hybrid(records, text))
        print("\n=== hybrid 补全结果 ===")
        print_records(enriched)
        print(f"\n补全报告：{json.dumps(report, ensure_ascii=False)}")
        result["hybrid"] = enriched
        result["enrich_report"] = report

    if args.json_out:
        target = pathlib.Path(args.json_out)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入：{target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
