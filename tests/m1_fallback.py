"""M1 生死判据：作者**一行协议都没接**的游戏，工具还能不能测出可复现的缺陷。

注册模式只证明"我们写的游戏我们能测"；
兜底模式跑通别人的游戏，才证明这工具对本届那几百件作品有意义。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import CuriosityPolicy, run_episode, verify_pack
from playtest.env import BrowserEnv
from playtest.trace import pack_id, save_pack

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


def uri(name: str) -> str:
    return (ROOT / "games" / name / "index.html").resolve().as_uri()


print("== A. 模式判定：没接协议必须落到 dom-fallback ==")
for name in ("clicker-quest",):
    with BrowserEnv(uri(name)) as env:
        env.start()
        check(f"{name} 模式 = dom-fallback", env.mode == "dom-fallback", env.mode)
        acts = env.actions()
        print("     推断出的动作空间:", json.dumps(acts, ensure_ascii=False))
        check(f"{name} 动作空间非空", len(acts) > 0, str(len(acts)))
        clicks = [a for a in acts if a.startswith("click:")]
        check(f"{name} 识别出按钮控件", len(clicks) >= 3, ",".join(clicks))

print("== B. 零接入下发现 UI 死路并产出可回放复现包 ==")
found = None
for s in (20261105, 20261106, 20261107):
    r = run_episode(uri("clicker-quest"), s, CuriosityPolicy(s), max_steps=40)
    if r.error:
        print("     错误:", s, r.error)
        continue
    kind = (r.defect or {}).get("kind")
    print(f"     seed={s} steps={r.steps} states={r.distinct_states} defect={kind}")
    if r.defect:
        found = r
        break
check("零接入游戏上开火", found is not None and found.defect is not None)
if found and found.defect:
    print("     签名:", found.defect["signature"])
    print("     动作:", json.dumps([a["action"] for a in found.pack["actions"]], ensure_ascii=False))
    v = verify_pack(uri("clicker-quest"), found.pack)
    print("     回放校验:", json.dumps(v, ensure_ascii=False))
    check("零接入的复现包也能逐帧全等", v["replayed"] is True,
          f"{v.get('steps_matched')}/{v.get('steps_total')}")
    OUT.mkdir(exist_ok=True)
    save_pack(found.pack, OUT / f"m1-fallback-{pack_id(found.pack)}.json")

print()
if fails:
    print("M1 兜底未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M1 兜底全部通过")
