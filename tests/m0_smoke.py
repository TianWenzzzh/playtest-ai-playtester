"""M0 冒烟：确定性与复现包必须当场成立，否则后面全白搭。

跑法：  .venv/Scripts/python.exe tests/m0_smoke.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import CuriosityPolicy, LLMPolicy, RandomPolicy, SemanticPolicy, run_episode, verify_pack
from playtest.env import BrowserEnv, render_warning
from playtest.trace import TraceRecorder, pack_id, save_pack

ROOT = pathlib.Path(__file__).resolve().parent.parent
GAME = (ROOT / "games" / "blocky-jump" / "index.html").as_uri()
OUT = ROOT / "out"

SEQUENCE = ["jump", "jump", "wait", "jump", "jump", "wait", "jump", "jump", "jump", "jump"]
SEED = 20261105

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


def seq_for_seed(seed: int) -> list[str]:
    with BrowserEnv(GAME, headless=True) as env:
        env.start()
        rec = TraceRecorder(env, seed)
        hashes = [rec.start.hash]
        for a in SEQUENCE:
            hashes.append(rec.step(a).hash)
        return hashes


print("== 1. 探针注入与模式 ==")
with BrowserEnv(GAME, headless=True) as env:
    env.start()
    check("probe 模式 = registered", env.mode == "registered", env.mode)
    acts = env.actions()
    check("动作空间非空", len(acts) > 0, ",".join(acts))
    s0 = env.reset(SEED)
    s1 = env.reset(SEED)
    check("同种子 reset 得到同初始状态", s0.hash == s1.hash, s0.hash[:24])
    s2 = env.reset(SEED + 1)
    check("换种子得到不同初始状态", s0.hash != s2.hash)

print("== 2. 确定性：同 (seed, 动作序列) 两次独立进程逐帧哈希全等 ==")
run_a = seq_for_seed(SEED)
run_b = seq_for_seed(SEED)
check("两次独立浏览器实例的哈希序列逐值相等", run_a == run_b, f"{len(run_a)} 帧")
print("     " + " ".join(h.split(":")[1][:6] for h in run_a))

print("== 3. 虚拟时钟真的在控制时间（不是墙钟） ==")
with BrowserEnv(GAME, headless=True) as env:
    env.start()
    env.reset(SEED)
    c1 = env.read().clock
    c2 = env.read().clock
    check("不推进时时钟不动", c1["frames"] == c2["frames"], str(c1["frames"]))
    # 时钟本身的契约：打 n 帧就是 n 帧，一帧不多。直接打在时钟上，不经 env。
    env._js("window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.advance(30)")
    c3 = env.read().clock
    check("时钟 advance(30) 恰好 +30 帧", c3["frames"] == c1["frames"] + 30,
          f"{c1['frames']}->{c3['frames']}")
    # env._advance 在时钟之上多了一层「等资产就绪」（解码完成时刻不在帧栅格上，
    # 不等就会把"图还没画出来"那一瞬当成指纹），所以增量只可能 ≥ 30。
    # 它必须仍然可复现：同一状态下两次调用得到同一个增量。
    deltas = []
    for _ in range(2):
        with BrowserEnv(GAME, headless=True) as e2:
            e2.start()
            e2.reset(SEED)
            f0 = e2.read().clock["frames"]
            e2._advance(30)
            deltas.append(e2.read().clock["frames"] - f0)
    check("env 推进规则可复现且不小于请求帧数", deltas[0] == deltas[1] and deltas[0] >= 30, str(deltas))

print("== 4. 种子真的在控制随机 ==")
with BrowserEnv(GAME, headless=True) as env:
    env.start()
    env.reset(SEED)
    r1 = env.read()
    env.reset(SEED)
    r2 = env.read()
    check("同种子 → 同平台布局", r1.data["level"] == r2.data["level"] and r1.hash == r2.hash)
    env.reset(SEED + 7)
    r3 = env.read()
    check("换种子 → 不同关卡布局", r3.hash != r1.hash)

print("== 5. 探索器开火 + 复现包回放校验 ==")
res = run_episode(GAME, SEED, CuriosityPolicy(SEED), max_steps=40)
check("探索器发现缺陷", res.defect is not None, res.error or f"probes={res.probes}")
if res.defect:
    print("     缺陷类型:", res.defect["kind"])
    print("     签名:", res.defect["signature"])
    print("     证据:", json.dumps(res.defect.get("detail", {}).get("state"), ensure_ascii=False))
    v = verify_pack(GAME, res.pack)
    print("     回放校验:", json.dumps(v, ensure_ascii=False))
    check("复现包在新实例里逐帧哈希全等", v["replayed"] is True, f"{v.get('steps_matched')}/{v.get('steps_total')}")
    res.pack["verification"] = v
    OUT.mkdir(exist_ok=True)
    p = save_pack(res.pack, OUT / f"m0-{pack_id(res.pack)}.json")
    print("     落盘:", p)

print("== 6. 正对照：把间隙上限收到可跳距离以内，工具必须保持沉默 ==")
SAFE = GAME + "?maxgap=3"
false_pos = 0
for s in (SEED, SEED + 1, SEED + 2, SEED + 3, SEED + 4):
    r = run_episode(SAFE, s, CuriosityPolicy(s), max_steps=40)
    if r.defect:
        false_pos += 1
        print("     误报:", s, r.defect["kind"], r.defect["signature"])
    elif r.error:
        print("     错误:", s, r.error)
check("可通关变体上零误报", false_pos == 0, f"{false_pos}/5 误报")

print("== 7. 随机策略基线（对照表里的下限） ==")
rr = run_episode(GAME, SEED, RandomPolicy(SEED), max_steps=40)
check("随机基线能跑完", rr.error is None,
      f"steps={rr.steps} states={rr.distinct_states} defect={(rr.defect or {}).get('kind')}")

print("== 8. 策略层：语义启发式 + LLM 不可用时必须如实标注降级 ==")
rs = run_episode(GAME, SEED, SemanticPolicy(SEED), max_steps=40)
check("语义策略能跑完", rs.error is None, f"steps={rs.steps} defect={(rs.defect or {}).get('kind')}")

llm = LLMPolicy(SEED)
rl = run_episode(GAME, SEED, llm, max_steps=40)
check("LLM 策略在无端点时不崩", rl.error is None, rl.error or "")
if not llm.client.cfg.available():
    check("LLM 不可用被如实记进 trace", rl.policy_trace.get("llm_available") is False,
          str(rl.policy_trace.get("note")))
    check("LLM 臂仍产出可用结果（等价 curiosity）", rl.defect is not None,
          rl.policy_trace.get("last_error", ""))
    print("     降级原因:", llm.client.cfg.reason_unavailable())
else:
    check("LLM 臂真的走通了模型调用", rl.policy_trace.get("llm_hits", 0) > 0,
          json.dumps(rl.policy_trace.get("calls", {}), ensure_ascii=False))

print("== 9. 渲染后端自证：软件渲染必须报警，且仪器不许改动被测游戏 ==")
# 为什么这条值得进冒烟闸门：无头浏览器没有 GPU 直通时会**静默**退回 SwiftShader，
# 那时 canvas 像素指纹大面积同色 —— "画面没动"类判据会把渲染降级误判成游戏坏了。
# 不报警就是闭眼跑。（成因来自外部调研报告 08 A1，落地前先量我们自己会不会中招。）
env9 = BrowserEnv(GAME, headless=True)
try:
    env9.start()
    r9 = dict(env9.render or {})
    check("探针报得出渲染后端", bool(r9.get("renderer")) or bool(r9.get("error")),
          json.dumps(r9, ensure_ascii=False)[:150])
    check("环境量已归集成报警句", isinstance(env9.render_note, str))
    if r9.get("software"):
        check("本机是软件渲染 ⇒ 必须正在报警", "软件渲染" in env9.render_note, env9.render_note[:80])
    else:
        check("本机不是软件渲染 ⇒ 不许误报", env9.render_note == "",
              f"renderer={r9.get('renderer')!r} note={env9.render_note[:60]!r}")

    # 🔴 关键不变量：读渲染后端这件事**不许改变游戏**。
    # 若探针去碰游戏自己的 canvas（对无上下文的 canvas 调 getContext 会替它建上下文），
    # 状态指纹就会动 —— 那是仪器改被测物。用改前/改后同帧哈希直接判。
    before = env9.read().hash
    mid = env9._js("window.__GSPP__.render()")
    after = env9.read().hash
    check("重复读渲染后端不改变状态哈希", before == after,
          f"{before[:12]} vs {after[:12]} · renderer={str((mid or {}).get('renderer'))[:40]}")
    check("渲染后端读数跨调用一致（环境量该有的样子）",
          (mid or {}).get("renderer") == (env9._js("window.__GSPP__.render()") or {}).get("renderer"))
finally:
    env9.close()

# 分类器两臂：不开浏览器就能判它会不会咬人。
soft = render_warning({"webgl": True, "renderer": "ANGLE (Google, Vulkan 0.0.0 "
                         "(SwiftShader, llvmpipe)", "vendor": "Google Inc.", "software": True})
hard = render_warning({"webgl": True, "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060)",
                       "vendor": "NVIDIA", "software": False})
masked = render_warning({"webgl": True, "renderer": "WebKit WebGL", "vendor": "WebKit",
                         "software": False, "masked": True})
check("植入 SwiftShader ⇒ 分类器开火", "软件渲染" in soft, soft[:60])
check("真 GPU ⇒ 分类器沉默", hard == "", hard[:60])
check("掩码串 ⇒ 降档而不是沉默", "掩码" in masked, masked[:60])
check("空读数不许编出一句报警", render_warning({}) == "")

print()
if fails:
    print("M0 未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M0 全部通过")
