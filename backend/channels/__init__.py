"""渠道抽象层。

当前只有 ``web``（浏览器 + SSE）是启用的；``qq`` / ``wechat`` 是预留实现，
接口与能力声明已经在位，接入时只需补 ``parse()`` / ``render()``。

设计约束（决定了接口长这样）：

* QQ 官方机器人**不支持流式**，一条回复只能整段发；
* 微信（公众号/企业微信）**不支持 @ 成员**，也拿不到全群消息；
* 两个平台都有单条字数上限，长回复必须切段。

所以引擎只产出 ``OutboundMessage``，由各渠道自己决定怎么落到传输层。
"""

from channels.base import (
    KIND_DONE,
    KIND_END,
    KIND_ERROR,
    KIND_START,
    KIND_SYSTEM,
    KIND_TEXT,
    KIND_USER,
    Channel,
    ChannelCapabilities,
    InboundMessage,
    OutboundMessage,
    event_to_outbound,
)
from channels.registry import CHANNELS, DEFAULT_CHANNEL, describe_all, enabled_channels, get

__all__ = [
    "KIND_DONE",
    "KIND_END",
    "KIND_ERROR",
    "KIND_START",
    "KIND_SYSTEM",
    "KIND_TEXT",
    "KIND_USER",
    "CHANNELS",
    "DEFAULT_CHANNEL",
    "Channel",
    "ChannelCapabilities",
    "InboundMessage",
    "OutboundMessage",
    "describe_all",
    "enabled_channels",
    "event_to_outbound",
    "get",
]