"""基准表里"零命中该怪谁"这一行的归因判据 —— 纯逻辑，不开浏览器。

为什么单独一条：`benchmark/run.py` 原先在 llm 两列 0 命中时**按策略名**硬写
"LLM 端点未配置，本列实际等价 curiosity"。今天端点确实不在，所以这句话碰巧为真，
没人会发现它是错的；等真端点配上以后，若这两列仍 0 命中，真因可能是模型给出了
非法动作（`invalid_action`）或预算用完（`decision_cap`），而表里照样说"没配端点"
—— 那既是一句假话，又把真因盖掉了。

所以这条判据要的是：**归因只能来自这一列自己读到的降级计数**。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m6_benchmark_note.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from benchmark.run import DEGRADATION_GLOSS, _zero_hit_note  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    print("\n== 1. 归因跟着读数走，不跟着策略名走 ==")
    n = _zero_hit_note(["endpoint_unavailable"] * 24)
    check("端点不在时仍然说得出『未配置』，并带局数", "端点未配置" in n and "24" in n, n)

    bad = _zero_hit_note(["invalid_action"] * 24)
    check("🔴 端点在但动作被合法性挡下时，不许写成『未配置』",
          "未配置" not in bad and DEGRADATION_GLOSS["invalid_action"] in bad and "24" in bad, bad)

    cap = _zero_hit_note(["decision_cap"] * 20 + ["invalid_action"] * 4)
    check("预算用完与非法动作分开计，各自的局数都在场",
          "decision_cap" not in cap and "invalid_action" not in cap
          and "20" in cap and "4" in cap
          and DEGRADATION_GLOSS["decision_cap"] in cap
          and DEGRADATION_GLOSS["invalid_action"] in cap, cap)

    mixed = _zero_hit_note(["endpoint_unavailable", "invalid_action"])
    check("两种成因同时出现时不许只报一种",
          "端点未配置" in mixed and DEGRADATION_GLOSS["invalid_action"] in mixed, mixed)

    check("非 LLM 列不产出降级读数 ⇒ 这一行保持沉默",
          _zero_hit_note([]) == "" and _zero_hit_note([None, None]) == "",
          repr(_zero_hit_note([None, None])))

    real = _zero_hit_note(["none"] * 24)
    check("端点在且零降级时，这句话不许替失败找台阶",
          "未配置" not in real and "等价 curiosity" not in real, real)

    print("\n== 2. 接线：这条归因真的在出表那一步被调用 ==")
    src = (ROOT / "benchmark" / "run.py").read_text(encoding="utf-8")
    body = src.split("def _zero_hit_note", 1)[-1]
    called = body.count("_zero_hit_note(")
    check("定义之外恰好一处调用点（多了是复制，零了是装饰）", called == 1, f"calls={called}")
    check("旧的按策略名硬写已经不在源码里",
          'tag = "（LLM 端点未配置' not in src and "本列实际等价 curiosity）\" if p in" not in src,
          "")
    check("调用点带的是这一列自己的降级读数",
          "policy_trace" in body.split("_zero_hit_note(", 1)[-1][:200],
          body.split("_zero_hit_note(", 1)[-1][:150].replace("\n", " "))

    failed = [x for x in results if not x[1]]
    print(f"\n{'=' * 46}\n{len(results) - len(failed)}/{len(results)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:160]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
