"""playtest 命令行。

三条命令对应三种用法：
  scan    给一个游戏 URL，扫出缺陷并落复现包
  replay  给一个复现包，校验它是否真的可复现（退出码 0/1，可以进 CI）
  bench   跑策略对比基准，出召回率/误报率表

`replay` 是这套东西能被别人复用的关键 —— 没有它，复现包只是一份好看的 JSON；
有了它，别人可以把自己的复现包丢进 CI，红了就拦下来。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from .agent import POLICIES, run_episode, sweep, verify_pack
from .env import shutdown_browser
from .llm import LLMClient, LLMConfig
from .trace import load_pack, save_pack


def _uri_or_url(target: str) -> str:
    # 先把 ?query 摘出来再判断是不是本地路径 —— 带查询串的本地夹具路径
    # 直接丢给 Path.exists() 会判假，于是被当成远程 URL 交给浏览器。
    path_part, sep, query = target.partition("?")
    p = pathlib.Path(path_part)
    if p.exists():
        if p.is_dir():
            p = p / "index.html"
        return p.resolve().as_uri() + (sep + query if sep else "")
    return target


def cmd_scan(args: argparse.Namespace) -> int:
    url = _uri_or_url(args.game)
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    cls = POLICIES.get(args.policy)
    if cls is None:
        print(f"未知策略 {args.policy}，可选：{', '.join(POLICIES)}", file=sys.stderr)
        return 2

    out_dir = pathlib.Path(args.out)
    report = sweep(url, seeds, lambda s: cls(seed=s), out_dir, max_steps=args.max_steps)
    hits = [e for e in report["episodes"] if e["defect"]]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n{len(seeds)} 局 · 开火 {len(hits)} · 回放通过 "
          f"{sum(1 for e in hits if e.get('replayed'))}", file=sys.stderr)
    return 0 if not args.fail_on_defect else (1 if hits else 0)


def cmd_replay(args: argparse.Namespace) -> int:
    pack = load_pack(pathlib.Path(args.pack))
    url = args.game or pack["game"]["url"]
    v = verify_pack(_uri_or_url(url), pack)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    if v["replayed"]:
        print("复现包有效：初始状态与全部动作帧哈希一致", file=sys.stderr)
        return 0
    print("复现包无效：存在哈希不一致，见 mismatch", file=sys.stderr)
    return 1


def cmd_bench(args: argparse.Namespace) -> int:
    games = [_uri_or_url(g) for g in args.game]
    policies = args.policies.split(",")
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    client = LLMClient()

    rows: list[dict[str, Any]] = []
    for g in games:
        for pol in policies:
            cls = POLICIES[pol]
            for s in seeds:
                kw: dict[str, Any] = {"seed": s}
                if pol in ("llm", "llm_memory"):
                    kw["client"] = client
                    kw["memory"] = pol == "llm_memory"
                res = run_episode(g, s, cls(**kw), max_steps=args.max_steps)
                rows.append({
                    "game": g.rsplit("/", 2)[-2] if "/" in g else g,
                    "policy": pol, "seed": s,
                    "defect": (res.defect or {}).get("kind"),
                    "steps": res.steps, "states": res.distinct_states,
                    "replayed": (verify_pack(g, res.pack)["replayed"] if res.pack else None),
                    "wall_ms": res.wall_ms, "error": res.error,
                })

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"LLM 可用: {client.cfg.available()}  模型: {client.cfg.model}")
    if not client.cfg.available():
        print(f"  ⚠ {client.cfg.reason_unavailable()} —— llm 两列实际等价于 curiosity，出表时必须标注")
    print(f"  调用统计: {json.dumps(client.stat.as_dict(), ensure_ascii=False)}")
    print(f"\n共 {len(rows)} 局，明细见 {out / 'bench.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="playtest", description="AI 试玩员：自动试玩 / 确定性复现 / 修复闭环")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描一个游戏，产出复现包")
    s.add_argument("game")
    s.add_argument("--seeds", default="20261105,20261106,20261107")
    s.add_argument("--policy", default="curiosity", choices=sorted(POLICIES))
    s.add_argument("--max-steps", type=int, default=60)
    s.add_argument("--out", default="out/scan")
    s.add_argument("--fail-on-defect", action="store_true", help="发现缺陷时退出码为 1（CI 用）")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("replay", help="校验一个复现包是否真的可复现")
    r.add_argument("pack")
    r.add_argument("--game", default=None, help="覆盖包里的 URL（本地重放时用）")
    r.set_defaults(fn=cmd_replay)

    b = sub.add_parser("bench", help="跑策略对比基准")
    b.add_argument("--game", action="append", required=True)
    b.add_argument("--policies", default="random,curiosity,semantic,llm,llm_memory")
    b.add_argument("--seeds", default="20261105,20261106")
    b.add_argument("--max-steps", type=int, default=60)
    b.add_argument("--out", default="out/bench")
    b.set_defaults(fn=cmd_bench)

    ns = ap.parse_args(argv)
    try:
        return ns.fn(ns)
    finally:
        shutdown_browser()


if __name__ == "__main__":
    raise SystemExit(main())
