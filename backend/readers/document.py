"""小说文档读取：txt / md / pdf。

PDF 用已安装的 pypdf；其余按编码回退解码（中文小说常见 GB18030/GBK）。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 支持的扩展名
SUPPORTED_SUFFIXES = (".txt", ".md", ".markdown", ".pdf")

#: 解码顺序：BOM -> UTF-8 -> 简体中文常见编码 -> 繁体
_DECODE_ORDER = ("utf-8-sig", "utf-8", "gb18030", "big5")


class DocumentError(ValueError):
    """文档无法解析（格式不支持 / 编码失败 / 扫描版 PDF）。"""


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode(data: bytes) -> str:
    for encoding in _DECODE_ORDER:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_plain_text(data: bytes) -> str:
    if not data.strip():
        raise DocumentError("文件内容是空的")
    text = _decode(data)
    # 二进制文件被误传成 .txt 时，会出现大量控制字符
    control = sum(1 for ch in text[:4000] if ord(ch) < 9 or (13 < ord(ch) < 32))
    if control > 400:
        raise DocumentError("这看起来不是文本文件，请上传 .txt / .md / .pdf")
    return _normalise(text)


def _read_pdf(data: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise DocumentError(f"PDF 解析失败：{exc}") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页失败不影响其它页
            continue

    text = _normalise("\n\n".join(p.strip() for p in pages if p.strip()))
    if len(text) < 20:
        raise DocumentError("PDF 里几乎提取不到文字（可能是扫描件/图片版），请改传 txt")
    return text


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """返回 (正文, 类型)。类型用于前端展示，如 txt / md / pdf。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentError(f"不支持的文件类型「{suffix or '未知'}」，仅支持 {' / '.join(SUPPORTED_SUFFIXES)}")
    if suffix == ".pdf":
        return _read_pdf(data), "pdf"
    if suffix in (".md", ".markdown"):
        return _read_plain_text(data), "md"
    return _read_plain_text(data), "txt"
