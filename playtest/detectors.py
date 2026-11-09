"""缺陷检测器。

分类不是拍脑袋来的，对应游戏 QA 里真实会提的单子：

  crash              抛异常 / 页面崩
  invariant_violation 作者自己声明的不变量被违反（协议里 invariants() 的返回值）
  softlock           从当前状态出发，K 步内到不了任何终局 —— 玩家既赢不了也死不掉，永久卡住
  unwinnable         能到终局，但到不了"赢"的终局 —— 关卡已死，只是游戏还让你继续操作
  ui_dead_end        （DOM 兜底模式）一个可见可点的控件，派发后状态指纹毫无变化，且整条 click 链上无监听器

softlock 与 unwinnable 的判定靠协议里的 snapshot/restore：
把当前状态存下来，穷举动作往下探，再回滚。没有回滚能力就没有"探完再回来"，
也就没有可证的死路 —— 这是 snapshot/restore 存在的唯一理由。
"""
from __future__ import annotations

import json
from typing import Any

from .env import BrowserEnv, Snapshot


def _terminal(s: Snapshot) -> bool:
    return s.over


def reachability(env: BrowserEnv, horizon: int = 12, fanout_cap: int = 8, budget: int = 300) -> dict[str, Any]:
    """从当前状态出发，horizon 步内可达的终局集合。

    返回 {"terminals": [...], "states_seen": n, "truncated": bool}
    深度上限是启发式的代价，必须如实暴露：truncated=True 时不能断言"不存在赢的路径"，
    只能说"horizon 内没找到"。未截断才允许下 unwinnable / softlock 结论。

    🔴 budget 是硬护栏：去重靠等价键，但等价键声明得不好的游戏（比如没声明 key
    又带计时器）会让状态空间假装无限大，DFS 会跑到天荒地老。没有这道护栏，
    一个坏夹具就能把整批评测挂住 —— 这不是理论风险，是实测挂过一次。
    """
    origin = env.snapshot_state()
    if origin is None:
        return {"terminals": [], "states_seen": 0, "truncated": True, "reason": "无 snapshot 能力，无法回滚探测"}

    terminals: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_full: set[str] = set()
    stack: list[tuple[int, dict[str, Any]]] = [(0, origin)]
    states_seen = 0
    truncated = False

    while stack:
        if states_seen >= budget:
            truncated = True
            stack.clear()
            break
        depth, state = stack.pop()
        if depth >= horizon:
            truncated = True
            continue
        if not env.restore_state(state):
            truncated = True
            continue
        actions = env.actions()[:fanout_cap]
        for a in actions:
            # 🔴 每个动作之前都要回到本节点状态再试。
            # 只在出栈时回滚的话，第二个动作是在第一个动作之后的位置上做的 ——
            # 探到的是一条路径而不是可达集，"没有终局可达"这个结论就不健全，
            # 而 softlock / unwinnable 两个判据全都建立在它上面。
            if not env.restore_state(state):
                truncated = True
                break
            snap = env.step(a)
            states_seen += 1
            seen_full.add(snap.hash)
            if _terminal(snap):
                terminals.append({"hash": snap.hash, "win": snap.win, "depth": depth + 1, "action": a})
                continue
            # 用等价键去重，不用全量哈希：带回合计数器的游戏全量哈希永不重复，
            # 状态空间会假装无限大，探测永远截断。
            if snap.eq_key() in seen:
                continue
            child = env.snapshot_state()
            if child is None:
                truncated = True
                continue
            seen.add(snap.eq_key())
            stack.append((depth + 1, child))
            if states_seen >= budget:
                break

    env.restore_state(origin)
    return {"terminals": terminals, "states_seen": states_seen, "truncated": truncated,
            "distinct_positions": len(seen), "distinct_full": len(seen_full)}


