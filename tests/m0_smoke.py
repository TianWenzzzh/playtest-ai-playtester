"""M0 冒烟：确定性与复现包必须当场成立，否则后面全白搭。

跑法：  .venv/Scripts/python.exe tests/m0_smoke.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import CuriosityPolicy, LLMPolicy, RandomPolicy, SemanticPolicy, run_episode, verify_pack
from playtest.env import BrowserEnv
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
    env._advance(30)
    c3 = env.read().clock
    check("advance(30) 后帧数 +30", c3["frames"] == c1["frames"] + 30, f"{c1['frames']}->{c3['frames']}")

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

print()
if fails:
    print("M0 未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M0 全部通过")
