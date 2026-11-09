"""M1 生死判据：作者**一行协议都没接**的游戏，工具还能不能测出可复现的缺陷。

注册模式只证明"我们写的游戏我们能测"；
兜底模式跑通别人的游戏，才证明这工具对本届那几百件作品有意义。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import pathlib
import sys
import threading

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

print("== D. 一个动作只触发一次 click：开关型控件不许被双派发抵消 ==")
# 被三枚已公开的假阳性教出来的：domApply 先派发合成 click，再对 A/BUTTON 调 el.click()，
# 监听器跑两遍 ⇒ 静音/语音这类开关被拨过去又拨回来，指纹逐位不变 ⇒ check_ui_dead_end
# 把接好线的控件判成死路。夹具形状取自实测的第三方作品（aria-label 恒定 + textContent 翻转、
# title 翻转）。下面四条正对照守住"最小修法"不许顺手削掉的能力：链接导航、勾选框 checked、
# 画布/DOM 坐标、以及真没接线的控件仍然要开火。
from playtest.detectors import check_ui_dead_end, click_wiring  # noqa: E402

FIRES_JS = """(function(){return {
  snd: window.__FIRES_SND__, ttl: window.__FIRES_TTL__, nav: window.__FIRES_NAV__,
  chk: window.__FIRES_CHK__, coord: window.__FIRES_COORD__, coord_x: window.__COORD_X__,
  snd_text: document.getElementById('snd').textContent,
  ttl_title: document.getElementById('ttl').title,
  checked: document.getElementById('chk').checked,
  loc_hash: String(location.hash || '')};})()"""


def one_action(action_name: str):
    """在夹具上只走一个动作，返回 ( firing 计数与 DOM 前值, 后值, 前哈希, 后哈希 )。"""
    with BrowserEnv(uri("toggle-sound")) as env:
        env.start()
        if action_name not in env.actions():
            return None
        f0 = env.frame.evaluate(FIRES_JS)
        h0 = env.read().hash
        h1 = env.step(action_name).hash
        f1 = env.frame.evaluate(FIRES_JS)
        return f0, f1, h0, h1


r = one_action("click:button:SOUND_TOGGLE")
check("开关按钮在动作空间里", r is not None, "click:button:SOUND_TOGGLE")
if r:
    f0, f1, h0, h1 = r
    check("一次动作只触发一次 click 监听器（aria-label 恒定的开关）",
          f1["snd"] - f0["snd"] == 1, f"{f0['snd']} → {f1['snd']}")
    check("开关按钮点一下，textContent 真的翻到 SOUND_OFF",
          f1["snd_text"] == "SOUND_OFF", f1["snd_text"])
    check("开关按钮点一下，状态指纹必须变（否则会被判成死路）", h0 != h1,
          f"{h0[-12:]} → {h1[-12:]}")

r = one_action("click:button:VOICE_ON")
check("title 型开关在动作空间里", r is not None, "click:button:VOICE_ON")
if r:
    f0, f1, h0, h1 = r
    check("一次动作只触发一次 click 监听器（title 翻转的开关）",
          f1["ttl"] - f0["ttl"] == 1, f"{f0['ttl']} → {f1['ttl']}")
    check("title 型开关点一下，title 真的翻到 VOICE_OFF",
          f1["ttl_title"] == "VOICE_OFF", f1["ttl_title"])
    check("title 型开关点一下，状态指纹必须变", h0 != h1, f"{h0[-12:]} → {h1[-12:]}")

r = one_action("click:a:NAV_LINK")
check("链接在动作空间里", r is not None, "click:a:NAV_LINK")
if r:
    f0, f1, _h0, _h1 = r
    check("一次动作只触发一次 click 监听器（链接）",
          f1["nav"] - f0["nav"] == 1, f"{f0['nav']} → {f1['nav']}")
    check("链接的浏览器默认导航行为没被削掉", f1["loc_hash"] == "#nav-probe", f1["loc_hash"])

r = one_action("click:input:CHECK_BOX")
check("勾选框在动作空间里", r is not None, "click:input:CHECK_BOX")
if r:
    f0, f1, _h0, _h1 = r
    check("一次动作只触发一次 change 监听器（勾选框）",
          f1["chk"] - f0["chk"] == 1, f"{f0['chk']} → {f1['chk']}")
    check("勾选框的 checked 仍然翻得动", f1["checked"] is True, str(f1["checked"]))

with BrowserEnv(uri("toggle-sound")) as env:
    env.start()
    center = env.frame.evaluate(
        "(function(){var r=document.getElementById('coord').getBoundingClientRect();"
        "return r.left + r.width / 2;})()")
    f0 = env.frame.evaluate(FIRES_JS)
    env.step("click:div:COORD_PAD")
    f1 = env.frame.evaluate(FIRES_JS)
    check("一次动作只触发一次 click 监听器（带坐标的 DOM 垫）",
          f1["coord"] - f0["coord"] == 1, f"{f0['coord']} → {f1['coord']}")
    check("非激活行为元素仍然拿到真实坐标（画布游戏要读 clientX）",
          abs(f1["coord_x"] - center) <= 1, f"clientX={f1['coord_x']} 期望≈{center:.1f}")

with BrowserEnv(uri("toggle-sound")) as env:
    env.start()
    before = env.read()
    after = env.step("click:button:DEAD_CONTROL")
    d = check_ui_dead_end(env, "click:button:DEAD_CONTROL", before, after,
                          {"click:button:SOUND_TOGGLE"})
    check("真没接线的控件仍然被判死路（修双派发不许把判据一起消音）",
          d is not None and d.get("kind") == "ui_dead_end",
          json.dumps(d, ensure_ascii=False) if d else "None")

print("== E. 死路判据的边界：接了线的控件不许判死，真没接线的仍要开火 ==")
# 被「六枚主张逐条复核后全是假阳性」教出来的。两种形状都取自实测的第三方作品（已去身份）：
# ① 控件自身没有监听器，click 挂在父容器上，而且当前状态下合法地没反应（钱不够）——
#    这正是本判据文档串里「买钥匙」那个例子，原先的 ever_effective 守卫挡不住它；
# ② 控件确实改了页面，但改动落在指纹盲区里（texts 只取前 40 个叶子节点、完全不读 storage）。
# 两种都不是死控件。分辨不了就会拿假阳性去挂别人的作品。
LIMITS_JS = """(function(){return {
  delegate: window.__FIRES_DELEGATE__, mem: window.__FIRES_MEM__, coins: window.__COINS__,
  toasts: document.querySelectorAll('.toast').length,
  toast_text: (document.querySelector('.toast')||{}).textContent || ''};})()"""

with BrowserEnv(uri("deadend-limits")) as env:
    env.start()
    acts = env.actions()
    for nm in ("DELEGATED_INERT", "PLAIN_EFFECTIVE", "LATE_DOM_WRITER", "TRULY_UNWIRED"):
        check(f"边界夹具动作空间里有 {nm}", f"click:button:{nm}" in acts)

    # 先造出"本局确实有别的动作起过作用"这个前提：否则判据按设计直接返回 None，
    # 下面两条断言就成了空转 —— 测不到东西的绿不算绿。
    b0 = env.read()
    a0 = env.step("click:button:PLAIN_EFFECTIVE")
    check("正对照前提：PLAIN_EFFECTIVE 真的改了指纹", a0.hash != b0.hash,
          f"{b0.hash[-12:]} → {a0.hash[-12:]}")
    effective = {"click:button:PLAIN_EFFECTIVE"}

    f0 = env.frame.evaluate(LIMITS_JS)
    before = env.read()
    after = env.step("click:button:DELEGATED_INERT")
    f1 = env.frame.evaluate(LIMITS_JS)
    check("委托控件的处理器真的跑了（不是没接线）", f1["delegate"] - f0["delegate"] == 1,
          f"{f0['delegate']} → {f1['delegate']}")
    check("委托控件在本状态下指纹不变（钱不够，合法空操作）", after.hash == before.hash)
    d = check_ui_dead_end(env, "click:button:DELEGATED_INERT", before, after, effective)
    check("容器委托＋合法空操作不许判成死路", d is None,
          json.dumps(d, ensure_ascii=False) if d else "None")

    f0 = env.frame.evaluate(LIMITS_JS)
    before = env.read()
    after = env.step("click:button:LATE_DOM_WRITER")
    f1 = env.frame.evaluate(LIMITS_JS)
    check("晚期 DOM 写手的处理器真的跑了", f1["mem"] - f0["mem"] == 1,
          f"{f0['mem']} → {f1['mem']}")
    check("页面真的变了（DOM 深处多出一条提示）",
          f1["toasts"] == f0["toasts"] + 1 and f1["toast_text"] == "MEMORY_CLEARED",
          f"toasts={f1['toasts']} text={f1['toast_text']!r}")
    check("而探针指纹看不见这条变化（盲区成立，夹具才有效）", after.hash == before.hash,
          f"{before.hash[-12:]} → {after.hash[-12:]}")
    d = check_ui_dead_end(env, "click:button:LATE_DOM_WRITER", before, after, effective)
    check("指纹盲区造成的空读数不许判成死路", d is None,
          json.dumps(d, ensure_ascii=False) if d else "None")

    before = env.read()
    after = env.step("click:button:TRULY_UNWIRED")
    check("真没接线的控件指纹确实不变", after.hash == before.hash)
    d = check_ui_dead_end(env, "click:button:TRULY_UNWIRED", before, after, effective)
    check("真没接线的控件仍然开火（新守卫不许把判据整个消音）",
          d is not None and d.get("kind") == "ui_dead_end",
          json.dumps(d, ensure_ascii=False) if d else "None")

print("== F. 传播链必须走到 window：document/window 级委托也算接了线 ==")
# 上一版守卫用 parentElement 往上爬，到 <html> 就断了（html.parentElement 是 null，
# 它爹是 document，不是 Element）。于是 document / window 上的监听器一个都看不见 ——
# React 16 恰恰把整套合成事件挂在 document 上，那一整类假阳性会原样复活。
# 深度写成硬读数：button(0) → body(1) → html(2) → document(3) → window(4)。
DELEG_JS = """(function(){return {doc: window.__FIRES_DOC__, win: window.__FIRES_WIN__,
  n: window.__N__};})()"""

with BrowserEnv(uri("deadend-delegation")) as env:
    env.start()
    acts = env.actions()
    for nm in ("PLAIN_EFFECTIVE", "DOC_DELEGATE", "WIN_DELEGATE"):
        check(f"委托夹具动作空间里有 {nm}", f"click:button:{nm}" in acts)

    b0 = env.read()
    a0 = env.step("click:button:PLAIN_EFFECTIVE")
    check("正对照前提：PLAIN_EFFECTIVE 真的改了指纹", a0.hash != b0.hash,
          f"{b0.hash[-12:]} → {a0.hash[-12:]}")
    effective = {"click:button:PLAIN_EFFECTIVE"}

    for nm, key, depth in (("DOC_DELEGATE", "doc", 3), ("WIN_DELEGATE", "win", 4)):
        act = f"click:button:{nm}"
        f0 = env.frame.evaluate(DELEG_JS)
        before = env.read()
        after = env.step(act)
        f1 = env.frame.evaluate(DELEG_JS)
        check(f"{nm} 的处理器真的跑了（不是没接线）", f1[key] - f0[key] == 1,
              f"{f0[key]} → {f1[key]}")
        check(f"{nm} 在本状态下指纹不变（盲区成立，夹具才有效）", after.hash == before.hash,
              f"{before.hash[-12:]} → {after.hash[-12:]}")
        w = click_wiring(env, act)
        check("传播链一路走到 window（半截链不许当成量过了）",
              w.get("reached_window") is True, json.dumps(w, ensure_ascii=False))
        check(f"{nm} 的接线被量到，且链上深度 {depth} 处有监听器",
              w["wired"] is True and any(s["depth"] == depth for s in w["sites"]),
              json.dumps(w, ensure_ascii=False))
        d = check_ui_dead_end(env, act, before, after, effective)
        check(f"{nm} 不许判成死路", d is None,
              json.dumps(d, ensure_ascii=False) if d else "None")

print("== G. iframe 承载的应用：接线必须量得到，不许整类消音 ==")
# 实测两件 docker 创空间（try888/cat-detective、yangx2048/Describe_it）各 14/13 个动作
# 全部 reason='subframe'：env._resolve_frame() 刻意挑承载应用的子 frame，而守卫见到
# 子 frame 就拒绝 ⇒ ui_dead_end 在整个 iframe 作品群上沉默，沉默又被读成"没有缺陷"。
# 这正是 env.py 里"探针失明"那段警告的同一类失败，只是换到了接线通道上。
#
# 🔴 正负对照必须住在两份文档里：click_wiring 量的是"链上有没有监听器"，不是"处理器会不会
# 理这个 target"。document 上挂了 click，本文档每个控件就都算接了线（这是 C4 那类假阳性的
# 正确解法）。第一版把 TRULY_UNWIRED 和 DOC_DELEGATE 放进同一个 document，负对照当场被
# 自己的正对照接线，wired=True 是对的 —— 错的是夹具。
IFRAME_JS = """(function(){return {doc: window.__FIRES_DOC__, n: window.__N__};})()"""


def iframe_uri(name: str) -> str:
    return (ROOT / "games" / "deadend-iframe" / name).resolve().as_uri()


print("  -- G1 内层文档挂 document 委托：量得到，且不许判死路 --")
with BrowserEnv(iframe_uri("index.html")) as env:
    env.start()
    check("前提：解析到的 frame 不是主文档（否则本段根本没在测 iframe）",
          env.frame != env.page.main_frame, env.frame.url[-40:])
    check("前提：子 frame 里真的是应用（外壳自己一个按钮都没有）",
          env.frame.url.endswith("app.html"), env.frame.url[-40:])
    acts = env.actions()
    for nm in ("PLAIN_EFFECTIVE", "DOC_DELEGATE"):
        check(f"iframe 应用动作空间里有 {nm}", f"click:button:{nm}" in acts)

    b0 = env.read()
    a0 = env.step("click:button:PLAIN_EFFECTIVE")
    check("正对照前提：PLAIN_EFFECTIVE 真的改了指纹", a0.hash != b0.hash,
          f"{b0.hash[-12:]} → {a0.hash[-12:]}")
    effective = {"click:button:PLAIN_EFFECTIVE"}

    act = "click:button:DOC_DELEGATE"
    f0 = env.frame.evaluate(IFRAME_JS)
    before = env.read()
    after = env.step(act)
    f1 = env.frame.evaluate(IFRAME_JS)
    check("DOC_DELEGATE 的处理器真的跑了（不是没接线）", f1["doc"] - f0["doc"] == 1,
          f"{f0['doc']} → {f1['doc']}")
    check("DOC_DELEGATE 在本状态下指纹不变（盲区成立，夹具才有效）",
          after.hash == before.hash, f"{before.hash[-12:]} → {after.hash[-12:]}")
    wd = click_wiring(env, act)
    check("iframe 里的接线量得到（不许再回 subframe / cdp_unavailable）",
          wd.get("available") is True and wd.get("reason") == "",
          json.dumps(wd, ensure_ascii=False))
    check("传播链在 iframe 里也一路走到 window，深度硬读数 nodes=5",
          wd.get("reached_window") is True and wd.get("nodes") == 5,
          json.dumps(wd, ensure_ascii=False))
    check("iframe 里的 document 委托被量到，且链上深度 3 处有监听器",
          wd.get("wired") is True and any(s["depth"] == 3 for s in wd.get("sites", [])),
          json.dumps(wd, ensure_ascii=False))
    dd = check_ui_dead_end(env, act, before, after, effective)
    check("iframe 里接了线的控件不许判成死路", dd is None,
          json.dumps(dd, ensure_ascii=False) if dd else "None")
    wp = click_wiring(env, "click:button:PLAIN_EFFECTIVE")
    check("document 委托的正确后果：同文档里别的控件也算接了线",
          wp.get("wired") is True and any(s["depth"] == 3 for s in wp.get("sites", [])),
          json.dumps(wp, ensure_ascii=False))

print("  -- G2 内层文档一个委托都没有：真没接线的控件必须仍然开火 --")
with BrowserEnv(iframe_uri("unwired.html")) as env:
    env.start()
    check("前提：解析到的 frame 不是主文档", env.frame != env.page.main_frame,
          env.frame.url[-40:])
    check("前提：子 frame 里是无委托那份应用",
          env.frame.url.endswith("app-unwired.html"), env.frame.url[-40:])
    acts = env.actions()
    for nm in ("PLAIN_EFFECTIVE", "TRULY_UNWIRED"):
        check(f"无委托 iframe 应用动作空间里有 {nm}", f"click:button:{nm}" in acts)

    b0 = env.read()
    a0 = env.step("click:button:PLAIN_EFFECTIVE")
    check("正对照前提：PLAIN_EFFECTIVE 真的改了指纹", a0.hash != b0.hash,
          f"{b0.hash[-12:]} → {a0.hash[-12:]}")
    effective = {"click:button:PLAIN_EFFECTIVE"}
    wp = click_wiring(env, "click:button:PLAIN_EFFECTIVE")
    check("同一文档里接了线的控件量得到 wired=True（尺子不是恒 False）",
          wp.get("available") is True and wp.get("wired") is True,
          json.dumps(wp, ensure_ascii=False))

    w = click_wiring(env, "click:button:TRULY_UNWIRED")
    check("真没接线的控件：量得到且 wired=False、sites 为空",
          w.get("available") is True and w.get("wired") is False and w.get("sites") == [],
          json.dumps(w, ensure_ascii=False))
    check("负对照的传播链也走到 window（不是半截链导致的假 False）",
          w.get("reached_window") is True and w.get("nodes") == 5,
          json.dumps(w, ensure_ascii=False))

    before = env.read()
    after = env.step("click:button:TRULY_UNWIRED")
    check("负对照前提：点它指纹确实不变", after.hash == before.hash,
          f"{before.hash[-12:]} → {after.hash[-12:]}")
    d = check_ui_dead_end(env, "click:button:TRULY_UNWIRED", before, after, effective)
    check("iframe 里真没接线的控件仍然开火（修好消音不等于丢掉判据）",
          d is not None and d.get("kind") == "ui_dead_end",
          json.dumps(d, ensure_ascii=False) if d else "None")

print("== H. 跨源 iframe（OOPIF）：应用有自己的 CDP 会话，page 会话里根本找不到它 ==")
# 段 G 修的是同进程 iframe（没有独立会话，必须在 page 会话上显式给 contextId）。
# 但两件真实 docker 创空间实测是**另一种**形态：顶层 www.modelscope.cn、内层 *.ms.show，
# 跨源 ⇒ OOPIF ⇒ page 会话的 Page.getFrameTree 只有顶层一个节点（url_match_count=0），
# Runtime.enable 也只发顶层的上下文；反过来 new_cdp_session(frame) 能成，且求值落在应用里。
# 只修同进程那一支，等于把真实作品那一群继续消音（27/27 个动作 frame_context_not_resolvable）。
#
# 本地用 hostname 之差造真 OOPIF：外壳走 127.0.0.1、内层走 localhost，两者是不同 site。
# 已先量过（raw/audit/s2_oopi_shape_1108.log：own session ok / url_match_count=0 / CLASS=OOPIF_REPRODUCED）。
# 🔴 下面两条前提断言就是用来钉住"这一段真的在测 OOPIF"：哪天 Chromium 改了进程模型，
# 它会当场判红，而不是静默退回同进程形态、让本段变成段 G 的重复。
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ.get("no_proxy", "") + ",127.0.0.1,localhost"


class _QuietGames(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, *_) -> None:  # 关服时的连接重置不是测试失败
        pass


def oopi_pins(env, app_file: str) -> None:
    check("前提：解析到的 frame 不是主文档", env.frame != env.page.main_frame,
          env.frame.url[-46:])
    check("前提：内层走 localhost（与外壳的 127.0.0.1 不同 site）",
          env.frame.url.startswith("http://localhost:") and env.frame.url.endswith(app_file),
          env.frame.url[-46:])
    own_ok, own_err = False, ""
    try:
        cf = env._context.new_cdp_session(env.frame)
        try:
            r = cf.send("Runtime.evaluate", {
                "expression": "!!window.__GSPP__", "returnByValue": True})
            own_ok = r.get("result", {}).get("value") is True
        finally:
            cf.detach()
    except Exception as e:
        own_err = str(e)[:160]
    check("前提：OOPIF 有自己的 CDP 会话，且会话里就是应用（探针在）",
          own_ok, own_err or "own session ok, __GSPP__=true")
    cp = env._context.new_cdp_session(env.page)
    try:
        tree = cp.send("Page.getFrameTree")["frameTree"]
        urls, stack = [], [tree]
        while stack:
            n = stack.pop()
            urls.append(n["frame"].get("url"))
            stack.extend(n.get("childFrames") or [])
    finally:
        cp.detach()
    check("前提：page 会话的 frame tree 里没有这个 frame（否则本段测的是同进程形态）",
          env.frame.url not in urls, json.dumps(urls, ensure_ascii=False))


_oopi_box: dict[str, int] = {}


class _OopiShell(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        apps = {"/__oopi_unwired.html": "app-unwired.html",
                "/__oopi_delegate.html": "app.html"}
        if path in apps:
            src = f"http://localhost:{_oopi_box['port']}/deadend-iframe/{apps[path]}"
            body = ("<!doctype html><meta charset='utf-8'><title>oopi-shell</title>"
                    "<h1>local shell</h1>"
                    f"<iframe src='{src}' title='app'></iframe>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


_srv = _QuietGames(("127.0.0.1", 0),
                   functools.partial(_OopiShell, directory=str(ROOT / "games")))
_oopi_box["port"] = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_oopi_base = f"http://127.0.0.1:{_oopi_box['port']}"
try:
    print("  -- H1 OOPIF 内层无委托：真没接线的控件必须仍然开火 --")
    with BrowserEnv(f"{_oopi_base}/__oopi_unwired.html") as env:
        env.start()
        oopi_pins(env, "app-unwired.html")
        acts = env.actions()
        for nm in ("PLAIN_EFFECTIVE", "TRULY_UNWIRED"):
            check(f"OOPIF 应用动作空间里有 {nm}", f"click:button:{nm}" in acts)

        b0 = env.read()
        a0 = env.step("click:button:PLAIN_EFFECTIVE")
        check("正对照前提：PLAIN_EFFECTIVE 真的改了指纹", a0.hash != b0.hash,
              f"{b0.hash[-12:]} → {a0.hash[-12:]}")
        effective = {"click:button:PLAIN_EFFECTIVE"}
        wp = click_wiring(env, "click:button:PLAIN_EFFECTIVE")
        check("OOPIF 里接了线的控件量得到 wired=True（尺子不是恒 False）",
              wp.get("available") is True and wp.get("wired") is True,
              json.dumps(wp, ensure_ascii=False))

        w = click_wiring(env, "click:button:TRULY_UNWIRED")
        check("OOPIF 里真没接线的控件：量得到且 wired=False、sites 为空",
              w.get("available") is True and w.get("wired") is False and w.get("sites") == [],
              json.dumps(w, ensure_ascii=False))
        check("OOPIF 负对照的传播链也走到 window，nodes=5（不是半截链导致的假 False）",
              w.get("reached_window") is True and w.get("nodes") == 5,
              json.dumps(w, ensure_ascii=False))

        before = env.read()
        after = env.step("click:button:TRULY_UNWIRED")
        check("负对照前提：点它指纹确实不变", after.hash == before.hash,
              f"{before.hash[-12:]} → {after.hash[-12:]}")
        d = check_ui_dead_end(env, "click:button:TRULY_UNWIRED", before, after, effective)
        check("OOPIF 里真没接线的控件仍然开火（真实创空间就是这一形态）",
              d is not None and d.get("kind") == "ui_dead_end",
              json.dumps(d, ensure_ascii=False) if d else "None")

    print("  -- H2 OOPIF 内层挂 document 委托：量得到，且不许判死路 --")
    with BrowserEnv(f"{_oopi_base}/__oopi_delegate.html") as env:
        env.start()
        oopi_pins(env, "app.html")
        acts = env.actions()
        for nm in ("PLAIN_EFFECTIVE", "DOC_DELEGATE"):
            check(f"OOPIF 委托应用动作空间里有 {nm}", f"click:button:{nm}" in acts)
        env.step("click:button:PLAIN_EFFECTIVE")
        effective = {"click:button:PLAIN_EFFECTIVE"}

        act = "click:button:DOC_DELEGATE"
        f0 = env.frame.evaluate(IFRAME_JS)
        before = env.read()
        after = env.step(act)
        f1 = env.frame.evaluate(IFRAME_JS)
        check("DOC_DELEGATE 的处理器真的跑了（不是没接线）", f1["doc"] - f0["doc"] == 1,
              f"{f0['doc']} → {f1['doc']}")
        check("DOC_DELEGATE 在本状态下指纹不变（盲区成立，夹具才有效）",
              after.hash == before.hash, f"{before.hash[-12:]} → {after.hash[-12:]}")
        wd = click_wiring(env, act)
        check("OOPIF 里的接线量得到（不许回 frame_context_not_resolvable）",
              wd.get("available") is True and wd.get("reason") == "",
              json.dumps(wd, ensure_ascii=False))
        check("OOPIF 里的 document 委托被量到，链上深度 3 处有监听器",
              wd.get("wired") is True and any(s["depth"] == 3 for s in wd.get("sites", [])),
              json.dumps(wd, ensure_ascii=False))
        check("OOPIF 正对照的传播链也走到 window，nodes=5",
              wd.get("reached_window") is True and wd.get("nodes") == 5,
              json.dumps(wd, ensure_ascii=False))
        dd = check_ui_dead_end(env, act, before, after, effective)
        check("OOPIF 里接了线的控件不许判成死路", dd is None,
              json.dumps(dd, ensure_ascii=False) if dd else "None")
finally:
    _srv.shutdown()

print()
if fails:
    print("M1 兜底未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M1 兜底全部通过")
