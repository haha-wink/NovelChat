"""能力探测：判断某个技能在这台机器上能不能跑。

技能在 `SKILL.md` 里声明依赖：

```yaml
requires: [node, ffmpeg, chrome]
```

注册表用它决定「这个技能现在可不可用」，`/api/health` 用它解释为什么不可用。

这里只做**探测**，不做安装——缺什么就如实报什么。
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path
from typing import Optional

#: 已知的依赖名 -> 人类可读的说明（缺了时给用户看）
DEPENDENCY_HINTS = {
    "node": "Node.js（跑渲染脚本）",
    "ffmpeg": "ffmpeg（视频编码）",
    "chrome": "无头 Chrome（逐帧渲染）",
    "numpy": "numpy（音频分析）",
}


def _which(*names: str) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_chrome() -> Optional[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    return None


@lru_cache(maxsize=1)
def _probe() -> dict[str, bool]:
    """探测一次，结果缓存（进程内不再重复扫）。"""
    try:
        import numpy  # noqa: F401

        has_numpy = True
    except Exception:  # noqa: BLE001
        has_numpy = False

    return {
        "node": bool(_which("node")),
        "ffmpeg": bool(_which("ffmpeg")),
        "chrome": bool(find_chrome()),
        "numpy": has_numpy,
        # 纯 Python 能力，永远可用
        "python": True,
    }


def refresh() -> None:
    """装了新依赖后调用，让缓存失效。"""
    _probe.cache_clear()


def find_bundled(name: str, package_dir: Optional[Path]) -> Optional[str]:
    """在技能包的 bin/ 里找依赖（自包含包用）。"""
    if package_dir is None:
        return None
    bin_dir = Path(package_dir) / "bin"
    if not bin_dir.exists():
        return None
    candidates = [
        bin_dir / name,
        bin_dir / f"{name}.exe",
        bin_dir / name / name,
        bin_dir / name / f"{name}.exe",
        bin_dir / f"{name}-linux64" / name,
    ]
    # Chrome 有多种发行形态：完整版 chrome / headless-shell
    if name == "chrome":
        candidates += [
            bin_dir / "chrome" / "chrome-headless-shell",
            bin_dir / "chrome" / "chrome-headless-shell.exe",
            bin_dir / "chrome-headless-shell",
            bin_dir / "chrome-headless-shell.exe",
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # 兜底：bin/chrome*/ 里任何名字带 chrome 的可执行文件
    for folder in bin_dir.glob("chrome*"):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if item.is_file() and "chrome" in item.name.lower():
                return str(item)
    return None


def check(
    requires: list[str] | tuple[str, ...],
    *,
    package_dir: Optional[Path] = None,
) -> tuple[bool, list[str]]:
    """检查一组依赖是否齐全。返回 (是否齐全, 缺哪些)。

    ``package_dir`` 给了就先看这个技能包自带的 ``bin/``——支持"依赖放包里"。
    """
    if not requires:
        return True, []
    probed = _probe()
    missing = []
    for name in requires:
        if find_bundled(name, package_dir):
            continue
        if not probed.get(name, False):
            missing.append(name)
    return (not missing), missing


def describe() -> dict:
    probed = dict(_probe())
    return {
        "available": {name: ok for name, ok in probed.items()},
        "hints": DEPENDENCY_HINTS,
    }