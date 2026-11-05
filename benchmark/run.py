"""缺陷植入基准：每个夹具都有"植入臂"和"干净臂"两臂。

🔴 两臂缺一不可：
  只跑植入臂 ⇒ 得到召回率，但"见到什么都报缺陷"的仪器也能拿满分；
  只跑干净臂 ⇒ 得到误报率，但一个什么都不报的仪器也能拿满分。
必须两臂同时报，这张表才有分辨力。

跑法：python -m benchmark.run
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import POLICIES, run_episode, verify_pack
from playtest.env import shutdown_browser
from playtest.llm import LLMClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEDS = (20261105, 20261106, 20261107)

# (夹具目录, 查询串, 期望缺陷 kind 或 None 表示必须沉默)
SUITE: list[tuple[str, str, str | None]] = [
    ("blocky-jump", "?maxgap=6", "unwinnable"),
    ("blocky-jump", "?maxgap=3", None),
    ("maze-exit", "?plant=keywall", "softlock"),
    ("maze-exit", "?plant=none", None),
    ("clicker-quest", "?plant=deadend", "ui_dead_end"),
    ("clicker-quest", "?plant=none", None),
    # 🔴 分辨力夹具：缺陷在第 17 层，探测 horizon=12 从起点证不出，
    # 必须真把游戏打上去；而打上去要求连续 17 次读对中文线索。
    # 在这两个臂上，"基线漏报"是**期望行为**而不是失败 —— 它才是分辨力的证据。
    ("vault-run", "?plant=sealed", "unwinnable"),
    ("vault-run", "?plant=none", None),
]

# 🔴 正向对照按判据的瓶颈类型分派，不能用一个对照包打所有判据：
#   softlock / unwinnable 的瓶颈是"走得到那个深度" ⇒ 需要会走的对照（oracle）
#   ui_dead_end 的瓶颈是"每个控件都点一遍"          ⇒ 需要会扫的对照（sweep）
# 用错类会得到 0 开火，然后把它误读成"检测器有缺陷"。这是实测踩到的。
CONTROL_BY_KIND = {
    "unwinnable": "oracle",
    "softlock": "oracle",
    "ui_dead_end": "sweep",
    "crash": "sweep",
    "invariant_violation": "sweep",
}

# 只有能读语义的策略才被期望在这些夹具上开火
SEMANTIC_ONLY = {"vault-run"}
SEMANTIC_POLICIES = {"clue", "llm", "llm_memory"}

POLICY_NAMES = ("random", "curiosity", "semantic", "clue", "llm", "llm_memory")


def url_for(game: str, query: str) -> str:
    return (ROOT / "games" / game / "index.html").resolve().as_uri() + query


def run_one(game: str, query: str, policy_name: str, seed: int, client: LLMClient) -> dict[str, Any]:
    cls = POLICIES[policy_name]
    kw: dict[str, Any] = {"seed": seed}
    if policy_name in ("llm", "llm_memory"):
        kw.update({"client": client, "memory": policy_name == "llm_memory"})
    res = run_episode(url_for(game, query), seed, cls(**kw), max_steps=40)
    replayed = None
    if res.pack:
        replayed = verify_pack(url_for(game, query), res.pack)["replayed"]
    return {
        "game": game, "query": query, "policy": policy_name, "seed": seed,
        "kind": (res.defect or {}).get("kind"),
        "steps": res.steps, "states": res.distinct_states, "probes": res.probes,
        "replayed": replayed, "wall_ms": res.wall_ms, "error": res.error,
        "policy_trace": res.policy_trace or {},
    }


def main(argv: list[str] | None = None) -> int:
    client = LLMClient()
    if not client.cfg.available():
        print(f"⚠ LLM 端点不可用（{client.cfg.reason_unavailable()}）"
              f"⇒ llm / llm_memory 两列会退化成 curiosity，出表时必须带这个标注\n")

    rows = [run_one(g, q, p, s, client)
            for (g, q, _exp) in SUITE for p in POLICY_NAMES for s in SEEDS]

    out = ROOT / "out" / "benchmark.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"llm_available": client.cfg.available(),
                               "llm_note": None if client.cfg.available() else "llm 两列等价 curiosity",
                               "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 按 (策略, 臂) 汇总。两臂必须分开数，且分辨力夹具上的漏报是期望行为。
    print(f"{'策略':<12} {'召回(普通)':>12} {'召回(语义)':>12} {'误报':>10} {'回放全等':>10} {'平均步数':>8}")
    problems: list[str] = []
    open_findings: list[str] = []
    for p in POLICY_NAMES:
        hits_n = planted_n = hits_s = planted_s = fps = clean = 0
        ok_replay = all_replay = 0
        steps: list[int] = []
        for (g, q, exp) in SUITE:
            semantic_only = g in SEMANTIC_ONLY
            for r in [x for x in rows if x["policy"] == p and x["game"] == g and x["query"] == q]:
                if exp is None:
                    clean += 1
                    if r["kind"]:
                        fps += 1
                        problems.append(f"{p} 在干净臂 {g}{q} seed={r['seed']} 误报 {r['kind']}")
                else:
                    fired = r["kind"] == exp
                    if semantic_only:
                        planted_s += 1
                        hits_s += 1 if fired else 0
                        # 🔴 漏报不算闸门失败。oracle 已经证明检测器判得出来，
                        # 走不到第 17 层是**策略能力缺口** —— 那正是这张表要量出来的东西本身。
                        # 把它记进 open_findings 大声报出来，但不让闸门红，
                        # 否则"还没做到的事"会被读成"做坏了的事"。
                        if fired and p not in SEMANTIC_POLICIES:
                            problems.append(f"{p} 在分辨力夹具上开火了 —— 夹具可能太浅，失去分辨力")
                    else:
                        planted_n += 1
                        hits_n += 1 if fired else 0
                        if not fired:
                            problems.append(f"{p} 在 {g}{q} seed={r['seed']} 漏报（期望 {exp}）")
                if r["steps"]:
                    steps.append(r["steps"])
                if r["replayed"] is not None:
                    all_replay += 1
                    ok_replay += 1 if r["replayed"] else 0
        print(f"{p:<12} {hits_n}/{planted_n:>8} {hits_s}/{planted_s:>8} "
              f"{fps}/{clean:>8} {ok_replay}/{all_replay:>8} "
              f"{(sum(steps)//len(steps)) if steps else 0:>8}")
        if planted_s and hits_s == 0:
            tag = "（LLM 端点未配置，本列实际等价 curiosity）" if p in ("llm", "llm_memory") else ""
            open_findings.append(f"{p} 在分辨力夹具上 0/{planted_s}：走不到缺陷所在深度{tag}")

    errs = [r for r in rows if r["error"]]

    # 🔴 正向对照：按判据的瓶颈类型选对照策略，把"仪器判不出"和"策略走不到"分开量。
    # 实测教训：一开始只用 oracle 打天下，于是它在 ui_dead_end 上 0/3 ——
    # 恒选"最正确动作"的策略永远不会去点那个没接逻辑的按钮。
    # 那不是检测器有缺陷，是对照用错了类。
    print("\n检测器正向对照（不参与策略排名）：")
    for (g, q, exp) in SUITE:
        if exp is None:
            continue
        ctrl_name = CONTROL_BY_KIND.get(exp, "sweep")
        hit = 0
        for s in SEEDS:
            r = run_one(g, q, ctrl_name, s, client)
            if r["error"]:
                problems.append(f"{ctrl_name} 在 {g}{q} seed={s} 出错：{r['error']}")
            elif r["kind"] == exp:
                hit += 1
            else:
                problems.append(f"{ctrl_name} 在 {g}{q} seed={s} 没开火（期望 {exp}，实得 {r['kind']}）"
                                f"⇒ 这是**检测器的缺陷**，不是策略的")
        print(f"  {g}{q:<18} 对照={ctrl_name:<7} 开火 {hit}/{len(SEEDS)}")

    if errs:
        problems.append(f"{len(errs)} 局运行出错，首例：{errs[0]['error']}")

    print()
    if open_findings:
        print("🔴 未达成的目标（不是闸门失败，是这张表量出来的能力缺口）：")
        for x in dict.fromkeys(open_findings):
            print("  -", x)
        print("  ⇒ 含义：分辨力已经建立，缺陷藏在搜索视界之外，")
        print("     oracle 能走到并判出，其余策略走不到。LLM 臂的靶就立在这里。")
        print()
    if problems:
        print(f"基准未通过，{len(problems)} 条：")
        for x in problems[:20]:
            print("  -", x)
        print(f"\n明细见 {out}")
        return 1
    print(f"基准全部通过 · {len(rows)} 局 · 明细见 {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutdown_browser()
