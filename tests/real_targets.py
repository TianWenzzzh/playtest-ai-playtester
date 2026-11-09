"""在真实第三方作品上跑一轮 —— M1.5 的可重跑证据。

这些是魔搭创空间上的公开应用，作者不是我们，也没接我们的协议。
真实地址与作品名一律由本机供给（公开仓库只留不可追溯代号，
见 evidence/corpus.json 的 redaction 注记）：PLAYTEST_REAL_URL 是允许完整探索的
那个纯客户端小游戏，PLAYTEST_READONLY_URL_1 / _2 是只做只读探测的两个。
默认只读探测（不派发任何动作）：其中两个会调云端模型，每点一次都消耗作者的额度，
拿别人的额度换自己的分数这种事不做。

跑法：PYTHONUTF8=1 python tests/real_targets.py
缺上述变量时判红退出，不静默跳过。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from playtest.agent import CuriosityPolicy, run_episode, verify_pack
from playtest.env import PROBE_DIR, resolve_browser, shutdown_browser
from playtest.trace import pack_id, save_pack

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# (名称, URL, 是否允许派发动作)；URL 由本机环境供给，仓库里不存真实地址。
TARGETS = [
    ("作品 A", os.environ.get("PLAYTEST_REAL_URL", ""), True),
    ("作品 D", os.environ.get("PLAYTEST_READONLY_URL_1", ""), False),
    ("作品 E", os.environ.get("PLAYTEST_READONLY_URL_2", ""), False),
]

OUT = pathlib.Path(__file__).resolve().parent.parent / "out" / "real"


def inspect_only(url: str) -> dict:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=resolve_browser(),
                              args=["--disable-gpu", "--mute-audio", "--no-sandbox"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1100, "height": 760})
        for s in ("virtual_clock.js", "seeded_random.js", "probe.js"):
            ctx.add_init_script(path=str(PROBE_DIR / s))
        pg = ctx.new_page()
        pg.goto(url, wait_until="load", timeout=45000)
        pg.wait_for_timeout(2500)
        frames = []
        for f in pg.frames:
            try:
                frames.append({
                    "url": f.url[:90],
                    "nodes": f.evaluate("document.querySelectorAll('body *').length"),
                    "controls": f.evaluate("document.querySelectorAll("
                                          "'button,[role=button],a[href],input,canvas,[onclick]').length"),
                    "title": f.evaluate("document.title"),
                })
            except Exception as exc:
                frames.append({"url": f.url[:90], "error": str(exc)[:80]})
        ctx.close()
        b.close()
    return {"frames": frames}


def main() -> int:
    missing = [name for name, url, _ in TARGETS if not url]
    if missing:
        print("FAIL  真实作品地址未配置：" + ",".join(missing)
              + "（未测，不能跳过后打印 PASS）")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    for name, url, allow_explore in TARGETS:
        print(f"\n===== {name}  ({'允许探索' if allow_explore else '只读'})")
        report[name] = {"url": url, "explored": allow_explore, "inspect": inspect_only(url)}
        for f in report[name]["inspect"]["frames"]:
            print("  frame", f)
        if not allow_explore:
            continue
        res = run_episode(url, 20261105, CuriosityPolicy(20261105), max_steps=40)
        row = {"steps": res.steps, "states": res.distinct_states, "probes": res.probes,
               "defect": (res.defect or {}).get("kind"),
               "signature": (res.defect or {}).get("signature"), "error": res.error}
        if res.pack:
            row["determinism_class"] = verify_pack(url, res.pack).get("determinism_class")
            res.pack["verification"] = {"determinism_class": row["determinism_class"]}
            save_pack(res.pack, OUT / f"{name}-{pack_id(res.pack)}.json")
        report[name]["episode"] = row
        print("  ", json.dumps(row, ensure_ascii=False)[:400])

    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总落盘 {OUT / 'report.json'}")
    shutdown_browser()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