def key_is_too_coarse(r: dict[str, Any]) -> bool:
    """识别 key() 投影声明得过粗。

    🔴 症状：探测报"没有任何终局可达"，但全量哈希其实在变 ——
    说明作者从等价键里漏掉了一个**会影响转移**的字段（实测漏的是 cooldown：
    它门控着跳跃，不参与等价判定 ⇒ 所有动作在键空间里都成了自环 ⇒
    工具"看不见"任何进展 ⇒ 把 unwinnable 误报成 softlock）。

    投影过粗时绝不能下 softlock 结论。宁可报"判不了"，也不要报一个错的类型 ——
    报错类型的代价是别人照着错的诊断去修，修不好还怀疑工具。
    """
    if r["terminals"]:
        return False
    return r.get("distinct_positions", 0) <= 1 and r.get("distinct_full", 0) > 1


def check_step(env: BrowserEnv, snap: Snapshot, prev_errors: int) -> dict[str, Any] | None:
    """每步都要跑的轻量判据。重的可达性探测单独走 probe_deadend。"""
    if snap.page_exceptions:
        return {
            "kind": "crash",
            "first_seen_step": snap.frame,
            "signature": "crash:" + snap.page_exceptions[0][:120],
            "detail": {"exceptions": snap.page_exceptions[:5], "state": snap.data},
        }
    if snap.errors > prev_errors:
        return {
            "kind": "crash",
            "first_seen_step": snap.frame,
            "signature": f"page_error:{snap.errors}",
            "detail": {"state": snap.data},
        }
    if snap.violations:
        return {
            "kind": "invariant_violation",
            "first_seen_step": snap.frame,
            "signature": "inv:" + "|".join(sorted(snap.violations))[:120],
            "detail": {"violations": snap.violations, "state": snap.data},
        }
    return None


def probe_deadend(env: BrowserEnv, snap: Snapshot, horizon: int = 12) -> dict[str, Any] | None:
    """对可疑状态做一次完整的可达性探测。

    调用时机要省着点：每个新状态都探一次会慢到不可用。
    策略是在探索循环里，当"连续若干步没见到新状态"时才触发。
    """
    if _terminal(snap):
        return None
    r = reachability(env, horizon=horizon)
    if r["truncated"]:
        return None
    if key_is_too_coarse(r):
        # 宁可不报，也不报一个错的类型
        return None
    wins = [t for t in r["terminals"] if t["win"]]
    if not r["terminals"]:
        return {
            "kind": "softlock",
            "first_seen_step": snap.frame,
            "signature": f"softlock@{snap.hash.split(':')[1][:12]}|horizon={horizon}|states={r['states_seen']}",
            "detail": {
                "state": snap.data,
                "horizon": horizon,
                "states_explored": r["states_seen"],
                "note": "该深度内不存在任何终局：玩家无法达成任何结局",
            },
        }
    if not wins:
        return {
            "kind": "unwinnable",
            "first_seen_step": snap.frame,
            "signature": f"unwinnable@{snap.hash.split(':')[1][:12]}|terminals={len(r['terminals'])}|horizon={horizon}",
            "detail": {
                "state": snap.data,
                "horizon": horizon,
                "terminals_reachable": r["terminals"],
                "note": "可达终局全部为失败结局：该局面已不可能通关",
            },
        }
    return None


CLICK_TYPES = frozenset({
    "click", "pointerdown", "mousedown", "mouseup", "pointerup", "touchstart", "touchend",
})
# mouseenter/mouseover 不在里面：只接了悬停高亮、没接点击的按钮正是本判据要抓的真阳性。
_INLINE_JS = (
    "function(){var t=" + json.dumps(sorted(CLICK_TYPES)) + ";"
    "for(var i=0;i<t.length;i++){if(typeof this['on'+t[i]]==='function')return true;}"
    "return false;}"
)
_PARENT_JS = "function(){return this.parentNode || (this.nodeType===9 ? this.defaultView : null);}"
# 必须用 parentNode 不是 parentElement：<html> 的 parentElement 是 null（它爹是 document，
# 不是 Element），用 parentElement 爬会断在 html，document/window 上的委托一个都看不见。
_AT_WINDOW_JS = "function(){return this === window;}"


