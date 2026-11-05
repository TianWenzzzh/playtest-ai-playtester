"""Spark 侧的实测脚本：把 deploy/dgx-spark/README.md §5 那张表填满。

单位选得刻意：`episodes_per_hour`（一小时能扫多少个游戏）才是这个产品真正的吞吐，
tok/s 只是它的零件。批量扫描是 7×24 的负载，正好对上官方给的使用场景。

跑法（在 Spark 上）：
    python deploy/dgx-spark/bench.py --games 20 --seeds 4 --tier A
"""
from __future__ import annotations

import argparse
import json
import pathlib
import platform
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from playtest.agent import run_episode, POLICIES
from playtest.env import shutdown_browser
from playtest.llm import LLMClient

ROOT = pathlib.Path(__file__).resolve().parent


def measure_decode(client: LLMClient, rounds: int) -> dict[str, float]:
    """直接量模型：一次长 prompt 的 prefill + 一次长输出的 decode。"""
    ttfts: list[float] = []
    tps: list[float] = []
    prompt = "请列举 200 个常见的游戏关卡缺陷类型，每条一行。" * 4
    for _ in range(rounds):
        before_tokens = client.stat.total_completion_tokens
        t0 = time.perf_counter()
        out = client.complete(
            [{"role": "user", "content": prompt + "\n现在开始输出。"}], force_json=False
        )
        dt = time.perf_counter() - t0
        if not out:
            continue
        produced = client.stat.total_completion_tokens - before_tokens
        # usage 里没给 token 数时退回 len/4 估算，并在 note 里说明口径
        approx = produced if produced > 0 else max(1, len(out) // 4)
        tps.append(approx / dt)
        ttfts.append(dt * 1000)  # 粗口径：整段时延，非严格 TTFT
    if not tps:
        return {"tok_per_sec": 0.0, "latency_ms_mean": 0.0, "samples": 0}
    return {
        "tok_per_sec": round(statistics.mean(tps), 1),
        "tok_per_sec_p95_low": round(sorted(tps)[max(0, int(len(tps) * 0.05))], 1),
        "latency_ms_mean": round(statistics.mean(ttfts), 1),
        "samples": len(tps),
        "note": "优先用 usage.completion_tokens；缺失时退回 len/4 估算",
    }


def measure_end_to_end(games: list[str], seeds: list[int], policy: str) -> dict[str, float]:
    """端到端：一小时能扫多少个游戏局。"""
    cls = POLICIES[policy]
    wall: list[int] = []
    defects = 0
    t0 = time.perf_counter()
    for g in games:
        for s in seeds:
            res = run_episode(g, s, cls(seed=s), max_steps=40)
            wall.append(res.wall_ms)
            defects += 1 if res.defect else 0
    total = time.perf_counter() - t0
    episodes = len(games) * len(seeds)
    return {
        "episodes": episodes,
        "episodes_per_hour": round(episodes / total * 3600, 1) if total else 0.0,
        "ms_per_episode_mean": round(statistics.mean(wall), 1) if wall else 0.0,
        "defects_found": defects,
    }


def mem_snapshot() -> dict[str, str]:
    """统一内存的水位只能从 /proc/meminfo 读，nvidia-smi 那一栏是 Not Supported。"""
    try:
        txt = pathlib.Path("/proc/meminfo").read_text()
    except OSError:
        return {"meminfo": "unavailable"}
    out = {}
    for line in txt.splitlines():
        key = line.split(":")[0]
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            out[key] = line.split()[1] + "kB"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="A")
    ap.add_argument("--games", nargs="*", default=[])
    ap.add_argument("--seeds", nargs="*", type=int, default=[20261105, 20261106])
    ap.add_argument("--policy", default="llm")
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()

    client = LLMClient()
    if not client.cfg.available():
        print(f"❌ 未配置推理端点：{client.cfg.reason_unavailable()}", file=sys.stderr)
        print("   先跑 deploy/dgx-spark/serve.sh，再 export PLAYTEST_BASE_URL/…", file=sys.stderr)
        return 2

    games = args.games or [
        (pathlib.Path(__file__).resolve().parent.parent.parent / "games" / g / "index.html").as_uri()
        for g in ("blocky-jump", "maze-exit", "clicker-quest")
    ]

    report = {
        "tier": args.tier,
        "model": client.cfg.model,
        "base_url": client.cfg.base_url,
        "arch": platform.machine() or "unknown",
        "memory": mem_snapshot(),
        "decode": measure_decode(client, args.rounds),
        "end_to_end": measure_end_to_end(games, args.seeds, args.policy),
        "calls": client.stat.as_dict(),
    }
    out = ROOT / f"measured-{args.tier}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n落盘：{out}\n把 decode 与 end_to_end 两块的数字抄进 README §5 与手记，"
          f"并标注测量日期与机器（Spark 节点 / 本地等价验证）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutdown_browser()
