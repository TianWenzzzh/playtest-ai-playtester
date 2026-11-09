"""M3′ 离线取包 —— 把"异步不可复现"从一句限制，变成一个有出口的处置。

06 §8.3 测到的事实：远端真实作品上，同一 (seed, 动作序列) 两次跑会漂，
漂的源头是异步资源的到达顺序，不是持久状态，也不是我们的哈希域。
`_classify()` 的 mitigation 早就写了出口："把页面与资源取到本地离线跑"。
这个文件就是那条出口的落地与验证。

四段，缺一不可：
  1. 管线正对照 —— 自有夹具走"录包 → 关源 → 离线重放"，必须仍然 bit_reproducible。
     证明新加的镜像层本身没有引入不确定性。
  2. 负控 —— 把镜像里那一条清掉，加载必须当场失败。
     证明页面吃的字节**真的**来自镜像，而不是背后那条还活着的网络。
     （捕获完之后本地服务是被 shut down 的，所以"没网还能跑"本身就是证据。）
  3. 发现腿（自有夹具 canary）—— 在 planted 缺陷夹具上离线跑，必须发现缺陷且复现包
     replayed=true。证明仪器本身有「离线发现并复现缺陷」的能力。
  4. 真实第三方作品（作品 A（脱敏代号），作者不是我们，没接我们的协议）——
     离线镜像健康度：扫描有效完整（供字节·代码全覆盖·探针未失明·每局 steps>0）。
     不再要求「真实作品必须开火」：那是作品的经验属性，不是工具质量门。work-a 是健康
     作品（40/120 步诚实负、17 池扫尽零开火），开火数作为诚实发现报告，0=诚实负而非仪器
     失败。仪器「能发现缺陷」的能力由段3 canary 守；本段只在镜像/探针坏掉时判红（#15 拆臂）。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m3_offline.py
真实作品地址由本机供给：PLAYTEST_REAL_URL=https://…
（公开仓库只留脱敏代号；缺这个变量时第 4 段判红而不是静默跳过，
同 tests/release_public.py 的处置）。
可选：--skip-real（只跑 1、2、3 段）／--recapture（重取 har）
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import pathlib
import re
import socketserver
import sys
import threading
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import run_episode, sweep, verify_pack
from playtest.env import BrowserEnv, shutdown_browser
from playtest.offline import OfflineMirror, capture, harvest_episodes, summarize, top_up
from playtest.policies import CuriosityPolicy, RandomPolicy

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "offline"
GAMES = ROOT / "games"

REAL_URL = os.environ.get("PLAYTEST_REAL_URL", "")
REAL_HOST = "/".join(REAL_URL.split("/")[:3]) if REAL_URL else ""
REAL_HAR = OUT / "work-a.har"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: Any = "") -> None:
    results.append((label, bool(ok), str(detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def _fire_evidence_ok(verif: dict[str, Any]) -> bool:
    """离线镜像保证的是「初始状态一致 + 定级诚实」，不是「异步应用必然位级复现」。

    作品 A（脱敏代号） 23 步那枚按 repeat3 台账本就是 async_dependent（第 18 步动画分叉，
    initial_hash_matched=true、前缀 18/23 可重放，见 evidence/ledger/work-a_repeat3.json）；
    强求 replayed=True 会在合法样本上假红。下限 steps_matched>=1 挡「开机即崩」，
    level 非 unverified 挡「镜像/探针坏了连初始状态都对不上」。
    """
    level = (verif.get("determinism_class") or {}).get("level")
    return (verif.get("initial_hash_matched") is True
            and level in ("bit_reproducible", "async_dependent")
            and int(verif.get("steps_matched", 0)) >= 1)


def _scan_valid_ok(rep: dict[str, Any], code_miss: list[str],
                   rows: list[dict[str, Any]]) -> bool:
    """段4 诚实判据：真实作品离线镜像健康 + 扫描是一次有效完整的测量。

    这不是「真实作品必须开火」（那是作品经验属性，不是工具质量门 —— work-a 健康，
    40/120 步诚实负、17 池扫尽零开火）。开火数作为诚实发现单独报告，0=诚实负而非仪器
    失败；仪器「能离线发现并复现缺陷」的能力由段3 自有夹具 canary 守，不靠这条臂。
    本判据只在镜像/探针坏掉时判红：
      served>0       镜像确实在供字节（空镜像/坏镜像 → 红）
      not code_miss  代码字节全覆盖（懒加载未命中 → 红，归因仪器而非应用异步）
      rows 非空且每局 error is None、steps>0  探针未失明、每局真跑起来了
                     （挡 work-w 型 VOID：err+steps=0 被静默计为干净负，#29）
    """
    return (int(rep.get("served", 0)) > 0
            and not code_miss
            and len(rows) > 0
            and all(r.get("error") is None and int(r.get("steps", 0)) > 0 for r in rows))


class _Quiet(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, *_) -> None:  # 关服时的连接重置不是测试失败
        pass


def serve_games() -> tuple[Any, int]:
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

    srv = _Quiet(("127.0.0.1", 0), functools.partial(H, directory=str(GAMES)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def walk(url: str, seed: int, steps: int, offline: Any = None,
         policy: str = "random") -> dict[str, Any]:
    """按策略走 steps 步，逐帧记哈希。两次同种子调用可比 = 环境的确定性读数。"""
    pol = (RandomPolicy if policy == "random" else CuriosityPolicy)(seed)
    hashes: list[str] = []
    actions: list[str] = []
    seen: set[str] = set()
    with BrowserEnv(url, offline=offline) as env:
        snap = env.reset(seed)
        hashes.append(snap.hash)
        for _ in range(steps):
            acts = env.actions()
            if not acts:
                break
            pick = pol.choose(acts, snap, seen, actions)
            snap = env.step(pick)
            actions.append(pick)
            hashes.append(snap.hash)
            seen.add(snap.hash)
        return {
            "actions": actions, "hashes": hashes, "mode": env.mode,
            "relinks": env.relinks,
            "asset_errors": list(env.asset_errors), "exceptions": list(env.page_exceptions),
        }


# ---------------------------------------------------------------- 段 1 + 段 2
def stage_fixture() -> dict[str, Any]:
    print("\n== 1. 管线正对照：自有夹具走「录包 → 关源 → 离线重放」仍逐帧全等 ==")
    OUT.mkdir(parents=True, exist_ok=True)
    srv, port = serve_games()
    url = f"http://127.0.0.1:{port}/blocky-jump/index.html?maxgap=6"
    har = OUT / "fixture-blocky.har"
    info = capture(url, har)
    check("录制 har 非空", info["entries"] > 0, f"{info['entries']} 条 / {info['bytes']} B")

    # 🔴 关源：之后任何一次联网都会变成 mirror_miss，被记下来而不是被悄悄满足
    srv.shutdown()
    srv.server_close()
    print("   （本地服务已关闭 —— 下面三次重放若有任何请求回源，就会落成 mirror_miss）")

    mirror = OfflineMirror(har)
    a = walk(url, 20261105, 14, offline=mirror)
    b = walk(url, 20261105, 14, offline=mirror)
    report = mirror.report()
    check("镜像确实在供字节", report["served"] > 0, f"served={report['served']}")
    check("零回源（misses 必须为空）", report["miss_calls"] == 0, report["miss_examples"])
    check("两次独立离线重放逐帧全等", a["hashes"] == b["hashes"] and len(a["hashes"]) > 3,
          f"{len(a['hashes'])} 帧，前 4 位 {' '.join(h.split(':')[-1][:6] for h in a['hashes'][:4])}")
    check("动作序列也一致", a["actions"] == b["actions"], " ".join(a["actions"][:6]))

    print("\n== 2. 负控：把文档的三条服务路径一起拆掉，加载必须当场失败 ==")
    starving = OfflineMirror(har)
    n = starving.forget("index.html")
    check("forget 把三条路径一起拆干净",
          n > 0 and not starving.index and not any(v is not None for v in starving.norm_index.values())
          and not starving.root_doc_map, f"删 {n} 条，派生档已重算")
    broke = None
    try:
        walk(url, 20261105, 3, offline=starving)
    except Exception as exc:  # noqa: BLE001 - 这里要的就是"起不来"
        broke = f"{type(exc).__name__}"
    check("断网后页面确实起不来（证明字节来自镜像而非网络）", broke is not None, broke or "居然加载成功了")

    print("\n== 3. 缺陷腿： planted 臂离线复现到 bit 级 ==")
    fresh = OfflineMirror(har)
    res = run_episode(url, 20261105, CuriosityPolicy(20261105), max_steps=40, offline=fresh)
    check("离线也能发现缺陷", res.defect is not None,
          (res.defect or {}).get("kind") or res.error)
    repacked: dict[str, Any] = {}
    if res.pack:
        repacked = verify_pack(url, res.pack, offline=fresh)
        check("离线复现包 replayed=true", repacked["replayed"] is True,
              f"steps {repacked['steps_matched']}/{repacked['steps_total']} "
              f"⇒ {repacked['determinism_class']['level']}")
        res.pack["verification"] = repacked
        (OUT / "fixture-blocky-pack.json").write_text(
            json.dumps(res.pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"har": summarize(har), "mirror": fresh.report(),
            "walk_hashes": len(a["hashes"]), "repack": repacked}


# ---------------------------------------------------------------- 段 3
def _app_frame(page):
    """录制期用的那个 frame：真正承载应用的 ms.show 文档，不是平台壳。"""
    for f in page.frames:
        if (f.url or "").startswith(REAL_HOST):
            return f
    return None


def _click_walk(page, rounds: int = 4, per_round: int = 5) -> int:
    """录制期热身：在应用 frame 里点几下，把懒加载的代码分块与美术请求出来。

    这一步决定镜像的覆盖面有多宽 —— 只录首屏的包，跑到第二屏就会未命中。
    只对纯客户端的应用用（点一次不消耗任何人的推理额度）；
    会调云端模型的那些，宁可让包小一点，也不替作者烧额度。
    """
    clicked = 0
    for _ in range(rounds):
        fr = _app_frame(page)
        if fr is None:
            page.wait_for_timeout(800)
            continue
        try:
            btns = fr.query_selector_all("button")
        except Exception:  # noqa: BLE001 - frame 可能被替换掉，跳过这一轮
            continue
        for b in btns[:per_round]:
            try:
                if b.is_visible():
                    b.click(timeout=800)
                    clicked += 1
                    page.wait_for_timeout(350)
            except Exception:  # noqa: BLE001 - 点不动的按钮不是失败
                pass
    return clicked


def stage_real(skip: bool, recapture: bool) -> dict[str, Any]:
    print("\n== 4. 真实第三方作品（作品 A（脱敏代号），作者不是我们）离线跑 ==")
    if skip:
        print("   SKIP（--skip-real）")
        return {"skipped": True}
    if not REAL_URL:
        check("真实作品地址已配置", False,
              "缺少 PLAYTEST_REAL_URL：未测，不能跳过后打印 PASS")
        return {"skipped": True, "reason": "target_not_configured"}
    if recapture or not REAL_HAR.exists():
        info = capture(REAL_URL, REAL_HAR, warm=_click_walk)
        print(f"   取包：{info['entries']} 条 / {info['bytes']} B / 线上 {info['wire_bytes']} B "
              f"/ 补声明 {info['declared_prefetched']} 条")

    seed = 20261105
    seeds = [seed, seed + 1]
    origin = "/".join(REAL_URL.split("/")[:3])

    def measure() -> dict[str, Any]:
        mirror = OfflineMirror(REAL_HAR)
        print(f"   镜像 GET 条目：{len(mirror.index)}")
        a = walk(REAL_URL, seed, 12, offline=mirror)
        b = walk(REAL_URL, seed, 12, offline=mirror)
        rows = sweep(REAL_URL, seeds, lambda s: CuriosityPolicy(s), OUT / "real-packs",
                     max_steps=40, offline=mirror)["episodes"]
        late = mirror.report()
        code_miss = [m for m in late["miss_examples"]
                     if re.search(r"\.(js|mjs|css)([?#]|$)", m)]
        return {"mirror": mirror, "a": a, "b": b, "rows": rows,
                "late": late, "code_miss": code_miss}

    # 首屏热身不等于完整覆盖面。若探索发现懒加载代码未命中，先补包、让真实 episode
    # 把深层字节请求出来，再重新量；否则"离线复现失败"会被误归因给应用异步。
    rebuilt_rounds = 0
    for _round in range(3):
        m = measure()
        if not m["code_miss"]:
            break
        print(f"   代码字节未命中 {len(m['code_miss'])} 条 ⇒ 补包并重测（第 {rebuilt_rounds + 1} 轮）")
        top_up(REAL_URL, REAL_HAR, host=origin)
        harvest_episodes(REAL_URL, REAL_HAR, seeds=seeds, max_steps=40)
        rebuilt_rounds += 1
    else:
        print("   三轮补包后仍有代码未命中，下面会如实判红")

    mirror, a, b = m["mirror"], m["a"], m["b"]
    rep = m["late"]
    rows = m["rows"]

    check("探针在离线镜像里可用", a["mode"] != "unstarted", f"mode={a['mode']}")
    code_miss = m["code_miss"]
    check("未命中里没有代码字节", not code_miss, code_miss[:3])
    misses_a = sorted(x for x in a["asset_errors"] if x.startswith("mirror_miss"))
    misses_b = sorted(x for x in b["asset_errors"] if x.startswith("mirror_miss"))
    check("两次离线跑的未命中集合一致", misses_a == misses_b,
          f"{len(misses_a)} 条：{[x.split(':')[-1].split('/')[-1][:28] for x in misses_a[:4]]}")
    same = a["hashes"] == b["hashes"]
    check("离线两次同种子逐帧全等", same,
          f"{len(a['hashes'])} 帧 / 分歧在第 "
          f"{next((i for i, (x, y) in enumerate(zip(a['hashes'], b['hashes'])) if x != y), -1)} 帧")
    print(f"   served={rep['served']} 分档 {rep['served_by']} / 未命中 {rep['miss_calls']} "
          f"/ 导航后重新解析 frame {a['relinks']}+{b['relinks']} 次")
    if not same:
        n = min(len(a["hashes"]), len(b["hashes"]))
        first = next((i for i in range(n) if a["hashes"][i] != b["hashes"][i]), None)
        print(f"   ⚠️ 离线仍会漂（首处分歧 frame={first}）⇒ 异步依赖不止网络一层，"
              f"React 调度/动画本身也在贡献非确定性。这条要如实进台账。")

    fire = [r for r in rows if r["defect"]]
    for r in rows:
        print(f"   seed {r['seed']}: steps={r['steps']} states={r['distinct_states']} "
              f"defect={r['defect'] and r['defect']['kind']} replayed={r['replayed']} "
              f"err={r['error']}")
    check("真实作品镜像健康度：扫描有效完整（供字节·代码全覆盖·探针未失明·每局 steps>0）",
          _scan_valid_ok(rep, code_miss, rows),
          f"served={rep.get('served')} code_miss={len(code_miss)} rows={len(rows)} "
          f"fires={len(fire)}（0=诚实负，非仪器失败）")
    check("扫描有效判据负控：空镜像/代码未命中/探针失明/步数0/空局必须判否",
          not _scan_valid_ok({"served": 0}, [], [{"error": None, "steps": 40}])
          and not _scan_valid_ok({"served": 100}, ["chunk.js"], [{"error": None, "steps": 40}])
          and not _scan_valid_ok({"served": 100}, [], [{"error": "RuntimeError", "steps": 0}])
          and not _scan_valid_ok({"served": 100}, [], [{"error": None, "steps": 0}])
          and not _scan_valid_ok({"served": 100}, [], [])
          and _scan_valid_ok({"served": 100}, [], [{"error": None, "steps": 40}]),
          "broken→False, healthy complete→True")
    fire_verifs: dict[int, Any] = {}
    for r in fire:
        pp = OUT / "real-packs" / f"{r['policy']}-{r['seed']}-{r['pack_id']}.json"
        fire_verifs[r["seed"]] = (
            json.loads(pp.read_text(encoding="utf-8")).get("verification", {})
            if pp.exists() else {})
    check("开火的包离线初始状态一致且定级诚实（非 unverified；无开火则空真，判据由负控守）",
          all(_fire_evidence_ok(v) for v in fire_verifs.values()),
          [(s, (v.get("determinism_class") or {}).get("level"),
            v.get("steps_matched"), v.get("steps_total")) for s, v in fire_verifs.items()]
          or "本轮无开火（诚实负）：判据强度由下方负控 + 段3 canary 守")
    check("离线复现判据负控：unverified/初始不符/开机即崩必须判否",
          not _fire_evidence_ok({"initial_hash_matched": False,
                                 "determinism_class": {"level": "unverified"}, "steps_matched": 0})
          and not _fire_evidence_ok({"initial_hash_matched": True,
                                     "determinism_class": {"level": "async_dependent"}, "steps_matched": 0})
          and _fire_evidence_ok({"initial_hash_matched": True,
                                 "determinism_class": {"level": "async_dependent"},
                                 "steps_matched": 18, "steps_total": 23}),
          "broken→False, legal async 18/23→True")
    check("深度探索后仍未命中代码字节", not code_miss, code_miss[:3])
    print(f"   自重建补包：{rebuilt_rounds} 轮")
    return {"mirror": rep, "bit_equal": same, "rebuilt_rounds": rebuilt_rounds,
            "frames": len(a["hashes"]), "misses": sorted(
                {x.split(":", 1)[1] for x in mirror.misses})[:20],
            "episodes": [{k: r[k] for k in ("seed", "steps", "distinct_states", "defect",
                                            "pack_id", "replayed", "error")} for r in rows]}


def main() -> int:
    skip_real = "--skip-real" in sys.argv
    recapture = "--recapture" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"fixture": stage_fixture(), "real": stage_real(skip_real, recapture)}
    (OUT / "m3-offline.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    shutdown_browser()

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 46}\n{len(results) - len(failed)}/{len(results)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:120]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
