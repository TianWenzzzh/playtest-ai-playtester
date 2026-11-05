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
]

POLICY_NAMES = ("random", "curiosity", "semantic", "llm", "llm_memory")


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

    # 按 (策略, 臂) 汇总
    print(f"{'策略':<12} {'召回':>10} {'误报':>10} {'回放全等':>10} {'平均步数':>8}")
    problems: list[str] = []
    for p in POLICY_NAMES:
        hits = 0
        planted = 0
        fps = 0
        clean = 0
        ok_replay = 0
        all_replay = 0
        steps: list[int] = []
        for (g, q, exp) in SUITE:
            for r in [x for x in rows if x["policy"] == p and x["game"] == g and x["query"] == q]:
                if exp is None:
                    clean += 1
                    if r["kind"]:
                        fps += 1
                        problems.append(f"{p} 在干净臂 {g}{q} seed={r['seed']} 误报 {r['kind']}")
                else:
                    planted += 1
                    if r["kind"] == exp:
                        hits += 1
                    elif r["kind"]:
                        problems.append(f"{p} 在 {g}{q} seed={r['seed']} 报成 {r['kind']}，期望 {exp}")
                    else:
                        problems.append(f"{p} 在 {g}{q} seed={r['seed']} 漏报（期望 {exp}）")
                if r["steps"]:
                    steps.append(r["steps"])
                if r["replayed"] is not None:
                    all_replay += 1
                    ok_replay += 1 if r["replayed"] else 0
        print(f"{p:<12} {hits}/{planted:>8} {fps}/{clean:>8} {ok_replay}/{all_replay:>8} "
              f"{(sum(steps)//len(steps)) if steps else 0:>8}")

    errs = [r for r in rows if r["error"]]
    if errs:
        problems.append(f"{len(errs)} 局运行出错，首例：{errs[0]['error']}")

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
