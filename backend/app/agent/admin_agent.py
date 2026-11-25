"""工作模式的管理员 Agent：ReAct 调度器。

管理员不是书架成员（不写 characters / shelf_members，避免污染用户数据），
工作模式每一轮由 ``prepare_node`` 临时拉起：

```
Thought     拆目标：这件事该谁做、先做什么
Action      派发给角色 Agent（角色自己再跑一轮：思考 → 调工具 → 汇报）
Observation 收报告 + 任务状态
循环        没完成就重新规划，最多 MAX_CYCLES 轮
```

管理员自己的记忆分三层（和角色 Agent 对齐，但没有初始记忆——它不是小说里的人）：

* 工作记忆：当前对话的 plan / dispatch / observation（``working_memory``，agent_id=admin）
* 短期上下文：本次请求的对话（调用方传入最近几条），以及各角色共享的工作记忆
* 长期记忆：``manager_memories.kind='admin'``，按书架存，跨对话

管理员**只读**角色的工作记忆，用来校验/防冲突；自己的调度笔记写台账。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Optional

from core import database as db
from core.config import get_settings

logger = logging.getLogger(__name__)

#: 管理员最多重新规划几次（每次规划最多派发 MAX_STEPS 个角色任务）
MAX_CYCLES = 3
MAX_STEPS = 3
#: 单次模型调用的 token 上限
MAX_TOKENS = 900

ADMIN_AGENT_ID = "admin"

_PLAN_PROMPT = """你是工作模式的管理员 Agent，负责统筹一个「角色群」完成用户的任务。

用户的话：
{text}

群里的角色（各自有专长和记忆，你看不到他们的完整记忆，但能看到共享工作记忆）：
{roster}

这个对话已有的工作记忆（含各角色汇报）：
{notes}

管理员长期记忆（以前的书架经验）：
{long_term}

请用 ReAct 的方式拆解目标：先判断哪些部分你自己就能决定，哪些必须派给哪个角色。
只输出 JSON：
{{"thought": "你的判断（一两句）",
  "goal": "整体目标（一句话）",
  "steps": [{{"agent": "角色名", "task": "给这个角色的具体任务（一句话）"}}],
  "done": false}}

规则：
- steps 最多 {max_steps} 个，只挑真正需要的角色；没人能做的部分你自己在 thought 里说明。
- 任务要具体、可执行；不要派「和用户聊天」这种空任务。
- 角色名必须从上面的名单里选，选不到就留空 steps。

【渲染器硬边界，非常重要】
画面只能画：场景 / 天气 / 时间段 / 配色 / 镜头运动（车站、火车、雨、雪、月亮、星星、山、海、城市、窗、路、花、伞…）
以及逐句歌词字幕。做不到：人物长相/性别/服装、具体剧情表演、谐音梗字幕替换、
屏幕内外特效、逐帧卡点。
- 用户要的东西超出边界时，翻译成「画面里能出现的场景/物件」（例如「锦鲤」→ 画面里的鱼和河），
  并在汇总里如实说明哪些做不到；**不要替用户脑补分镜和人物**。
- 拆分任务时让角色只提「场景/镜头」层面的方案，不要让角色替用户编剧情。

【一个需求只起一个任务】
- 渲染任务只能派给**一个**角色去调工具；其他角色只出方案 / 分镜 / 配色建议，不要让他们也起任务。
- 方案型任务里别写「渲染/生成/做一版」，写「给出…方案」「列出…分镜」，否则角色会误以为要自己起任务。
- 用户同一个需求连续追问时，先看工作记忆里有没有刚起的任务；有就只补方案或换画质，别重复开新任务。

【派活必须可执行】
- 角色能做的只有：起渲染任务、查任务状态、改需求重渲、写文字方案。
- 不要派「看关键帧/确认画面/核对截图/检查成片内容」——角色看不了图，只会回「我做不到」。
- 派给执行角色时必须把用户的风格词原样带上（水墨/像素/赛博朋克/胶片…），
  不要改写成「写实」「电影感」，也不要省略；风格丢了渲染会退回默认水彩。
