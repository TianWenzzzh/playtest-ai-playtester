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

print("== C. 非按钮控件对照：滑杆与勾选框要「看得见变化」，不许判成死路 ==")
# 这条是被一次真实误报教出来的：在真实第三方作品上，音量滑杆被派发 MouseEvent 点不动
# （非可信事件跑不动浏览器自己实现的轨道拖拽），指纹里没有变化 ⇒ 判成 ui_dead_end。
# 那报的是仪器的盲区，不是游戏的缺陷。修法是 domApply 对 range/select 走原生 setter + input/change，
# 并且指纹里给勾选框带上 checked。
with BrowserEnv(uri("clicker-quest")) as env:
    env.start()
    acts = env.actions()
    slider = next((a for a in acts if "音量" in a), None)
    muter = next((a for a in acts if "静音" in a), None)
    check("动作空间里有滑杆", slider is not None, str(slider))
    check("动作空间里有勾选框", muter is not None, str(muter))
    if slider:
        h0 = env.read().hash
        h1 = env.step(slider).hash
        check("点滑杆后状态指纹真的变了", h0 != h1, f"{h0[-8:]} → {h1[-8:]}")
    if muter:
        h0 = env.read().hash
        h1 = env.step(muter).hash
        check("勾选框的 checked 进了指纹（只读 value 会隐形）", h0 != h1,
              f"{h0[-8:]} → {h1[-8:]}")

misreports: list[str] = []
for s in (20261105, 20261108, 20261113):
    rr = run_episode(uri("clicker-quest"), s, CuriosityPolicy(s), max_steps=25)
    sig = (rr.defect or {}).get("signature") or ""
    if "音量" in sig or "静音" in sig:
        misreports.append(f"{s}:{sig}")
check("没有任何一局把滑杆/勾选框报成死路", not misreports, ";".join(misreports))

print()
if fails:
    print("M1 兜底未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M1 兜底全部通过")
