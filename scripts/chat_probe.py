"""对话测试器：一句话进去，一路的诊断信息出来。

## 为什么单独做这个

真在界面上测，你只能看到"角色回了什么"——回得不好，你不知道是
**识别错了**（把倾诉当成求助）、**策略错了**（该共情却在给方案），
还是**模型不听话**（策略对但没照做）。

这个工具把中间判断全打出来：

```
用户输入
  → 清洗后的文本
  → 情绪 / 强度 / 意图 / 是否危机 / 歧义 / 判官有没有出手
  → SOP（模型实际看到的策略）
  → 角色此刻的心情与态度
  → 生成（流式）
  → 校验改了什么
  → 落库的 intent/emotion
```

默认**只走本地判断，不连模型**（`--dry` 是默认值）：
先看判断对不对，再决定要不要花钱生成。

## 两种用法

    单条：python scripts/chat_probe.py --shelf 龙族大家庭 --text "今天好累啊"
    单聊：python scripts/chat_probe.py --shelf 龙族大家庭 --character 路明非 --text "..."
    批量：python scripts/chat_probe.py --batch scripts/probe_samples.txt
    真跑：python scripts/chat_probe.py --text "..." --live

退出码：0 正常；1 有失败项（批量模式下）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "app"))

from core import database as db  # noqa: E402
from core.config import get_settings  # noqa: E402
from services import affect, dialogue  # noqa: E402

C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RED = "\033[31m"
C_YELLOW = "\033[33m"
C_GREEN = "\033[32m"
C_CYAN = "\033[36m"


def _hr(title: str = "") -> None:
    line = "─" * (58 - len(title)) if title else "─" * 58
    print(f"{C_DIM}{line}{C_RESET}")


def _section(title: str) -> None:
    print(f"\n{C_BOLD}{C_CYAN}▌ {title}{C_RESET}")


# ---------------------------------------------------------------------------
# 挑角色 / 书架
# ---------------------------------------------------------------------------

def find_shelf(name_or_id: str):
    if not name_or_id:
        shelves = db.list_shelves()
        return shelves[0] if shelves else None
    for shelf in db.list_shelves():
        if shelf["id"] == name_or_id or shelf["title"] == name_or_id:
            return shelf
        if name_or_id in (shelf.get("title") or ""):
            return shelf
    return None


def find_character(shelf, name_or_id: str):
    members = db.ensure_shelf_members(shelf["id"])
    if not name_or_id:
        return None
    for member in members:
        if member.get("character_id") == name_or_id or member.get("name") == name_or_id:
            return member
        if name_or_id in (member.get("name") or ""):
            return member
    return None


# ---------------------------------------------------------------------------
# 核心：只分析，不生成
# ---------------------------------------------------------------------------

async def analyze(text: str, *, character: dict | None = None) -> dialogue.TurnSignals:
    settings = get_settings()
    return await dialogue.classify_with_judge(
        text,
        mode=settings.dialogue_judge_mode,
        api_key=settings.api_key_for(None),
        model=(settings.dialogue_judge_model or settings.deepseek_model),
        timeout=float(settings.dialogue_judge_timeout),
    )


def show_analysis(raw: str, signals: dialogue.TurnSignals, character: dict | None = None) -> dict:
    cleaned = dialogue.preprocess(raw)
    _section("① 预处理")
    if cleaned != raw:
        print(f"  清洗前：{raw!r}")
        print(f"  清洗后：{cleaned!r}")
    else:
        print(f"  {cleaned!r}{C_DIM}（无需清洗）{C_RESET}")

    _section("② 情绪 & 意图")
    intensity = signals.intensity
    bar = "█" * int(round(intensity * 20)) + "░" * (20 - int(round(intensity * 20)))
    color = C_RED if signals.crisis else (C_YELLOW if intensity >= 0.6 else C_GREEN)
    print(f"  情绪：{signals.emotion}   强度：{color}{bar}{C_RESET} {intensity:.2f}")
    label = signals.intent_label
    if signals.crisis:
        label = f"{C_RED}{label}（安全流程）{C_RESET}"
    print(f"  意图：{label}")
    print(f"  歧义：{'是' if signals.ambiguous else '否'}"
          f"{C_DIM}（auto 模式下歧义才会问判官）{C_RESET}")
    if signals.evidence:
        print(f"  依据：{C_DIM}{'、'.join(signals.evidence)}{C_RESET}")

    _section("③ 规划层 SOP（模型实际看到的）")
    turn_plan = dialogue.plan(signals, character=character)
    for line in turn_plan.context_block().splitlines():
        print(f"  {line}")

    if character:
        _section("④ 角色此刻的状态")
        try:
            state = affect.load_state(character, "probe-user")
            print(f"  {character.get('name')}：{state.get('emotion')}"
                  f"（好感档位 {affect.tone_band(int(state.get('favorability') or 0))['label']}）")
            for line in affect.prompt_block(state).splitlines():
                print(f"  {C_DIM}{line}{C_RESET}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {C_RED}读状态失败：{exc}{C_RESET}")

    return {"cleaned": cleaned, "plan": turn_plan}


# ---------------------------------------------------------------------------
# 真跑（可选）
# ---------------------------------------------------------------------------

async def run_live(text: str, signals, character: dict, book_title: str, user_name: str) -> str:
    from agent import graph as agent_graph
    from agent.manager import get_agent

    settings = get_settings()
    api_key = settings.api_key_for(None)
    if not api_key:
        print(f"{C_RED}没有 API Key，跳过真跑。{C_RESET}")
        return ""

    async def llm(state: dict):
        agent = await asyncio.to_thread(
            get_agent,
            api_key=api_key,
            character=character,
            book_title=book_title,
            user_name=user_name,
        )
        import copy as _copy

        per_run = _copy.copy(agent)
        per_run.additional_context = state.get("context_block") or None
        accumulated = ""
        async for chunk in per_run.arun(state.get("text"), user_id="probe-user", stream=True):
            content = getattr(chunk, "content", None) or ""
            if not content:
                continue
            if content.startswith(accumulated):
                delta = content[len(accumulated):]
                accumulated = content
            else:
                delta = content
                accumulated += content
            if delta:
                yield delta

    _section("⑤ 生成（真跑，会消耗 token）")
    final: dict = {}
    async for mode, chunk in agent_graph.get_turn_graph().astream(
        {
            "user_id": "probe-user",
            "user_name": user_name,
            "character": character,
            "book_title": book_title,
            "shelf_id": "",
            "raw_text": text,
            "image_urls": [],
            "api_key": api_key,
            "model": settings.deepseek_model,
            "extra_context": "",
            "persist": False,          # 探测不写库
            "session_id": "probe-session",
        },
        config={
            "configurable": {"thread_id": "probe", "llm": llm, "summarizer": None},
            "recursion_limit": 100,
        },
        stream_mode=["custom", "values"],
    ):
        if mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "delta":
                print(chunk["text"], end="", flush=True)
        elif isinstance(chunk, dict) and chunk.get("reply"):
            final = chunk

    print()
    notes = final.get("notes") or []
    if notes:
        _section("⑥ 后置校验改了什么")
        for note in notes:
            print(f"  · {note}")
    else:
        print(f"{C_DIM}  （校验未改动回复）{C_RESET}")
    return final.get("reply") or ""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def probe_one(args, text: str) -> bool:
    shelf = find_shelf(args.shelf)
    if shelf is None:
        print(f"{C_RED}找不到书架：{args.shelf}{C_RESET}")
        return False

    character = None
    if args.character:
        character = find_character(shelf, args.character)
        if character is None:
            print(f"{C_RED}书架「{shelf['title']}」里找不到角色：{args.character}{C_RESET}")
            members = db.ensure_shelf_members(shelf["id"])
            print(f"  可用：{', '.join(m['name'] for m in members[:12])}")
            return False

    _hr()
    who = f"单聊 · {character['name']}" if character else f"群聊 · {shelf['title']}"
    print(f"{C_BOLD}【{who}】{C_RESET}  {text}")

    character_dict = None
    book_title = ""
    if character:
        character_dict = db.get_character(character["character_id"])
        book = db.get_book(character_dict["book_id"]) if character_dict else None
        book_title = (book or {}).get("title") or ""

    signals = await analyze(text, character=character_dict)
    show_analysis(text, signals, character_dict)

    if args.live:
        if character_dict is None:
            print(f"{C_YELLOW}--live 目前只支持单聊（群聊请用界面或 --shelf 不加 --character 看判断）。{C_RESET}")
        else:
            await run_live(text, signals, character_dict, book_title, args.user_name)

    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="对话测试器：看判断、SOP、生成与校验")
    parser.add_argument("--shelf", default="", help="书架名或 id（省略则用第一个）")
    parser.add_argument("--character", default="", help="角色名或 id；给了就是单聊")
    parser.add_argument("--user-name", default="读者", help="你希望角色怎么称呼你")
    parser.add_argument("--text", default="", help="一句话")
    parser.add_argument("--batch", default="", help="批量：每行一句话的文本文件")
    parser.add_argument("--live", action="store_true", help="真的调模型生成（默认只做判断）")
    args = parser.parse_args()

    texts: list[str] = []
    if args.batch:
        path = Path(args.batch)
        if not path.is_file():
            print(f"{C_RED}找不到文件：{path}{C_RESET}")
            return 1
        texts = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
    if args.text:
        texts.append(args.text)
    if not texts:
        parser.print_help()
        return 1

    ok = True
    for text in texts:
        ok = await probe_one(args, text) and ok

    if len(texts) > 1:
        _hr()
        print(f"跑完 {len(texts)} 条")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))