- 用户没提风格时**不要自己发明**「写实/电影感」：留空，系统用默认水彩；
  「写实」这种词会被识别成胶片风，但模板画不出写实画面，只会名不副实。
- 修改已渲染的任务：派「改需求并重渲」，任务里写清改哪一处，并带上任务号（anim_…）。

【必须派活的情况】
只要用户这句话是在提产出需求（视频 / MV / 动画 / 分镜 / 渲染 / 画面 / 成片 / 歌词画面…）
或者是在修改、补充这类需求，**steps 至少要有 1 个**，派给角色去执行（调用动画工具）。
不允许把它当成「信息整理」后直接汇总；只有纯闲聊、纯查询、纯记笔记才允许 steps 为空。
"""

_EVAL_PROMPT = """你是工作模式的管理员 Agent，正在循环推进用户的任务。

整体目标：{goal}
用户的话：{text}

本轮各角色汇报：
{reports}

当前任务状态：
{jobs}

请判断整体目标是否已经完成。只输出 JSON：
{{"done": true/false,
  "thought": "一句话判断",
  "summary": "如果 done=true，给用户的最终汇总（两三句，说明做了什么、结果在哪）",
  "next": [{{"agent": "角色名", "task": "还差的部分派给谁"}}]}}

规则：
- 如果用户要的是成片（MV/视频/渲染），**必须看到本轮的渲染任务**（jobs 非空）或角色明确
  说明「渲染器做不到/缺少素材」才允许 done=true；只是聊完方案不算完成。
- 如果角色汇报里已经明确完成（例如「已开渲任务 anim_xxx」），done=true。
- 角色汇报出现「做不到 / 没有权限 / 看不了」时，**不要原样再派一次**：
  换成「改需求重渲」或直接收尾并在汇总里说明边界。
- 如果还缺关键信息/还有没做的部分，done=false，next 最多 {max_steps} 个。
- 做不了的部分要如实写进 summary，不要假装完成。
"""

#: 一眼能看出「这是要产出成片」的词：管理员不能只汇总，必须派活
_ACTION_HINTS = (
    "渲染", "视频", "MV", "mv", "动画", "成片", "歌词", "画面", "镜头", "分镜",
    "重渲", "做一段", "生成", "剪",
)


#: 「明显在要求动手做」的动词：光有场景词/画面词不算（"配色方案"不该起任务）
_EXECUTE_HINTS = (
    "做一段", "做一个", "做一版", "做成", "做版", "渲染成片", "渲染一版",
    "生成", "起一个", "起个", "开渲", "拍一段", "剪一段", "来一段", "出一段",
    "输出成片", "重渲",
)


def _looks_like_video_request(text: str) -> bool:
    return any(word in (text or "") for word in _ACTION_HINTS)


#: 渲染器做不到的东西 → 汇总里要明说，别让用户以为「画面元素对了 = 需求做完了」
_LIMIT_WORDS: tuple[tuple[str, str], ...] = (
    ("女主", "人物长相"),
    ("男主", "人物长相"),
    ("人物", "人物长相"),
    ("剧情", "剧情表演"),
    ("谐音", "谐音梗字幕"),
    ("字幕替换", "屏幕字幕替换"),
    ("特效", "屏幕内外特效"),
)


def _limits_note(text: str) -> str:
    """把用户需求里超出渲染器能力的那部分列出来，附在汇总后面。"""
    hits = [label for word, label in _LIMIT_WORDS if word in (text or "")]
    if not hits:
        return ""
    return "（说明：渲染器只能画场景/物件/镜头，" + "、".join(
        dict.fromkeys(hits)
    ) + "做不到，已用画面元素近似表达。）"


def _asks_to_render(text: str) -> bool:
    """任务里既有产出物、又有「动手做」的动词，才算真的要起渲染任务。

    只写「给出配色与画面质感方案」的顾问型任务不在此列——否则三个角色
    会一人起一个任务，同一个需求变成三版成片。
    """
    body = text or ""
    return (
        any(word in body for word in _EXECUTE_HINTS)
        and any(word in body for word in _ACTION_HINTS)
    )


_ROLE_DECIDE_PROMPT = """你是角色「{name}」（{role}），管理员把下面这件事派给你：

