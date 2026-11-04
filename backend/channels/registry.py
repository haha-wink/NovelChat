"""渠道注册表：按名字取渠道路由。

接新平台时只改这里 + 新增一个 adapter 文件，别处不用动。
"""

from __future__ import annotations

from typing import Optional

from channels.base import Channel
from channels.qq import QQChannel
from channels.wechat import WeChatChannel
from channels.web import channel as web_channel

CHANNELS: dict[str, Channel] = {
    web_channel.name: web_channel,
    QQChannel.name: QQChannel(),
    WeChatChannel.name: WeChatChannel(),
}

#: 默认渠道（web 端）
DEFAULT_CHANNEL = web_channel.name


def get(name: Optional[str]) -> Channel:
    """取渠道；未知渠道抛 KeyError（调用方转 400）。"""
    key = (name or DEFAULT_CHANNEL).strip().lower()
    if key not in CHANNELS:
        raise KeyError(f"未知渠道：{name}")
    return CHANNELS[key]


def describe_all() -> list[dict]:
    """/api/channels 的数据源。"""
    return [channel.describe() for channel in CHANNELS.values()]


def enabled_channels() -> list[str]:
    return [name for name, channel in CHANNELS.items() if channel.enabled]