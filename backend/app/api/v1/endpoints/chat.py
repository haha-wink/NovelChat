"""对话：单聊与群聊的 SSE 流。"""

from fastapi import APIRouter

from api.v1.deps import *  # noqa: F401,F403

router = APIRouter()


@router.post("/shelves/{shelf_id}/chat")
async def shelf_chat(shelf_id: str, req: ChatRequest, request: Request):
    """群聊：一条消息 -> 若干角色按「发言意愿」接力说话（SSE 逐条推送）。

    成本红线：一轮最多 ``group_max_speakers`` 人、最多 ``group_max_rounds`` 轮，
    也就是单次最多 4 次模型调用（默认 2×2）。
    """
    settings = get_settings()
    session = resolve_session(req.token, request)

    shelf = await anyio.to_thread.run_sync(db.get_shelf, shelf_id)
    if not shelf:
        raise HTTPException(status_code=404, detail="书架不存在")

    prompt = (req.prompt or "").strip()
    if len(prompt) > settings.max_prompt_chars:
        raise HTTPException(
            status_code=413, detail=f"单次输入过长（上限 {settings.max_prompt_chars} 字）"
        )

    try:
        images = decode_images(
            req.images_base64,
            max_count=settings.max_images,
            max_image_bytes=settings.max_image_bytes,
            max_total_bytes=settings.max_total_image_bytes,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not prompt and not images:
        raise HTTPException(status_code=400, detail="内容为空：请输入文字或上传截图")

    # 图片落盘一次：本回合所有发言人都看得到同一批，历史里也能翻回来看
    agno_images: list = []
    image_urls: list[str] = []
    if images:
        upload_root = settings.uploads_path
        upload_root.mkdir(parents=True, exist_ok=True)
        folder = uuid.uuid4().hex
        image_dir = upload_root / "chat" / folder
        image_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(images):
            file_name = f"image-{index}.{item.suffix}"
            file_path = image_dir / file_name
            file_path.write_bytes(item.data)
            agno_images.append(AgnoImage(filepath=str(file_path)))
            image_urls.append(f"/api/chat-image/{folder}/{file_name}")

    # 纯图片时也要给模型一句话，否则 arun 拿到空串
    user_text = prompt or "我发了一张图片，你看看。"

    api_key = settings.api_key_for(req.api_key)
    if not api_key:
        raise HTTPException(
            status_code=503, detail="服务端未配置 DEEPSEEK_API_KEY"
        )

    async def generate(member: dict, transcript: list[dict]):
        """给 group_chat 用的生成器：构建角色 Agent -> 流式产出文本片段。"""
        character = await anyio.to_thread.run_sync(
            require_character, member["character_id"]
        )
        book = (await anyio.to_thread.run_sync(db.get_book, character["book_id"])) or {}
        agent = await anyio.to_thread.run_sync(
            functools.partial(
                get_agent,
                api_key=api_key,
                character=character,
                book_title=book.get("title") or "",
                user_name=session.display_name,
            )
        )
        agent, _state = await anyio.to_thread.run_sync(
            functools.partial(
                character_agent.attach_state,
                agent,
                character,
                session.user_id,
                user_text=user_text,
            )
        )
        # 附加群聊上下文：后说话的人要接得上前面的话
        lines = [f"{t['speaker']}：{t['text']}" for t in transcript[-8:]]
        agent.additional_context = (
            (agent.additional_context or "")
            + "\n\n【群聊上下文，最近几条】\n"
            + "\n".join(lines)
            + f"\n\n你在群聊里，用「{member['name']}」的身份说话：**只说 1 句，最多 2 句短话**；"
            "不要写动作、神态、心理描写，不要复述别人说过的话。"
        )

        accumulated = ""
        async for chunk in agent.arun(
            user_text,
            images=agno_images or None,
            user_id=session.user_id,
            session_id=conversation_session(session, member["character_id"]),
            stream=True,
        ):
            content = getattr(chunk, "content", None) or ""
            if not content or should_skip(content):
                continue
            if content.startswith(accumulated):
                delta = content[len(accumulated):]
                accumulated = content
            elif accumulated.endswith(content):
                delta = ""
            else:
                delta = content
                accumulated += content
            if delta:
                yield delta

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in group_chat.run_turn(
                shelf_id=shelf_id,
                user_id=session.user_id,
                text=prompt or "［图片］",
                image_urls=image_urls,
                generate=generate,
                session_id=f"group:{session.user_id}:{shelf_id}",
            ):
                # 引擎事件 -> 渠道无关的出站消息 -> web 载荷。
                # 这一圈看着绕，但换来的是：接 QQ/微信时引擎与这里一行都不用改。
                outbound = event_to_outbound(
                    event, channel=web_channel.name, conversation_id=shelf_id
                )
                yield web_channel.to_sse(web_channel.render(outbound))
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("群聊出错（shelf=%s）", shelf_id)
            yield sse({"type": "error", "message": f"群聊失败：{exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/chat-image/{folder}/{name}")
async def chat_image(folder: str, name: str) -> FileResponse:
    """读回聊天里发过的图片。

    目录名固定成 32 位 hex、文件名固定成 image-N.扩展名——只允许这种形状，
    避免把 uploads 目录当成任意文件下载口。
    """
    if not re.fullmatch(r"[0-9a-f]{32}", folder):
        raise HTTPException(status_code=404, detail="图片不存在")
    if not re.fullmatch(r"image-\d+\.(png|jpg|jpeg|gif|webp)", name):
        raise HTTPException(status_code=404, detail="图片不存在")
    path = get_settings().uploads_path / "chat" / folder / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    settings = get_settings()
    session = resolve_session(req.token, request)

    character_ids = [cid for cid in (req.character_ids or []) if cid] or (
        [req.character_id] if req.character_id else []
    )
    if not character_ids:
        raise HTTPException(status_code=400, detail="缺少 character_id：请先选择角色")
    if len(character_ids) > 1:
        raise HTTPException(status_code=400, detail="当前版本一次只能和一个角色对话")
    character_id = character_ids[0]

    prompt = (req.prompt or "").strip()
    if len(prompt) > settings.max_prompt_chars:
        raise HTTPException(
            status_code=413, detail=f"单次输入过长（上限 {settings.max_prompt_chars} 字）"
        )

    try:
        images = decode_images(
            req.images_base64,
            max_count=settings.max_images,
            max_image_bytes=settings.max_image_bytes,
            max_total_bytes=settings.max_total_image_bytes,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not prompt and not images:
        raise HTTPException(status_code=400, detail="内容为空：请输入文字或上传截图")

    api_key = settings.api_key_for(req.api_key)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DEEPSEEK_API_KEY，请在 backend/.env 中设置后重启后端",
        )

    character = await anyio.to_thread.run_sync(require_character, character_id)
    book = await anyio.to_thread.run_sync(require_book, character["book_id"])

    build = functools.partial(
        get_agent,
        api_key=api_key,
        character=character,
        book_title=book["title"],
        user_name=session.display_name,
    )
    try:
        agent = await anyio.to_thread.run_sync(build)
        # 注入本轮情绪状态 + RAG 检索到的相关记忆。
        # attach_state 返回的是 Agent 的**副本**，缓存里的共享实例不受影响（并发下不串状态）。
        agent, affect_state = await anyio.to_thread.run_sync(
            functools.partial(
                character_agent.attach_state,
                agent,
                character,
                session.user_id,
                user_text=prompt,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("构建角色 Agent 失败")
        raise HTTPException(status_code=500, detail=f"角色初始化失败：{exc}") from exc

    agno_session = conversation_session(session, character_id)

    # 本轮用了什么情绪说话，记一笔便于复盘语气
    agent_log.log(
        "chat_turn_started",
        owner_type="character",
        owner_id=character_id,
        book_id=book["id"],
        user_id=session.user_id,
        session_id=agno_session,
        severity="debug",
        payload={
            "mood": affect_state.get("emotion"),
            "turns": affect_state.get("turns", 0),
            "has_images": bool(images),
        },
    )

    image_urls: list[str] = []

    async def event_stream() -> AsyncIterator[str]:
        try:
            agno_images = []
            if images:
                upload_root = settings.uploads_path
                upload_root.mkdir(parents=True, exist_ok=True)
                folder = uuid.uuid4().hex
                folder_path = upload_root / "chat" / folder
                folder_path.mkdir(parents=True, exist_ok=True)
                for index, item in enumerate(images):
                    file_name = f"image-{index}.{item.suffix}"
                    file_path = folder_path / file_name
                    file_path.write_bytes(item.data)
                    agno_images.append(AgnoImage(filepath=str(file_path)))
                    image_urls.append(f"/api/chat-image/{folder}/{file_name}")

            accumulated = ""
            async for chunk in agent.arun(
                prompt or "请看着这张截图，说说你的想法。",
                images=agno_images or None,
                user_id=session.user_id,
                session_id=agno_session,
                stream=True,
            ):
                if await request.is_disconnected():
                    logger.info("客户端断开，停止流（session=%s）", agno_session)
                    break

                content = getattr(chunk, "content", None) or ""
                if not content or should_skip(content):
                    continue

                if content.startswith(accumulated):
                    delta = content[len(accumulated):]
                    accumulated = content
                elif accumulated.endswith(content):
                    delta = ""
                else:
                    delta = content
                    accumulated += content

                if delta:
                    yield sse({"type": "delta", "text": delta})

            # 去掉动作/神态旁白（模型偶尔仍会写）。只影响最终文本——
            # 前端收到 final 后用干净文本覆盖刚才流式显示的版本。
            clean_reply = reply_mod.strip_stage_directions(accumulated)
            if clean_reply and clean_reply != accumulated.strip():
                accumulated = clean_reply
                yield sse({"type": "final", "text": clean_reply})

            # 情绪与好感度推进：放在回复生成完成之后，不拖慢首字；失败不影响回复。
            # 数值不返回给前端（用户不看数字），只影响下一轮的语气。
            state_after: dict = {}
            try:
                state_after = await anyio.to_thread.run_sync(
                    functools.partial(
                        affect.observe_user_message,
                        character=character,
                        user_id=session.user_id,
                        text=prompt,
                        image_count=len(images),
                        session_id=agno_session,
                    )
                ) or {}
            except Exception:  # noqa: BLE001
                logger.exception("情绪更新失败（已忽略）")

            # 记一条对话流水：7 天窗口内保留原文，超期由 chat_memory 蒸馏成长期记忆。
            # 没有这条记录，角色就永远不记得"我们聊过什么"。
            try:
                shelf_id = str(book.get("shelf_id") or "")
                if shelf_id and accumulated.strip():
                    for role, text, speaker in (
                        ("user", prompt, session.display_name),
                        ("assistant", accumulated, character.get("name") or ""),
                    ):
                        await anyio.to_thread.run_sync(
                            functools.partial(
                                db.add_chat_turn,
                                shelf_id=shelf_id,
                                user_id=session.user_id,
                                role=role,
                                content=text,
                                character_id=character["id"],
                                session_id=agno_session,
                                speaker_name=speaker,
                                images=image_urls if role == "user" else [],
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.exception("记录对话流水失败（已忽略）")

            # 条数过了阈值就顺手蒸馏一半（低于阈值时只有一次 COUNT，很便宜）
            try:
                shelf_id = str(book.get("shelf_id") or "")
                if shelf_id:
                    await anyio.to_thread.run_sync(
                        functools.partial(
                            chat_memory.maybe_distill,
                            shelf_id,
                            character["id"],
                            summarizer=memory_summarizer(),
                            user_id=session.user_id,
                            character_name=character.get("name") or "",
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.exception("对话蒸馏失败（已忽略）")

            # 贴纸：问起状态/心情、或者这一轮心情变了 -> 一定发；否则按情绪强度掷骰子。
            try:
                mood_changed = bool(
                    state_after.get("previous_emotion")
                    and state_after["previous_emotion"] != state_after.get("emotion")
                )
                sticker = expression.maybe_sticker(
                    state_after,
                    mood_changed=mood_changed,
                    asked_about_mood=expression.asks_about_mood(prompt),
                )
                if sticker:
                    yield sse({"type": "sticker", **sticker})
            except Exception:  # noqa: BLE001
                logger.exception("贴纸决策失败（已忽略）")

            yield sse({"type": "done"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("流式响应出错（session=%s）", agno_session)
            yield sse({"type": "error", "message": f"生成失败：{exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
