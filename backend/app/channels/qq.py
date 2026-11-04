"""QQ 渠道（**预留，未接入**）。

写在这里的目的是把"接 QQ 需要改哪些地方"变成可执行的清单，而不是一句 TODO。

## 真正接入时要做的

1. 在 QQ 开放平台建机器人，拿到 ``AppID`` / ``AppSecret`` / ``Token``；
2. 实现 ``parse()``：QQ 回调是加密的 JSON，先验签解密，再映射成 ``InboundMessage``
   （群号 -> conversation_id，QQ 号 -> sender_id）；
3. 实现 ``render()``：QQ **不支持流式**，必须把 ``delta`` 累积起来，
   等 ``speaker_end`` 时整段发送；超过长度上限要切段（用 ``OutboundMessage.split()``）；
4. 配置回调地址 + 白名单 IP（QQ 要求公网可访问，本地开发要用内网穿透）；
5. ``enabled`` 改 True，并在 ``channels/registry.py`` 里注册启动时的回调校验。

## 引擎侧不用改

群聊选角、@ 唤醒、情绪、贴纸决策全部走 ``OutboundMessage``，
所以 QQ 只是多了一个"把 OutboundMessage 变成 HTTP 请求"的实现。
唯一要注意的是 ``supports_streaming=False`` 时前端那套逐字动画不适用。
"""

from __future__ import annotations

from typing import Any

from channels.base import Channel, ChannelCapabilities, InboundMessage, OutboundMessage

#: 未接入时的统一报错文案
NOT_READY = (
    "QQ 渠道还没接入：需要先在 QQ 开放平台创建机器人并配置 AppID/AppSecret，"
    "然后实现 channels/qq.py 的 parse() 与 render()（详见该文件顶部注释）"
)


class QQChannel(Channel):
    name = "qq"
    enabled = False

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name=self.name,
            supports_mentions=True,
            supports_images=True,
            supports_stickers=False,
            #: 官方机器人 API 是"一问一答"，没有 SSE 那样的逐字流
            supports_streaming=False,
            max_text_length=1000,
            notes="官方机器人 API；不支持流式与表情包，长回复需切段。上限以最新文档为准",
        )

    def render(self, outbound: OutboundMessage) -> Any:
        raise NotImplementedError(NOT_READY)

    def parse(self, raw: dict[str, Any]) -> InboundMessage:
        raise NotImplementedError(NOT_READY)