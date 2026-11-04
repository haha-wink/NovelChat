"""下载 RAG 记忆库用的嵌入模型（bge-small-zh-v1.5，约 92 MB）。

**模型不进 git**，新机器上跑一次这个脚本即可：

    pwsh scripts/fetch_model.ps1
    # 或
    python scripts/fetch_model.py

走的镜像与坑：

* ``huggingface.co`` 在部分网络下连不通，用 ``hf-mirror.com``；
* python 进程可能是 LOW 完整性等级，写不了 ``%USERPROFILE%\\.cache``，
  所以 HF 缓存目录挪到项目内，并关掉 xet（它会额外往用户目录写日志）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "backend" / "data" / "models" / "bge-small-zh-v1.5"
REPO = "BAAI/bge-small-zh-v1.5"

ALLOW = ["*.json", "*.txt", "*.safetensors", "*.bin", "*.model"]
IGNORE = ["onnx/*", "openvino/*", "*.onnx", "*.h5", "*.msgpack", "*.ot", "imgs/*"]


def _configure_env() -> None:
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HOME", str(ROOT / ".hfhome"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def main() -> int:
    _configure_env()
    if (TARGET / "config.json").exists() and any(TARGET.glob("*.safetensors")):
        print(f"模型已存在，跳过下载：{TARGET}")
        return 0

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺少依赖：先跑  pip install torch transformers", file=sys.stderr)
        return 1

    print(f"镜像：{os.environ['HF_ENDPOINT']}")
    print(f"目标：{TARGET}")
    started = time.time()
    snapshot_download(
        repo_id=REPO,
        local_dir=str(TARGET),
        allow_patterns=ALLOW,
        ignore_patterns=IGNORE,
        max_workers=4,
    )

    # 两种权重格式会重复下载，只留 safetensors，能省一半空间
    redundant = TARGET / "pytorch_model.bin"
    if redundant.exists() and any(TARGET.glob("*.safetensors")):
        redundant.unlink()

    total = sum(item.stat().st_size for item in TARGET.rglob("*") if item.is_file())
    print(f"完成，用时 {time.time() - started:.1f}s，合计 {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())