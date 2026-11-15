#!/usr/bin/env python3
"""把 animation 技能需要的外部依赖下载进**技能包自己的 bin/ 目录**。

用法：

```bash
python scripts/fetch_animation_deps.py              # 下载 ffmpeg + Chrome
python scripts/fetch_animation_deps.py --dry-run    # 只看要下什么、多大
python scripts/fetch_animation_deps.py --force      # 已存在也重下
python scripts/fetch_animation_deps.py --ffmpeg-only
```

下载目标：

```
backend/app/skills/work/animation/bin/
├── ffmpeg.exe        （Windows）/ ffmpeg（Linux/macOS）
└── chrome/
    └── chrome.exe
```

放进去之后，这个技能包就**完全自包含**：拷到别的机器也能跑，
不需要改 PATH，也不需要装到系统目录。

为什么要"放进包里"而不是装到系统：用户要求的是可插拔技能包——
依赖跟着包走，删掉包就等于删掉能力，不会在系统里留垃圾。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

#: 技能包位置（本脚本从仓库根跑）
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "backend" / "app" / "skills" / "work" / "animation"
BIN_DIR = PACKAGE_DIR / "bin"

USER_AGENT = "NovelChat-fetch-deps/1.0 (+https://github.com)"

#: ffmpeg 官方 Windows 构建（gyan.dev，essentials 版够用）
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
#: Linux / macOS 用 BtbN 的 GitHub 构建
FFMPEG_LINUX_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)
FFMPEG_MACOS_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-osx64-gpl.tar.xz"
)

CHROME_KNOWN_GOOD = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win64"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def download(url: str, target: Path, *, show_progress: bool = True) -> Path:
    """流式下载，带进度。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if show_progress and total:
                    percent = done * 100 / total
                    sys.stdout.write(
                        f"\r    下载中 {percent:5.1f}%  "
                        f"{human(done)} / {human(total)}   "
                    )
                    sys.stdout.flush()
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
    return target


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def fetch_ffmpeg(*, force: bool, dry_run: bool) -> bool:
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    target = BIN_DIR / exe_name

    if target.exists() and not force:
        print(f"  ffmpeg 已存在：{target.relative_to(REPO_ROOT)}")
        return True

    platform = platform_key()
    if platform == "win64":
        url, kind = FFMPEG_WIN_URL, "zip"
    elif platform == "mac":
        url, kind = FFMPEG_MACOS_URL, "tar.xz"
    else:
        url, kind = FFMPEG_LINUX_URL, "tar.xz"

    print(f"  ffmpeg 来源：{url}")
    if dry_run:
        print(f"    （dry-run）会下载并解出 {exe_name} 到 bin/")
        return True

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        archive = temp / ("ffmpeg.zip" if kind == "zip" else "ffmpeg.tar.xz")
        download(url, archive)

        if kind == "zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.namelist():
                    if member.endswith(f"bin/{exe_name}") or member.endswith(exe_name):
                        with bundle.open(member) as source, open(target, "wb") as sink:
                            shutil.copyfileobj(source, sink)
                        break
        else:
            # tar.xz：用系统 tar（Windows 10+ 自带）
            subprocess.run(
                ["tar", "-xf", str(archive), "-C", str(temp)], check=True
            )
            found = next(temp.rglob(exe_name), None)
            if found:
                shutil.copy2(found, target)

        if not target.exists():
            print("    ✗ 压缩包里没找到 ffmpeg 可执行文件")
            return False

    if os.name != "nt":
        target.chmod(0o755)

    version = _run_version([str(target), "-version"])
    print(f"    ✓ {target.relative_to(REPO_ROOT)}  {version}")
    return True


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

