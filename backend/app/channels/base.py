"""渠道抽象层：把"引擎事件"和"某个平台怎么发出去"解耦。

## 为什么要有这一层

群聊引擎现在吐出的是 dict 事件（``speaker_start`` / ``delta`` / ``speaker_end``…），
这套东西天然是给 SSE 用的。以后要接 QQ / 微信，问题就来了：

* QQ 官方机器人**不支持流式**，一条消息只能整段发；
* 微信文本有长度上限，长回复要切段；
* 两个平台都不支持 ``@`` 的语义（或只支持部分），选角逻辑要做降级。

所以这里定三件事：

```
InboundMessage   平台进来的消息（统一形状）
OutboundMessage  引擎要发出去的消息（统一形状）
Channel          某个平台的实现：capabilities / render / parse
```

引擎只认这两个 dataclass，**不知道**底层是 SSE 还是 QQ 的 HTTP API。
接新平台 = 新增一个 adapter，群聊和情绪逻辑一行都不改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

#: 引擎事件 -> 出站消息的 kind
KIND_TEXT = "text"
KIND_START = "start"
KIND_END = "end"
KIND_SYSTEM = "system"
KIND_ERROR = "error"
KIND_DONE = "done"
#: 用户自己的消息（群聊回显用；部分平台不需要回显，由渠道自己决定怎么处理）
KIND_USER = "user"


@dataclass(frozen=True)
class ChannelCapabilities:
    """一个渠道能做什么、不能做什么。引擎据此决定要不要降级。"""

    name: str
    #: 是否支持 @ 唤醒（微信个人号基本没有，QQ 群聊有）
    supports_mentions: bool = True
    #: 是否支持图片
    supports_images: bool = False
    #: 是否支持贴纸/表情（QQ 支持表情包，微信只能用 emoji 字符）
    supports_stickers: bool = True
    #: 是否支持流式（逐字推送）
    supports_streaming: bool = True
    #: 单条消息最大字数，超出要切段
    max_text_length: int = 4000
    #: 实现状态说明（给 /api/channels 和排查用）
    notes: str = ""


@dataclass
class InboundMessage:
    """平台进来的一条消息。"""

    channel: str
    #: 会话/群 id。web 端用 shelf_id，QQ 用群号
    conversation_id: str
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    mentions: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """引擎要发出去的一条消息。"""

    channel: str
    conversation_id: str
    kind: str = KIND_TEXT
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def split(self, limit: int) -> list[str]:
        """按渠道字数上限切段（QQ/微信必须用）。"""
        limit = max(1, int(limit))
        if len(self.text) <= limit:
            return [self.text]
        return [self.text[index : index + limit] for index in range(0, len(self.text), limit)]


class Channel(ABC):
    """渠道基类。子类只需要实现三件事。"""

    name: str = "base"
    #: 是否已经能用（web = True，QQ/微信 = False 直到真正接入）
    enabled: bool = False

    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """声明能力，引擎据此降级。"""

    @abstractmethod
    def render(self, outbound: OutboundMessage) -> Any:
        """把出站消息变成该渠道的传输载荷（SSE 字典 / QQ 请求体 / …）。"""

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> InboundMessage:
        """把该渠道的原始输入变成统一的 InboundMessage。"""

    def describe(self) -> dict:
        caps = self.capabilities()
        return {
            "name": self.name,
            "enabled": self.enabled,
            "supports_mentions": caps.supports_mentions,
            "supports_images": caps.supports_images,
            "supports_stickers": caps.supports_stickers,
            "supports_streaming": caps.supports_streaming,
            "max_text_length": caps.max_text_length,
            "notes": caps.notes,
        }


# ---------------------------------------------------------------------------
# 引擎事件 <-> 出站消息
# ---------------------------------------------------------------------------
_EVENT_KIND = {
    "user": KIND_USER,
    "speaker_start": KIND_START,
    "delta": KIND_TEXT,
    "speaker_end": KIND_END,
    "system": KIND_SYSTEM,
    "error": KIND_ERROR,
    "done": KIND_DONE,
}


def event_to_outbound(
    event: dict[str, Any], *, channel: str, conversation_id: str
) -> OutboundMessage:
    """把群聊引擎的一个事件翻译成渠道无关的出站消息。

    这是整个渠道层唯一的"翻译点"：新增平台只要会处理 OutboundMessage 就行。
    未知事件类型不吞掉——``kind`` 保留原样，交给渠道决定怎么处理。
    """
    event_type = str(event.get("type") or "")
    kind = _EVENT_KIND.get(event_type) or event_type or KIND_TEXT
    text = ""
    if kind in (KIND_TEXT, KIND_END, KIND_USER):
        text = str(event.get("text") or "")
    elif kind in (KIND_SYSTEM, KIND_ERROR):
        text = str(event.get("text") or event.get("message") or "")
    elif kind.startswith("admin_"):
        # 管理员事件（思考/派发/观察/汇总）：文本要原样带到前端
        text = str(event.get("text") or "")

    meta = {
        key: value
        for key, value in event.items()
        if key not in {"type", "text", "message", "name", "character_id"}
    }
    return OutboundMessage(
        channel=channel,
        conversation_id=conversation_id,
        kind=kind,
        sender_id=str(event.get("character_id") or ""),
        sender_name=str(event.get("name") or ""),
        text=text,
        meta=meta,
    )