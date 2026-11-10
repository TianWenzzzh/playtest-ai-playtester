"""基准表里"零命中该怪谁"这一行的归因判据 —— 纯逻辑，不开浏览器。

为什么单独一条：`benchmark/run.py` 原先在 llm 两列 0 命中时**按策略名**硬写
"LLM 端点未配置，本列实际等价 curiosity"。今天端点确实不在，所以这句话碰巧为真，
没人会发现它是错的；等真端点配上以后，若这两列仍 0 命中，真因可能是模型给出了
非法动作（`invalid_action`）或预算用完（`decision_cap`），而表里照样说"没配端点"
—— 那既是一句假话，又把真因盖掉了。

所以这条判据要的是：**归因只能来自这一列自己读到的降级计数**。

§3 另守回放整表闸门：复现包回放不全等＝确定性证据失效，必须判红（区别于能力缺口的大声报）。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m6_benchmark_note.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import benchmark.run as bench  # noqa: E402
from benchmark.run import DEGRADATION_GLOSS, _zero_hit_note  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


# ---- §3 合成行：只隔离回放闸门，不代表任何真实策略表现 ----
_EXP_BY_ARM = {(g, q): e for g, q, e in bench.SUITE}


def _passing_kind(game: str, query: str, policy: str) -> str | None:
    """构造一个除回放外处处通过的 kind，好让 rc 只由回放闸门决定。"""
    exp = _EXP_BY_ARM[(game, query)]
    if exp is None:
        return None
    if game in bench.SEMANTIC_ONLY:
        return exp if policy in bench.SEMANTIC_POLICIES else None
    expected = bench.EXPECTED_FINDERS.get((game, query), set(bench.POLICY_NAMES))
    return exp if policy in expected else None


def _make_run_one(fail_target: tuple | None):
    """替掉浏览器层 run_one；verdict 逻辑（bench.main）保持真实。

    fail_target 命中的那一行 kind 仍开火（ ⇒ 真产出了复现包），但 replayed=False，
    正是「包产出了却回放不全等」这条打破立身之本的负控。
    """
    def fake(game, query, policy, seed, client):
        kind = _EXP_BY_ARM[(game, query)] if policy in ("oracle", "sweep") \
            else _passing_kind(game, query, policy)
        replayed = True if kind is not None else None
        if fail_target == (game, query, policy, seed):
            replayed = False
        return {"game": game, "query": query, "policy": policy, "seed": seed,
                "kind": kind, "steps": 1, "states": 1, "probes": 1,
                "replayed": replayed, "wall_ms": 0, "error": None, "policy_trace": {}}
    return fake


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

    print("\n== 3. 回放整表闸门：复现包回放不全等必须判红（证据失效，不是能力缺口）==")
    fail_target = ("blocky-jump", "?maxgap=6", "random", bench.SEEDS[0])
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        with mock.patch.object(bench, "run_one", _make_run_one(fail_target)), \
             mock.patch.object(bench, "ROOT", tmp):
            rc_fail = bench.main()
        check("🔴 一枚复现包回放不全等 ⇒ 基准 rc=1（print-only 会漏成 rc=0）",
              rc_fail == 1, f"rc={rc_fail}")
        with mock.patch.object(bench, "run_one", _make_run_one(None)), \
             mock.patch.object(bench, "ROOT", tmp):
            rc_pass = bench.main()
        check("正对照：全部回放全等/无包 ⇒ 基准 rc=0（防恒红）",
              rc_pass == 0, f"rc={rc_pass}")

    failed = [x for x in results if not x[1]]
    print(f"\n{'=' * 46}\n{len(results) - len(failed)}/{len(results)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:160]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
