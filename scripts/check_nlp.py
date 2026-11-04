"""检查/演示人名识别引擎：装没装 jieba / HanLP，装不上怎么装。

用法：
    python scripts\\check_nlp.py            # 体检 + 打印安装命令 + 跑一段样例
    python scripts\\check_nlp.py 小说.txt    # 顺手用你的小说试一下扫描效果
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from services import name_engine  # noqa: E402

SAMPLE = (
    "路明非推开卡塞尔学院的门，看见恺撒·加图索正和楚子航说话。"
    "古德里安教授在走廊里咳嗽了一声，曼施坦因抱着一摞书从旁边过去。"
    "冯·施耐德站在窗边，诺玛·劳恩斯的声音从广播里传出来。"
    "老周在厨房里喊：谁要加面？沈砚没有说话，苏晚笑了笑。"
)

INSTALL_STEPS = """
================== 安装说明（三选一） ==================

【方案 A：jieba（推荐，纯 Python、无需编译、约 19MB 词典）】
    python -m pip install jieba
    国内镜像（更快）：
    python -m pip install jieba -i https://pypi.tuna.tsinghua.edu.cn/simple
    离线安装（没网时）：
      1) 在能上网的机器上下载 wheel：
         python -m pip download jieba --no-deps -d wheels
      2) 把 wheels\\jieba-*.whl 拷到本机，然后：
         python -m pip install wheels\\jieba-*.whl

【方案 B：HanLP（效果更好，但要下模型，约几百 MB）】
    python -m pip install hanlp
    # HanLP 2.x 依赖 torch；首次调用会自动下载模型，需要能连外网
    # 不想折腾 torch 就别选这个，jieba 足够用

【方案 C：pkuseg（不推荐，Python 3.13 上基本装不上）】
    pkuseg 需要编译 + 老版本 numpy，且没有 3.13 的预编译包；当前环境是 3.13，
    建议直接用 jieba。真要用就建一个 3.9~3.11 的虚拟环境：
    python -m pip install pkuseg

【验证】
    python scripts\\check_nlp.py
    看到 "backend: jieba" 就成功了；显示 builtin 说明还在用内置规则兜底。

【切换引擎】backend/.env
    NAME_ENGINE=auto      # 默认：先 jieba，再 HanLP，都没有就用内置规则
    NAME_ENGINE=jieba     # 只用 jieba（没装会提示）
    NAME_ENGINE=hanlp     # 只用 HanLP
    NAME_ENGINE=builtin   # 只用内置规则
========================================================
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    engine = name_engine.get_engine()
    info = engine.info()

    print("=== 当前引擎 ===")
    for key, value in info.items():
        print(f"  {key}: {value}")

    print("\n=== 各后端探测 ===")
    for backend in (name_engine.JiebaBackend(), name_engine.HanLPBackend()):
        detail = backend.detail()
        mark = "OK" if detail.get("available") else "MISSING"
        print(f"  {detail['backend']:<8} {mark:<8} {detail.get('error', '')}")

    if not engine.is_nlp:
        print(INSTALL_STEPS)
        return 1

    text = SAMPLE
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")[:200000]
            print(f"\n=== 用 {path.name} 扫描（前 20 万字）===")
        else:
            print(f"\n找不到文件 {path}，改用内置样例")
    else:
        print("\n=== 内置样例 ===")

    result = engine.detect(text, limit=20)
    print(f"引擎：{result['engine']}｜认出 {result.get('total', 0)} 个名字")
    for item in result.get("names") or []:
        print(f"  {item['name']:<10} {item['kind']:<8} 次数={item['count']:<4} tag={item['tag']}")

    print("\n=== 单个判定 ===")
    for name in ("路明非", "恺撒·加图索", "冯·施耐德", "掌门", "了一声"):
        verdict = engine.classify(name)
        print(
            f"  {name:<10} {verdict['kind']:<8} score={verdict['score']:<5} "
            f"tag={verdict.get('engine_tag') or '-'} "
            f"{'✓' if verdict['plausible'] else '✗'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())