def chrome_download_url(platform: str, *, full: bool = False) -> tuple[str, str]:
    """从 Chrome for Testing 版本清单里取下载地址。

    默认用 ``chrome-headless-shell``：120MB，无头渲染够用，比全量 Chrome 小 80MB。
    ``--full-chrome`` 时用完整版。
    """
    key = "chrome" if full else "chrome-headless-shell"
    request = urllib.request.Request(CHROME_KNOWN_GOOD, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    stable = data["channels"]["Stable"]
    version = stable["version"]
    for item in stable["downloads"][key]:
        if item["platform"] == platform:
            return item["url"], version
    raise RuntimeError(f"没有 {platform} 的 {key} 构建")


def fetch_chrome(*, force: bool, dry_run: bool, full: bool = False) -> bool:
    platform = platform_key()
    exe_name = "chrome.exe" if os.name == "nt" else "chrome"
    target_dir = BIN_DIR / "chrome"
    target = target_dir / exe_name

    if target.exists() and not force:
        print(f"  Chrome 已存在：{target.relative_to(REPO_ROOT)}")
        return True

    try:
        url, version = chrome_download_url(platform, full=full)
    except Exception as exc:  # noqa: BLE001 - 网络失败要能看懂
        print(f"    ✗ 取 Chrome 版本清单失败：{exc}")
        return False

    label = "Chrome 完整版" if full else "Chrome headless-shell"
    print(f"  {label} {version}（{platform}）：{url}")
    if dry_run:
        print(f"    （dry-run）会解压到 bin/chrome/")
        return True

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        archive = temp / "chrome.zip"
        download(url, archive)

        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)

        # headless-shell 的可执行文件叫 chrome-headless-shell.exe，
        # 不能只按 chrome.exe 找——按"名字里带 chrome 的可执行文件"认
        extracted = None
        for pattern in ("chrome*.exe", "chrome-headless-shell*", "chrome"):
            for path in temp.rglob(pattern):
                if path.is_file() and "chrome" in path.name.lower():
                    extracted = path
                    break
            if extracted is not None:
                break
        if extracted is None:
            print("    ✗ 压缩包里没找到 chrome 可执行文件")
            return False
        print(f"    解出：{extracted.name}")

        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(extracted.parent, target_dir)

    # 实际落盘的可执行文件名可能是 chrome-headless-shell.exe，重新定位一次
    actual = next(
        (item for item in target_dir.iterdir()
         if item.is_file() and item.name.lower().endswith(".exe") and "chrome" in item.name.lower()),
        None,
    ) or next(
        (item for item in target_dir.iterdir()
         if item.is_file() and item.name == "chrome"),
        target,
    )
    if os.name != "nt":
        actual.chmod(0o755)

    version_text = _run_version([str(actual), "--version"])
    print(f"    ✓ {actual.relative_to(REPO_ROOT)}  {version_text}")
    return True


def _run_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        first = (result.stdout or result.stderr or "").strip().splitlines()
        return first[0][:60] if first else "（无输出）"
    except Exception as exc:  # noqa: BLE001
        return f"（无法执行：{exc}）"


def main() -> int:
    parser = argparse.ArgumentParser(description="把动画技能的依赖下载进技能包 bin/")
    parser.add_argument("--dry-run", action="store_true", help="只看要下载什么")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--ffmpeg-only", action="store_true")
    parser.add_argument("--chrome-only", action="store_true")
    parser.add_argument(
        "--full-chrome", action="store_true",
        help="下完整版 Chrome（200MB），默认用 headless-shell（120MB）",
    )
    args = parser.parse_args()

    print(f"技能包：{PACKAGE_DIR.relative_to(REPO_ROOT)}")
    print(f"安装到：{BIN_DIR.relative_to(REPO_ROOT)}")
    print(f"平台  ：{platform_key()}\n")

    ok = True
    if not args.chrome_only:
        ok = fetch_ffmpeg(force=args.force, dry_run=args.dry_run) and ok
    if not args.ffmpeg_only:
        ok = fetch_chrome(
            force=args.force, dry_run=args.dry_run, full=args.full_chrome
        ) and ok

    print()
    if not ok:
        print("有依赖没装成功。装好后可以手动放到：")
        print(f"  {BIN_DIR}")
        return 1

    if args.dry_run:
        print("dry-run 结束，没有真正下载。")
        return 0

    print("完成。验证一下：")
    print("  GET /api/animation/capabilities      （或群信息页点「体检」）")
    print("  期望：available=true, missing=[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())