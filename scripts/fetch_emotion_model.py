"""准备情绪小模型（下载 → 导出 ONNX → 量化）。

模型：``IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment``（中文情感二分类，110M 参数）

## 为什么不用现成的 ONNX 仓库

试过 ``lxyuan/...-sentiments-student`` 自带的 ONNX：**输出退化**，任何句子都判同一类
（fp32 和 int8 都一样，不是量化的问题）。自己从 PyTorch 权重导出，并做一致性校验，
才能确认"导出来的图和原模型算的是一个东西"。

## 三步

```
1. 下载 PyTorch 权重（约 390MB）
2. torch.onnx.export 导出（fp32，约 390MB）
3. onnxruntime 动态量化到 int8（约 98MB，CPU 推理 ~9ms）
```

跑完会删掉 1、2 的中间产物，只留 ``onnx/model_quantized.onnx`` + tokenizer。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "backend" / "data" / "models" / "erlangshen-sentiment"
REPO = "IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment"

ALLOW = ["*.json", "*.txt", "*.bin", "*.safetensors", "*.model"]
IGNORE = ["*.h5", "*.msgpack", "onnx/*"]

#: 一致性校验用的句子（导出前后算出来的 logits 必须几乎相同）
PROBE_TEXT = "我今天真的很开心，谢谢你一直陪着我"


def _configure_env() -> None:
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HOME", str(ROOT / ".hfhome"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _download() -> Path:
    from huggingface_hub import snapshot_download

    weights = TARGET / "pytorch_model.bin"
    if not weights.exists():
        print(f"下载权重：{REPO}")
        snapshot_download(
            repo_id=REPO,
            local_dir=str(TARGET),
            allow_patterns=ALLOW,
            ignore_patterns=IGNORE,
            max_workers=4,
        )
    return weights


def _export() -> Path:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    onnx_dir = TARGET / "onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    out = onnx_dir / "model.onnx"
    if out.exists():
        print("ONNX 已存在，跳过导出")
        return out

    print("导出 ONNX...")
    tokenizer = AutoTokenizer.from_pretrained(str(TARGET))
    model = AutoModelForSequenceClassification.from_pretrained(str(TARGET)).eval()
    dummy = tokenizer("测试", truncation=True, max_length=16, return_tensors="pt")
    args = (dummy["input_ids"], dummy["attention_mask"], dummy["token_type_ids"])

    started = time.perf_counter()
    torch.onnx.export(
        model,
        args,
        str(out),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "token_type_ids": {0: "batch", 1: "sequence"},
            "logits": {0: "batch"},
        },
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,  # 用旧导出器：不需要额外的 onnxscript 依赖
    )
    print(f"导出完成，用时 {time.perf_counter() - started:.1f}s")

    # 一致性校验
    import onnxruntime as ort

    session = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    batch = tokenizer(PROBE_TEXT, truncation=True, max_length=128, return_tensors="pt")
    with torch.no_grad():
        torch_logits = model(**batch).logits[0].tolist()
    feeds = {key: batch[key].numpy() for key in ("input_ids", "attention_mask", "token_type_ids")}
    onnx_logits = session.run(None, feeds)[0][0].tolist()
    drift = max(abs(a - b) for a, b in zip(torch_logits, onnx_logits))
    print(f"一致性校验：torch 与 ONNX 最大误差 {drift:.6f}")
    if drift > 0.01:
        raise SystemExit(f"导出结果与 PyTorch 差异过大（{drift}），中止")
    return out


def _quantize(source: Path) -> Path:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    target = source.parent / "model_quantized.onnx"
    if target.exists():
        print("量化模型已存在，跳过")
        return target
    print("量化到 int8...")
    started = time.perf_counter()
    quantize_dynamic(str(source), str(target), weight_type=QuantType.QInt8)
    print(f"量化完成，用时 {time.perf_counter() - started:.1f}s")
    return target


def main() -> int:
    _configure_env()
    if (TARGET / "onnx" / "model_quantized.onnx").exists():
        size = (TARGET / "onnx" / "model_quantized.onnx").stat().st_size / 1024 / 1024
        print(f"已就绪（{size:.1f} MB）：{TARGET / 'onnx' / 'model_quantized.onnx'}")
        return 0

    try:
        _download()
        fp32 = _export()
        quantized = _quantize(fp32)
    except ModuleNotFoundError as exc:
        print(f"缺少依赖：{exc.name}。先跑  pip install torch transformers onnxruntime onnx", file=sys.stderr)
        return 1

    # 只留量化模型与 tokenizer，省 ~780MB
    for stale in (fp32, TARGET / "pytorch_model.bin", TARGET / "model.safetensors"):
        if stale.exists():
            stale.unlink()
            print(f"已清理中间产物：{stale.name}")

    size = quantized.stat().st_size / 1024 / 1024
    print(f"完成：{quantized} （{size:.1f} MB，int8）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())