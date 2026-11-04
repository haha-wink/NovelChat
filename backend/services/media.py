"""上传图片校验。

原实现直接 ``base64.b64decode`` 后落盘，既不校验是不是图片，
也不限制大小/数量，且临时文件 ``delete=False`` 永不清理。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 魔数 -> 后缀
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class ImageValidationError(ValueError):
    """图片不合法（格式/大小/数量）。"""


@dataclass(frozen=True)
class DecodedImage:
    data: bytes
    suffix: str
    size: int


def _sniff_suffix(data: bytes) -> str | None:
    for magic, suffix in _SIGNATURES:
        if data.startswith(magic):
            return suffix
    # WEBP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _strip_data_url(value: str) -> str:
    text = value.strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    return text


def decode_images(
    items: Iterable[str] | None,
    *,
    max_count: int,
    max_image_bytes: int,
    max_total_bytes: int,
) -> list[DecodedImage]:
    """解码并校验 base64 图片列表。

    校验项：数量上限、单张大小、总大小、base64 合法性、真实图片格式（魔数）。
    """
    if not items:
        return []

    values: Sequence[str] = list(items)
    if len(values) > max_count:
        raise ImageValidationError(f"最多只能上传 {max_count} 张图片")

    decoded: list[DecodedImage] = []
    total = 0
    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, str) or not raw.strip():
            raise ImageValidationError(f"第 {index} 张图片内容为空")

        b64 = _strip_data_url(raw)
        # 先按 base64 长度预估，避免超大字符串直接进内存解码
        estimated = len(b64) * 3 // 4
        if estimated > max_image_bytes:
            raise ImageValidationError(
                f"第 {index} 张图片超过单张上限（{max_image_bytes // (1024 * 1024)}MB）"
            )

        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ImageValidationError(f"第 {index} 张图片不是合法的 base64 数据") from None

        if not data:
            raise ImageValidationError(f"第 {index} 张图片内容为空")
        if len(data) > max_image_bytes:
            raise ImageValidationError(
                f"第 {index} 张图片超过单张上限（{max_image_bytes // (1024 * 1024)}MB）"
            )

        suffix = _sniff_suffix(data)
        if suffix is None:
            raise ImageValidationError(
                f"第 {index} 个文件不是受支持的图片（仅支持 JPG/PNG/GIF/WEBP）"
            )

        total += len(data)
        if total > max_total_bytes:
            raise ImageValidationError(
                f"图片总大小超过上限（{max_total_bytes // (1024 * 1024)}MB）"
            )
        decoded.append(DecodedImage(data=data, suffix=suffix, size=len(data)))

    return decoded