def _frame_context_id(cdp: Any, frame: Any) -> int | None:
    """在 page 的 CDP 会话里找到承载应用那个 frame 的主世界执行上下文。

    同进程 iframe 没有独立会话（new_cdp_session(frame) 会被浏览器拒绝：
    "it is a part of the parent frame's session"），而对着 page 的默认上下文求值
    量到的是外壳 —— 外壳上当然一个监听器都没有。所以必须显式给 contextId。
    frame 靠 URL 认；认不唯一就返回 None，宁可报"测不到"也不量错文档。
    """
    stack = [cdp.send("Page.getFrameTree")["frameTree"]]
    hits: list[str] = []
    while stack:
        node = stack.pop()
        if node["frame"].get("url") == frame.url:
            hits.append(node["frame"]["id"])
        stack.extend(node.get("childFrames") or [])
    if len(hits) != 1:
        return None
    contexts: list[dict[str, Any]] = []
    cdp.on("Runtime.executionContextCreated", lambda e: contexts.append(e["context"]))
    cdp.send("Runtime.enable")
    for c in contexts:
        aux = c.get("auxData") or {}
        if aux.get("frameId") == hits[0] and aux.get("isDefault"):
            return int(c["id"])
    return None


def _app_cdp(env: BrowserEnv) -> tuple[Any, int | None]:
    """拿到承载应用那个 frame 的 CDP 会话，以及（必要时）它的主世界 contextId。

    两种 iframe 形态都实测存在，通道正好相反：
      跨源 OOPIF —— 真实魔搭创空间（顶层 www.modelscope.cn、内层 *.ms.show）。frame 有
        自己的会话；page 会话的 Page.getFrameTree 里只有顶层一个节点，Runtime.enable 也只发
        顶层的上下文 ⇒ 在 page 会话上永远解析不出它的 contextId。
      同进程 iframe —— 本地 file:// 夹具。new_cdp_session(frame) 被浏览器拒绝
        （"it is a part of the parent frame's session"）⇒ 只能绑 page，再显式给 contextId，
        否则求值落在外壳上，外壳当然一个监听器都没有。
    所以先试 frame 自己的会话，被拒再退 page+contextId。退的那支认不出唯一 frame 就抛
    LookupError，调用方如实报"测不到"，绝不猜一个文档去量。
    """
    try:
        return env._context.new_cdp_session(env.frame), None
    except Exception:
        pass
    cdp = env._context.new_cdp_session(env.page)
    ctx = _frame_context_id(cdp, env.frame)
    if ctx is None:
        try:
            cdp.detach()
        except Exception:
            pass
        raise LookupError("frame_context_not_resolvable")
    return cdp, ctx


