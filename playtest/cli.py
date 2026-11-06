"""playtest 命令行。

四条命令对应四种用法：
  scan    给一个游戏 URL，扫出缺陷并落复现包
  replay  给一个复现包，校验它是否真的可复现（退出码 0/1，可以进 CI）
  bench   跑策略对比基准，出召回率/误报率表
  capture 把一个线上游戏原样取成一个 .har 离线包，之后 scan/replay 加 --har 就零联网重放

`replay` 是这套东西能被别人复用的关键 —— 没有它，复现包只是一份好看的 JSON；
有了它，别人可以把自己的复现包丢进 CI，红了就拦下来。
`--har` 是它的另一半：异步应用的复现等级受网络时序限制，
取包之后同一份字节从磁盘回答，位级复现才有机会成立。
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
from .offline import OfflineMirror, capture, harvest_episodes, summarize, top_up, warm_clicks
from .trace import load_pack, save_pack


def _mirror(path: str | None) -> OfflineMirror | None:
    if not path:
        return None
    p = pathlib.Path(path)
    if not p.exists():
        print(f"--har 指向的文件不存在：{p}（先跑 playtest capture）", file=sys.stderr)
        raise SystemExit(2)
    return OfflineMirror(p)


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
    mirror = _mirror(args.har)
    report = sweep(url, seeds, lambda s: cls(seed=s), out_dir,
                   max_steps=args.max_steps, offline=mirror)
    hits = [e for e in report["episodes"] if e["defect"]]
    if mirror is not None:
        report["mirror"] = mirror.report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n{len(seeds)} 局 · 开火 {len(hits)} · 回放通过 "
          f"{sum(1 for e in hits if e.get('replayed'))}", file=sys.stderr)
    for note in dict.fromkeys(e.get("render_note") or "" for e in report["episodes"]):
        if note:
            # 🔴 软件渲染下"画面没动"是仪器的环境，不是游戏的缺陷 —— 报警必须出现在读数旁边，
            # 而不是藏在代码注释里等人想起来。
            print(f"渲染后端：{note}", file=sys.stderr)
    if mirror is not None:
        m = mirror.report()
        print(f"离线镜像：命中 {m['served']}（{m['served_by']}）· 未命中 {m['miss_calls']}",
              file=sys.stderr)
    return 0 if not args.fail_on_defect else (1 if hits else 0)


def cmd_replay(args: argparse.Namespace) -> int:
    pack = load_pack(pathlib.Path(args.pack))
    url = args.game or pack["game"]["url"]
    v = verify_pack(_uri_or_url(url), pack, offline=_mirror(args.har))
    print(json.dumps(v, ensure_ascii=False, indent=2))
    if v["replayed"]:
        print("复现包有效：初始状态与全部动作帧哈希一致", file=sys.stderr)
        return 0
    print("复现包无效：存在哈希不一致，见 mismatch", file=sys.stderr)
    return 1


def cmd_capture(args: argparse.Namespace) -> int:
    """把一个线上游戏取成一个 .har —— 之后所有读数都从磁盘回答，不再受网络时序影响。"""
    if args.top_up:
        # 补包：已知字节从磁盘给，未命中放行去线上取，顺便把走过的深层界面录回来
        info = top_up(args.url, args.har)
        if args.episodes:
            # 点击热身走不到的地方，让真实探索自己把缺的分块请出来
            seeds = [int(s.strip()) for s in args.episodes.split(",") if s.strip()]
            info["episodes"] = harvest_episodes(args.url, args.har, seeds=seeds,
                                                max_steps=args.max_steps)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        after = info.get("episodes", {}).get("after", info["after"])
        print(f"\n补包完成：条目 {info['before']['entries']} → {after['entries']}；"
              f"新放行 {info['filled']['passthrough']} 次", file=sys.stderr)
        return 0
    # 热身只在应用自己的 host 里点：平台壳上的"登录""点赞"不该被自动化碰到
    origin = "/".join(args.url.split("/")[:3])
    info = capture(args.url, args.har, settle_ms=args.settle_ms,
                   warm=(lambda p: warm_clicks(p, host=origin)) if args.warm_clicks else None)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"\n已取包：{args.har}（{info['entries']} 条 / {info['bytes']} B）\n"
          f"下一步：playtest scan {args.url} --har {args.har}", file=sys.stderr)
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    games = [_uri_or_url(g) for g in args.game]
    policies = args.policies.split(",")
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    client = LLMClient()
    # 整场基准跑在同一个镜像上：分数才只取决于策略，不取决于那一刻的网络
    mirror = _mirror(args.har)

    rows: list[dict[str, Any]] = []
    for g in games:
        for pol in policies:
            cls = POLICIES[pol]
            for s in seeds:
                kw: dict[str, Any] = {"seed": s}
                if pol in ("llm", "llm_memory"):
                    kw["client"] = client
                    kw["memory"] = pol == "llm_memory"
                res = run_episode(g, s, cls(**kw), max_steps=args.max_steps, offline=mirror)
                rows.append({
                    "game": g.rsplit("/", 2)[-2] if "/" in g else g,
                    "policy": pol, "seed": s,
                    "defect": (res.defect or {}).get("kind"),
                    "steps": res.steps, "states": res.distinct_states,
                    "replayed": (verify_pack(g, res.pack, offline=mirror)["replayed"]
                                 if res.pack else None),
                    "wall_ms": res.wall_ms, "error": res.error,
                })

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"rows": rows}
    if mirror is not None:
        payload["mirror"] = mirror.report()
    (out / "bench.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8")

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
    s.add_argument("--har", default=None, help="用离线包重放（零联网）；先跑 playtest capture")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("replay", help="校验一个复现包是否真的可复现")
    r.add_argument("pack")
    r.add_argument("--game", default=None, help="覆盖包里的 URL（本地重放时用）")
    r.add_argument("--har", default=None, help="用离线包重放（零联网）")
    r.set_defaults(fn=cmd_replay)

    c = sub.add_parser("capture", help="把一个线上游戏取成 .har 离线包，供 --har 重放")
    c.add_argument("url")
    c.add_argument("--har", default="out/offline/capture.har")
    c.add_argument("--settle-ms", type=int, default=3000)
    c.add_argument("--warm-clicks", action="store_true",
                   help="录制期点几下界面，把懒加载的分块也录进包（仅对不消耗别人额度的纯客户端应用用）")
    c.add_argument("--top-up", action="store_true",
                   help="补包：已知字节走磁盘、未命中放行联网录回，再合并进同一个 .har")
    c.add_argument("--episodes", default=None,
                   help="补包时再按这些种子跑真实探索（点击热身走不到的地方由探索补上），逗号分隔")
    c.add_argument("--max-steps", type=int, default=40, help="配合 --episodes 用")
    c.set_defaults(fn=cmd_capture)

    b = sub.add_parser("bench", help="跑策略对比基准")
    b.add_argument("--game", action="append", required=True)
    b.add_argument("--policies", default="random,curiosity,semantic,llm,llm_memory")
    b.add_argument("--seeds", default="20261105,20261106")
    b.add_argument("--max-steps", type=int, default=60)
    b.add_argument("--out", default="out/bench")
    b.add_argument("--har", default=None, help="整场基准跑在同一个离线包上（分数只取决于策略）")
    b.set_defaults(fn=cmd_bench)

    ns = ap.parse_args(argv)
    try:
        return ns.fn(ns)
    finally:
        shutdown_browser()


if __name__ == "__main__":
    raise SystemExit(main())
