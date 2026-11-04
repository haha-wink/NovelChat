"""Web 渠道：浏览器 + SSE。这是当前唯一真正启用的渠道。

它同时是"参照实现"——接 QQ/微信时照这个形状写即可。
"""

from __future__ import annotations

import json
from typing import Any

from channels.base import (
    KIND_DONE,
    Channel,
    ChannelCapabilities,
    InboundMessage,
    OutboundMessage,
)


class WebChannel(Channel):
    name = "web"
    enabled = True

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name=self.name,
            supports_mentions=True,
            supports_images=True,
            supports_stickers=True,
            supports_streaming=True,
            max_text_length=20000,
            notes="浏览器 + SSE，逐字推送；支持 @ 与贴纸",
        )

    def render(self, outbound: OutboundMessage) -> dict[str, Any]:
        """出站消息 -> SSE 事件字典（形状与群聊引擎事件一致，前端不用改）。"""
        if outbound.kind == KIND_DONE:
            return {"type": "done", **(outbound.meta or {})}

        payload: dict[str, Any] = {}
        type_map = {
            "user": "user",
            "start": "speaker_start",
            "text": "delta",
            "end": "speaker_end",
            "system": "system",
            "error": "error",
        }
        payload["type"] = type_map.get(outbound.kind, outbound.kind)
        if outbound.sender_id:
            payload["character_id"] = outbound.sender_id
        if outbound.sender_name:
            payload["name"] = outbound.sender_name
        if outbound.text:
            if outbound.kind == "error":
                payload["message"] = outbound.text
            else:
                payload["text"] = outbound.text
        payload.update(outbound.meta or {})
        return payload

    def to_sse(self, event: dict[str, Any]) -> str:
        """把事件字典序列化成一条 SSE 帧。"""
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    def parse(self, raw: dict[str, Any]) -> InboundMessage:
        return InboundMessage(
            channel=self.name,
            conversation_id=str(raw.get("shelf_id") or ""),
            sender_id=str(raw.get("user_id") or ""),
            sender_name=str(raw.get("display_name") or ""),
            text=str(raw.get("prompt") or ""),
            mentions=list(raw.get("mentions") or []),
            images=list(raw.get("images_base64") or []),
            raw=raw,
        )


#: 单例：路由直接用
channel = WebChannel()