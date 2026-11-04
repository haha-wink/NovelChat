"""微信渠道（**预留，未接入**）。

## 三条不同的路，差别很大

| 方式 | 官方支持 | 能拿到的能力 |
|---|---|---|
| 公众号（订阅号/服务号） | ✅ | 只能被动回复用户消息，有 5 秒超时，且**不能用 @** |
| 企业微信（含微信群机器人） | ✅ | 群机器人可以发消息，但**不能读全群消息** |
| 个人微信 | ❌ 无官方 API | 只能靠 Hook（违反用户协议，有封号风险） |

所以接微信之前必须先确认走哪条路——这决定了 ``supports_mentions`` 和
"能不能主动开口"这两件事，而群聊引擎的选角逻辑正依赖它们。

## 接入时要做的事

1. ``parse()``：公众号是 XML 回调、企业微信是 JSON，都要验签；
2. ``render()``：被动回复必须**5 秒内**返回，超时要先回"正在想"，再异步补发；
3. 长文本切段（微信单条上限比 QQ 更小）；
4. 因为拿不到全群消息，群聊里"谁该接话"要退化成"只回 @ 我的人"。

最后一点是硬约束：微信个人号拿不到群消息流，做不到真正的多角色群聊。
"""

from __future__ import annotations

from typing import Any

from channels.base import Channel, ChannelCapabilities, InboundMessage, OutboundMessage

NOT_READY = (
    "微信渠道还没接入：需要先确定走公众号 / 企业微信 / 个人号 Hook 中的哪一条，"
    "再实现 channels/wechat.py 的 parse() 与 render()（详见该文件顶部注释）"
)


class WeChatChannel(Channel):
    name = "wechat"
    enabled = False

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name=self.name,
            #: 公众号/企业微信都无法像 QQ 群那样 @ 某个成员
            supports_mentions=False,
            supports_images=True,
            #: 只能用 emoji 字符，发不了平台表情包
            supports_stickers=True,
            supports_streaming=False,
            max_text_length=600,
            notes="公众号/企业微信；不支持 @、无流式、被动回复有 5s 超时",
        )

    def render(self, outbound: OutboundMessage) -> Any:
        raise NotImplementedError(NOT_READY)

    def parse(self, raw: dict[str, Any]) -> InboundMessage:
        raise NotImplementedError(NOT_READY)