def click_wiring(env: BrowserEnv, action: str, max_depth: int = 16) -> dict[str, Any]:
    """问浏览器：这个控件的 click 传播链上到底有没有接监听器（含祖先容器与内联 on*）。

    只读，不派发事件。判据里再点一次会让录制多出一步没被记录的动作，回放当场失真。
    委托到父容器也算接了线：React 的 div#root、道具栏 div 上挂着整套监听器，
    这类控件在"钱不够"这种状态下合法地什么都不做，与"没接线"在指纹上完全同形。

    一路爬到 window 才算量过完整条传播链；半截链（shadow 边界、游离节点）与任何
    CDP 失败都置 wired=None，调用方必须沉默 —— 报"测不到"，不报"这个按钮坏了"。
    """
    out: dict[str, Any] = {"available": False, "reason": "", "wired": None,
                           "reached_window": False, "nodes": 0, "sites": []}
    try:
        cdp, ctx = _app_cdp(env)
    except LookupError as e:
        out["reason"] = str(e)
        return out
    except Exception as e:
        out["reason"] = f"cdp_unavailable:{e}"
        return out
    try:
        expr = f"window.__GSPP__ && window.__GSPP__.elementFor({json.dumps(action)})"
        params: dict[str, Any] = {"expression": expr, "returnByValue": False}
        if ctx is not None:
            params["contextId"] = ctx
        r = cdp.send("Runtime.evaluate", params)
        oid = (r.get("result") or {}).get("objectId")
        if not oid:
            out["reason"] = "target_not_locatable"
            return out
        last = None
        for depth in range(max_depth):
            if not oid:
                break
            got = cdp.send("DOMDebugger.getEventListeners", {"objectId": oid}).get("listeners", [])
            types = sorted({g.get("type") for g in got if g.get("type") in CLICK_TYPES})
            inline = bool(cdp.send("Runtime.callFunctionOn", {
                "objectId": oid, "returnByValue": True, "functionDeclaration": _INLINE_JS,
            }).get("result", {}).get("value"))
            out["nodes"] = depth + 1
            if types or inline:
                out["sites"].append({"depth": depth, "types": types, "inline": inline})
            last = oid
            oid = cdp.send("Runtime.callFunctionOn", {
                "objectId": oid, "returnByValue": False, "functionDeclaration": _PARENT_JS,
            }).get("result", {}).get("objectId")
        if last:
            out["reached_window"] = bool(cdp.send("Runtime.callFunctionOn", {
                "objectId": last, "returnByValue": True, "functionDeclaration": _AT_WINDOW_JS,
            }).get("result", {}).get("value"))
        if not out["reached_window"]:
            out["reason"] = "chain_incomplete"
            return out
        out["wired"] = bool(out["sites"])
        out["available"] = True
    except Exception as e:
        out["reason"] = f"cdp_error:{e}"
        out["wired"] = None
    finally:
        try:
            cdp.detach()
        except Exception:
            pass
    return out


def check_ui_dead_end(
    env: BrowserEnv, action: str, before: Snapshot, after: Snapshot,
    ever_effective: set[str], confirm_ticks: int = 40
) -> dict[str, Any] | None:
    """DOM 兜底模式专用：一个**从来没有在任何状态下产生过效果**的控件。

    🔴 判据不能是"这一次点了没变化"。实测证明那样会误报：
    「买钥匙」在金币不足时按下去，提示语和状态都不变 —— 这是**状态相关的合法空操作**，
    不是死路。只有当一个控件在我们见过的所有局面下都从来没有起过作用，
    它才是真的没接上逻辑。

    同理，只判 click 不判按键：点击类游戏里方向键本来就该没反应。
    还要求本局已经有别的动作产生过效果 —— 否则可能是整个指纹都不敏感（比如游戏
    在 iframe 里），那种情况该报"测不到"，不该报"这个按钮坏了"。

    🔴 最后一道：必须证明这条 click 链上**一个监听器都没有**（click_wiring）。
    6 枚已发布主张逐条复核后全是假阳性，成立的 3 类成因都能被这道守卫止住 ——
    双派发 click（开关被拨过去又拨回来）、指纹盲区（texts 只取前 40 个叶子、完全不读 storage）、
    容器事件委托（监听器挂在祖先上，且该控件在当前局面是合法空操作）。
    逐枚处置见 evidence/ledger/2026-11-08_ui_dead_end_withdrawal.json。
    它们的共同点是"控件确实接了线，只是我们看不见它的效果"。量不到接线就沉默。
    """
    if env.mode != "dom-fallback" or not action.startswith("click:"):
        return None
    if after.hash != before.hash or after.over:
        return None
    if action in ever_effective or not ever_effective:
        return None

    env._advance(confirm_ticks)
    if env.read().hash != before.hash:
        return None

    wiring = click_wiring(env, action)
    if wiring["wired"] is not False:
        return None

    return {
        "kind": "ui_dead_end",
        "first_seen_step": after.frame,
        "signature": f"deadend:{action}@{before.hash.split(':')[1][:12]}",
        "detail": {
            "action": action,
            "state": before.data,
            "confirmed_after_frames": confirm_ticks,
            "actions_known_effective": sorted(ever_effective),
            "click_wiring": wiring,
            "note": "该控件可见且在动作集合内，本局所有局面下都从未产生过状态变化，"
                    "且整条 click 传播链（含祖先容器与内联 on*）上没有任何监听器",
        },
    }