{task}

共享工作记忆（含管理员的计划）：
{notes}

先想清楚：这件事你直接回答/汇报，还是需要调用工具？你现在有三个工具：

- animation_start：起一个新的动画/MV 渲染任务（action="animation"，写 prompt）
- animation_status：查某个任务的进度/需求/风格（action="status"，给 job_id）
- animation_rerender：改任务的需求/风格并重渲（action="rerender"，给 job_id，可带新 prompt）
  你**没有**看图/看关键帧的能力：别说"确认了画面"，只能查状态、改需求、重渲。

只输出 JSON：
{{"thought": "一句话",
  "action": "reply" | "animation" | "status" | "rerender",
  "job_id": "action=status/rerender 时必填",
  "prompt": "action=animation/rerender 时写清楚要做什么（可含风格/时长/段落）"}}

你查任务用 action="status"（给 job_id），改已渲染任务用 action="rerender"
（给 job_id，可带新 prompt）；你**看不了关键帧/截图**，别声称核对过画面。

调用动画工具时，prompt 必须写成**渲染器看得懂的镜头描述**：只写场景、天气、物件、时间段、
配色和风格（例如「水墨风，雨夜车站，站台，伞，远处的月亮，蓝色鱼影」），
不要写人物长相、剧情、谐音梗或屏幕文字特效——这些渲染器做不到，写了也只会得到默认画面。
做不到的部分放在给管理员的汇报里，不要假装完成。
"""


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------
def _llm(
    api_key: str,
    messages: list[dict],
    *,
    json_mode: bool = True,
    temperature: float = 0.3,
) -> str:
    """同步调一次 DeepSeek；在调用方用 to_thread 包起来。"""
    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60)
    kwargs: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()


def _json(text: str) -> dict:
    """尽量把模型输出解析成 dict；失败返回空 dict（调用方走兜底）。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(raw[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------
def _shared_notes(conversation_id: str) -> str:
    """管理员视角的工作记忆：所有 agent 的笔记（自己 + 角色）。"""
    try:
        return db.working_memory_text(conversation_id, limit=40)[:1800]
    except Exception:  # noqa: BLE001
        return ""


def _long_term(shelf_id: str) -> str:
    try:
        rows = db.list_manager_memories(shelf_id, kind="admin", limit=6)
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    return "\n".join(f"- {str(row.get('content') or '')[:160]}" for row in rows)


def _remember(
    conversation_id: str,
    content: str,
    *,
    kind: str,
    shelf_id: str = "",
    user_id: str = "",
    agent_id: str = ADMIN_AGENT_ID,
) -> None:
    if not conversation_id or not content:
        return
    try:
        from services.memory import working_memory

        working_memory.remember(
            conversation_id, content, shelf_id=shelf_id, user_id=user_id,
            kind=kind, mode="work", agent_id=agent_id,
        )
    except Exception:  # noqa: BLE001 - 记忆失败不能挡住调度
        logger.exception("管理员写工作记忆失败（已忽略）")


# ---------------------------------------------------------------------------
# 角色 Agent 的 ReAct（收到任务 -> 思考 -> 调工具/回答 -> 汇报）
# ---------------------------------------------------------------------------
def _role_agent(api_key: str, member: dict) -> Optional[Any]:
    from agent import manager

    character = db.get_character(str(member.get("character_id") or ""))
    if not character:
        return None
    book = db.get_book(str(character.get("book_id") or "")) or {}
    try:
        return manager.get_agent(
            api_key=api_key,
            character=character,
            book_title=book.get("title") or "",
            user_name="朋友",
        )
    except Exception:  # noqa: BLE001
        logger.exception("管理员：构建角色 Agent 失败：%s", member.get("name"))
        return None


def _role_reply(api_key: str, member: dict, task: str, notes: str,
                conversation_id: str) -> str:
    agent = _role_agent(api_key, member)
    if agent is None:
        return f"（{member.get('name')} 不在线，没能接这个任务）"
    prompt = (
        f"【管理员派给你的任务】{task}\n\n"
        f"【共享工作记忆】\n{notes[:1200]}\n\n"
        "请以你的身份，用两三句话汇报你做了什么/建议怎么做；"
        "做不到就直说，不要假装完成。"
    )
    response = agent.run(
        prompt,
        user_id="",
        session_id=f"work:{conversation_id}:{member.get('character_id') or ''}",
    )
    return str(getattr(response, "content", "") or "").strip()


def _section_hint(text: str) -> str:
    """从需求里认「主歌 / 副歌」：认不出来交给引擎，认出来只渲这一段。"""
    body = str(text or "")
    if any(word in body for word in ("副歌", "高潮", "chorus")):
        return "chorus"
    if any(word in body for word in ("主歌", "verse")):
        return "verse"
    return ""


_STAGE_TEXT = {
    "queued": "排队中", "scaffold": "搭项目", "storyboard": "写分镜",
    "awaiting_confirmation": "等确认", "render": "逐帧渲染", "encode": "编码",
    "done": "完成", "failed": "失败",
}


def _job_status_report(job_id: str) -> str:
    """查任务状态：角色不会「看不见任务」了，能如实汇报进度。"""
    if not job_id:
        return "没给任务号，也没在本轮笔记里找到最近的动画任务。"
    try:
        from skills.work.animation import engine as animation_engine

        job = animation_engine.job_status(job_id)
    except Exception as exc:  # noqa: BLE001
        return f"查任务 {job_id} 失败：{exc}"
    if not job:
        return f"没有找到任务 {job_id}（可能已被删除）。"
    stage = _STAGE_TEXT.get(str(job.get("stage") or ""), str(job.get("stage") or "未知"))
    percent = int(round(float(job.get("progress") or 0) * 100))
    return (
        f"任务 {job_id}：{stage}（{percent}%），风格 {job.get('style') or '水彩'}，"
        f"需求：{str(job.get('prompt') or '')[:80]}"
    )


def _job_rerender(job_id: str, prompt: str, notes: str) -> str:
    """改需求/风格并重渲：这是「修改已渲染任务」唯一靠谱的做法。"""
    if not job_id:
        return "没给任务号，也没在本轮笔记里找到最近的动画任务，无法重渲。"
    try:
        from skills.work.animation import engine as animation_engine
        from skills.work.animation.styles import resolve_style

        job = animation_engine.job_status(job_id)
        if not job:
            return f"没有找到任务 {job_id}（可能已被删除）。"
        new_prompt = " ".join(str(prompt or "").split())
        if not new_prompt:
            # 没给新需求：只按原需求+补充笔记重渲（风格也重新解析一次）
            new_prompt = str(job.get("prompt") or "")
        style = resolve_style(f"{new_prompt} {notes}")
        updated = animation_engine.update_prompt(job_id, new_prompt, style=style)
        if not updated.get("ok"):
            return f"改需求失败：{updated.get('error') or '未知原因'}"
        result = animation_engine.rerender(job_id, str(job.get("quality") or "fast"))
        if not result.get("ok"):
            return f"{job_id} 已改需求，但重渲没起来：{result.get('error') or '未知原因'}"
        return f"已按新需求重渲 {job_id}（风格 {style}），进度看任务栏。"
    except Exception as exc:  # noqa: BLE001
        logger.exception("角色：重渲任务失败")
        return f"重渲任务失败：{exc}"


def _animation_tool(
    prompt: str, notes: str, assets: list[dict], conversation_id: str
) -> dict:
    """角色 Agent 的工具：起一个动画/MV 任务。

    ``notes``（共享工作记忆）里往往有用户前面说过的场景/风格词，但角色给的工具 prompt
    可能只写了一句「做这个 MV」。这里把笔记里的场景词并进去：引擎按关键词排镜头，
    没有关键词时只会轮换 8 种默认构图——那正是"视频没接住需求"的根因。
    """
    import skills
    from skills.work.animation.scene_gen import scene_keywords
    from skills.work.animation.styles import resolve_style

    skill = skills.get("animation")
    if skill is None:
        return {"ok": False, "error": "动画技能没加载"}
    text = prompt
    try:
        words = scene_keywords(f"{prompt} {notes}")
        if words:
            text = f"{prompt}；画面元素：{'、'.join(words[:12])}"
    except Exception:  # noqa: BLE001 - 关键词提取失败就把原 prompt 交出去
        logger.exception("管理员：提取画面关键词失败（已忽略）")
    payload = {
        "text": text,
        "emotion": "平静",
        "intent": "work",
        "style": resolve_style(f"{prompt} {notes}"),
        "section": _section_hint(f"{prompt} {notes}"),
        "lyrics": "",
        "lyrics_file": "",
        "audio": "",
        "duration": 0,
        "conversation_id": conversation_id,
    }
    for asset in assets or []:
        kind = str(asset.get("kind") or "")
        path = str(asset.get("path") or "")
        if kind == "lyrics":
            payload["lyrics_file"] = path
        elif kind == "audio":
            payload["audio"] = path
    # 「再改一版」这类消息通常不带附件，但歌和歌词早就传过了：
    # 从本对话最近一个任务里继承，否则新任务会变成没有歌词/配乐的默认画面。
    if not payload["lyrics_file"] or not payload["audio"]:
        try:
            from skills.work.animation import engine as animation_engine

            previous = animation_engine.list_jobs(
                limit=5, conversation_id=conversation_id
            )
        except Exception:  # noqa: BLE001 - 查历史失败就当没有
            previous = []
        if previous:
            last = previous[0]
            if not payload["lyrics_file"]:
                payload["lyrics_file"] = str(last.get("lyrics_file") or "")
            if not payload["audio"]:
                payload["audio"] = str(last.get("audio") or "")
    try:
        result = skill.run(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("管理员：动画工具失败")
        return {"ok": False, "error": str(exc)}
    jobs = []
    for output in getattr(result, "outputs", []) or []:
        if isinstance(output, dict) and output.get("ok") and output.get("job_id"):
            jobs.append(str(output["job_id"]))
    return {"ok": bool(jobs), "jobs": jobs,
            "error": "" if jobs else "动画技能没有起任务"}


async def _role_react(
    api_key: str,
    member: dict,
    task: str,
    *,
    notes: str,
    conversation_id: str,
    assets: list[dict],
    allow_tool: bool = True,
) -> dict:
    """角色 Agent 自己的一轮 ReAct：思考 -> 决定调工具还是直接汇报。"""
    decision_prompt = _ROLE_DECIDE_PROMPT.format(
        name=member.get("name") or "角色",
        role=member.get("role") or "群成员",
        task=task,
        notes=(notes or "（空）")[:1200],
    )
    decision: dict = {}
    try:
        decision = _json(await asyncio.to_thread(
            _llm, api_key,
            [{"role": "user", "content": decision_prompt}],
        ))
    except Exception:  # noqa: BLE001 - 决定失败就直接按普通汇报走
        logger.exception("管理员：角色 ReAct 决策失败：%s", member.get("name"))

    action = str(decision.get("action") or "reply").strip().lower()
    thought = str(decision.get("thought") or "").strip()
    # 模型有时把「做 MV」当成「聊方案」，任务里明明写着渲染/分镜还选 reply。
    # 这种硬需求直接改走工具，不让它用一段话把活糊过去。
    if action == "animation" and not allow_tool:
        # 本轮已经有角色在起任务了：方案型角色只汇报，别再起一版
        action = "reply"
        thought = thought or "本轮已有执行角色起任务，我只出方案"
    if (allow_tool and action not in ("animation", "status", "rerender")
            and _asks_to_render(task)):
        action = "animation"
        thought = thought or "这是产出需求，直接调动画工具"
    job_id = str(decision.get("job_id") or "").strip()
    if action in ("status", "rerender") and not job_id:
        # 没给任务号：从共享笔记里捞一个最近的任务号
        found = re.findall(r"anim_[0-9a-f]{6,}", notes or "")
        job_id = found[-1] if found else ""

    if action == "status":
        report = await asyncio.to_thread(_job_status_report, job_id)
        return {
            "agent": member.get("name") or "",
            "action": "status",
            "thought": thought or "查一下任务进度",
            "report": report,
            "jobs": [],
        }

    if action == "rerender":
        tool_prompt = str(decision.get("prompt") or "").strip()
        report = await asyncio.to_thread(
            _job_rerender, job_id, tool_prompt, notes
        )
        return {
            "agent": member.get("name") or "",
            "action": "rerender",
            "thought": thought or "按新需求重渲",
            "report": report,
            "jobs": [],
        }

    if action == "animation":
        tool_prompt = str(decision.get("prompt") or task).strip()
        tool = await asyncio.to_thread(
            _animation_tool, tool_prompt, notes, assets, conversation_id
        )
        if tool.get("ok"):
            jobs = "、".join(tool.get("jobs") or [])
            return {
                "agent": member.get("name") or "",
                "action": "animation",
                "thought": thought,
                "report": f"已开渲动画任务：{jobs}。",
                "jobs": list(tool.get("jobs") or []),
            }
        return {
            "agent": member.get("name") or "",
            "action": "animation",
            "thought": thought,
            "report": f"想调动画工具但没成功：{tool.get('error') or '未知原因'}",
            "jobs": [],
        }

    try:
        reply = await asyncio.to_thread(
            _role_reply, api_key, member, task, notes, conversation_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("管理员：角色汇报失败：%s", member.get("name"))
        reply = f"（汇报失败：{exc}）"
    return {
        "agent": member.get("name") or "",
        "action": "reply",
        "thought": thought,
        "report": reply,
        "jobs": [],
    }


# ---------------------------------------------------------------------------
# 管理员主循环
# ---------------------------------------------------------------------------
def _roster(members: list[dict]) -> str:
    lines = []
    for member in members[:24]:
        name = str(member.get("name") or "").strip()
        if not name:
            continue
        role = str(member.get("role") or member.get("profile") or "").strip()
        lines.append(f"- {name}" + (f"（{role[:30]}）" if role else ""))
    return "\n".join(lines) or "（这个书架还没有角色）"


def _strip_name(value: str, members: list[dict]) -> dict:
    """把模型给的角色名对回成员（容忍空格/全半角）。"""
    wanted = value.strip()
    if not wanted:
        return {}
    for member in members:
        name = str(member.get("name") or "").strip()
        if name and (name == wanted or name in wanted or wanted in name):
            return member
    return {}


def _jobs_text(jobs: list[dict]) -> str:
    if not jobs:
        return "（本轮没有新任务）"
    return "\n".join(
        f"- {job.get('id')}：{job.get('prompt') or ''}（{job.get('stage') or '排队'}）"
        for job in jobs
    )


async def run(
    *,
    api_key: str,
    shelf_id: str,
    conversation_id: str,
    text: str,
    members: list[dict],
    emit: Callable[[dict], None],
    user_id: str = "",
    assets: Optional[list[dict]] = None,
) -> dict:
    """跑一轮管理员 ReAct。

    返回 ``{"claimed": bool, "summary": str}``：
    ``claimed=False`` 表示这轮管理员没接（没有 key / 模型失败 / 没有角色），
    调用方照旧走单技能流程。
    """
    cleaned = " ".join(str(text or "").split())
    if not api_key or not conversation_id or not cleaned:
        return {"claimed": False, "summary": ""}
    if not members:
        return {"claimed": False, "summary": ""}
    if len(cleaned) <= 3 and cleaned not in ("做",):
        # 太短的话大概率是「嗯」「好」：别为一句附和跑三轮模型
        return {"claimed": False, "summary": ""}

    notes = await asyncio.to_thread(_shared_notes, conversation_id)
    long_term = await asyncio.to_thread(_long_term, shelf_id)
    roster = _roster(members)

    try:
        plan = _json(await asyncio.to_thread(
            _llm, api_key,
            [{"role": "user", "content": _PLAN_PROMPT.format(
                text=cleaned, roster=roster, notes=notes or "（空）",
                long_term=long_term or "（空）", max_steps=MAX_STEPS,
            )}],
        ))
    except Exception:  # noqa: BLE001
        logger.exception("管理员：规划失败，退回单技能流程")
        return {"claimed": False, "summary": ""}

    if not plan:
        return {"claimed": False, "summary": ""}

    # 模型有时把「做 MV」当成「整理需求」：steps 空着就收尾。
    # 这种时候别让它偷懒：明显是产出需求就兜底派给第一个角色去调工具。
    raw_steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    useful = [
        step for step in raw_steps
        if isinstance(step, dict) and str(step.get("task") or "").strip()
        and _strip_name(str(step.get("agent") or ""), members)
    ]
    if not useful and _looks_like_video_request(cleaned):
        executor = next(
            (m for m in members if str(m.get("name") or "").strip()), {}
        )
        if executor:
            plan["steps"] = [{
                "agent": str(executor.get("name") or ""),
                "task": "调用动画工具，按用户这次的需求直接渲染成片：" + cleaned[:180],
            }]
            plan["thought"] = (
                str(plan.get("thought") or "").strip()
                + "（这是渲染需求，不能只汇总：改为直接派给执行角色）"
            ).strip()

    goal = str(plan.get("goal") or cleaned).strip()[:200]
    _remember(conversation_id, f"目标：{goal}", kind="plan", shelf_id=shelf_id,
              user_id=user_id)
    # 没有可派发的步骤时，thought 会直接变成汇总：别再单独发一条，
    # 否则用户先看到一条蓝的、再看到一条绿的，内容是同一句话。
    raw_steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if any(isinstance(step, dict) and str(step.get("task") or "").strip()
           for step in raw_steps):
        emit({"type": "admin_thought", "text": str(plan.get("thought") or "").strip()})

    all_jobs: list[dict] = []
    reports: list[str] = []
    done = False
    summary = ""
    # 同一个需求一轮只起一个任务：三个角色各起一版就是「任务重复」
    jobs_made = 0

    for cycle in range(MAX_CYCLES):
        steps = plan.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        dispatched = 0
        for step in steps:
            if dispatched >= MAX_STEPS or not isinstance(step, dict):
                continue
            member = _strip_name(str(step.get("agent") or ""), members)
            task = " ".join(str(step.get("task") or "").split())[:300]
            if not member or not task:
                continue
            name = str(member.get("name") or "")
            emit({"type": "admin_dispatch", "agent": name, "task": task})
            _remember(conversation_id, f"派给 {name}：{task}", kind="dispatch",
                      shelf_id=shelf_id, user_id=user_id)
            result = await _role_react(
                api_key, member, task,
                notes=notes, conversation_id=conversation_id,
                assets=assets or [],
                allow_tool=jobs_made == 0,
            )
            dispatched += 1
            report = str(result.get("report") or "").strip()
            if report:
                reports.append(f"{name}：{report}")
                _remember(conversation_id, f"{name}：{report}", kind="report",
                          shelf_id=shelf_id, user_id=user_id,
                          agent_id=f"character:{member.get('character_id') or ''}")
            emit({
                "type": "admin_observation", "agent": name,
                "text": report[:400],
            })
            # 工具返回的是 job_id 字符串；统一存 id，后面查状态/写长期记忆都按 id 用
            for job in result.get("jobs") or []:
                job_id = (job.get("id") or job.get("job_id")) if isinstance(job, dict) else job
                if job_id:
                    all_jobs.append(str(job_id))
                    jobs_made += 1

        jobs = []
        for job_id in dict.fromkeys(all_jobs):
            try:
                from skills.work.animation import engine as animation_engine

                status = animation_engine.job_status(job_id)
            except Exception:  # noqa: BLE001
                status = None
            if status:
                jobs.append({
                    "id": job_id,
                    "prompt": str(status.get("prompt") or ""),
                    "stage": str(status.get("stage") or ""),
                })
        # 角色明确说「做不到」两次以上就别再原样派了：换做法需要用户确认，
        # 否则会像之前那样连着几轮派「核对关键帧」，每轮都收获一句「我做不到」。
        refused = [
            report for report in reports
            if any(word in report for word in (
                "做不到", "看不了", "没有权限", "接触不到", "无法访问",
            ))
        ]
        if len(refused) >= 2:
            done = True
            summary = (str(plan.get("thought") or "").strip()
                       or "角色能做的已经做完；剩下的部分看不了图/改不了文件，需要你确认换个做法。")
            break

        if dispatched == 0:
            # 没有可派的：自己收尾
            done = True
            summary = (str(plan.get("thought") or "").strip()
                       or "这件事没有可派的角色，我直接说明：暂时做不了。")
            break

        try:
            verdict = _json(await asyncio.to_thread(
                _llm, api_key,
                [{"role": "user", "content": _EVAL_PROMPT.format(
                    goal=goal, text=cleaned,
                    reports="\n".join(reports)[-1500:] or "（空）",
                    jobs=_jobs_text(jobs), max_steps=MAX_STEPS,
                )}],
            ))
        except Exception:  # noqa: BLE001
            logger.exception("管理员：评估失败（按未完成处理）")
            verdict = {}

        if verdict.get("done"):
            done = True
            summary = str(verdict.get("summary") or "").strip()
            break

        next_steps = verdict.get("next") or []
        if not isinstance(next_steps, list) or not next_steps:
            done = True
            summary = (str(verdict.get("summary") or "").strip()
                       or "这轮只能做到这里，剩下的需要你补充信息。")
            break
        plan = {
            "thought": str(verdict.get("thought") or "").strip(),
            "goal": goal,
            "steps": next_steps,
        }

    if not done:
        summary = "管理员循环到上限还没完全做完；已完成的部分在上面的汇报里。"

    # 最后兜底：明明要成片、角色们却只出了方案（没人调工具），管理员自己起任务
    if not all_jobs and _looks_like_video_request(cleaned):
        try:
            tool = await asyncio.to_thread(
                _animation_tool, cleaned, notes, assets or [], conversation_id
            )
        except Exception:  # noqa: BLE001
            logger.exception("管理员：兜底起渲染任务失败（已忽略）")
            tool = {}
        if tool.get("ok"):
            new_jobs = [str(job) for job in tool.get("jobs") or []]
            all_jobs.extend(new_jobs)
            summary = (
                summary + "\n（没有角色动手，我直接按你的需求起了渲染任务："
                + "、".join(new_jobs) + "）"
            ).strip()

    if all_jobs:
        note = _limits_note(cleaned)
        if note and note not in summary:
            summary = f"{summary}\n{note}".strip()

    if not summary:
        summary = "这轮没有产出可汇报的结果。"

    try:
        db.add_manager_memory(
            shelf_id, "admin",
            f"{cleaned[:80]} → {summary[:200]}",
            payload={"goal": goal, "jobs": list(dict.fromkeys(all_jobs))},
        )
    except Exception:  # noqa: BLE001 - 长期记忆失败不影响本轮
        logger.exception("管理员：写长期记忆失败（已忽略）")

    job_records: list[dict] = []
    try:
        from skills.work.animation import engine as animation_engine

        for job_id in dict.fromkeys(all_jobs):
            status = animation_engine.job_status(job_id)
            if status:
                job_records.append({
                    "job_id": job_id,
                    "prompt": str(status.get("prompt") or ""),
                    "stage": str(status.get("stage") or ""),
                })
    except Exception:  # noqa: BLE001 - 任务卡只是展示，查不到不影响汇总
        logger.exception("管理员：回查任务状态失败（已忽略）")

    return {"claimed": True, "summary": summary, "jobs": job_records}