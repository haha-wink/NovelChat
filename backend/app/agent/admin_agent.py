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
- 如果角色汇报里已经明确完成（例如「已开渲任务 anim_xxx」），done=true。
- 如果还缺关键信息/还有没做的部分，done=false，next 最多 {max_steps} 个。
- 做不了的部分要如实写进 summary，不要假装完成。
"""

_ROLE_DECIDE_PROMPT = """你是角色「{name}」（{role}），管理员把下面这件事派给你：

{task}

共享工作记忆（含管理员的计划）：
{notes}

先想清楚：这件事你直接回答/汇报，还是需要调用工具（目前只有动画/MV 工具 animation_start）？
只输出 JSON：
{{"thought": "一句话",
  "action": "reply" 或 "animation",
  "prompt": "如果 action=animation，写清楚要做什么（可含风格/时长/段落）"}}

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
            all_jobs.extend(result.get("jobs") or [])

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

    if not summary:
        summary = "这轮没有产出可汇报的结果。"

    try:
        db.add_manager_memory(
            shelf_id, "admin",
            f"{cleaned[:80]} → {summary[:200]}",
            payload={"goal": goal, "jobs": [j["id"] for j in all_jobs]},
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