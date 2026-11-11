"""M2.4 修复闭环：红→绿必须是真的绿，而且关掉补丁时必须还是红。

这一组断言里最重要的不是第 2 条（修好了），是第 3 条（改了无关地方时不许变绿）
和第 4、5 条（锚点 0 次 / 2 次必须炸）。少了它们，"验证机器"只是在给任何补丁背书。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.agent import CuriosityPolicy, run_episode
from playtest.fixer import (
    FixResult, ManualProposer, apply_patch, fix, regression, rerun_defect, stage_sources,
)
from playtest.trace import load_pack, save_pack

ROOT = pathlib.Path(__file__).resolve().parent.parent
GAME_DIR = ROOT / "games" / "blocky-jump"
WORK = ROOT / "out" / "fix-work"
SEED = 20261105
fails: list[str] = []

GAP_LINE = "var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * G.maxGap);"
FIXED_LINE = "var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * Math.min(G.maxGap, G.reach));"
UNRELATED_OLD = "var w = 40 + Math.floor(Math.random() * 60);"
UNRELATED_NEW = "var w = 40 + Math.floor(Math.random() * 61);"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


print("== 0. 先拿到一个真实开火的复现包 ==")
GAME_URL = (GAME_DIR / "index.html").resolve().as_uri()
base = run_episode(GAME_URL, SEED, CuriosityPolicy(SEED), max_steps=40)
check("基线开火", base.defect is not None, base.error or "")
if not base.pack:
    print("无复现包，后续无法验证"); sys.exit(1)
pack = base.pack
print("     ", pack["defect"]["kind"], pack["defect"]["signature"])

print("== 1. 锚点纪律：0 次与 2 次都必须炸 ==")
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
srcs = stage_sources(GAME_DIR, WORK / "s1")
check("源文件被复制而非原地改", "index.html" in srcs, str(list(srcs)))

for label, patch in [
    ("锚点 0 次", {"path": "index.html", "old": "这一行在本文件里不存在", "new": "x"}),
    ("锚点 2 次", {"path": "index.html", "old": "G.platforms[G.idx + 1]", "new": "G.platforms[G.idx + 2]"}),
    ("空操作", {"path": "index.html", "old": GAP_LINE, "new": GAP_LINE}),
]:
    try:
        apply_patch(WORK / "s1", patch)
        check(f"{label} 被拒", False, "竟然通过了")
    except (ValueError, FileNotFoundError) as e:
        check(f"{label} 被拒", True, str(e)[:70])

print("== 2. 真补丁：缺陷消失 ==")
res = fix(GAME_DIR, pack, ManualProposer({"path": "index.html", "old": GAP_LINE, "new": FIXED_LINE}), WORK)
check("修复闭环 ok", res.ok, "; ".join(res.notes))
print("     回归:", json.dumps(res.regression, ensure_ascii=False))
check("其余种子无新缺陷", not res.regression["defects"], str(res.regression["defects"]))
check("报告里标明补丁来自人工", res.proposer == "manual" and any("人工" in n for n in res.notes))

print("== 3. 负控：改无关行时缺陷必须还在 ==")
res2 = fix(GAME_DIR, pack, ManualProposer({"path": "index.html", "old": UNRELATED_OLD, "new": UNRELATED_NEW}), WORK)
check("无关改动没有把它判绿", res2.ok is False, "; ".join(res2.notes))
check("负控后缺陷仍在原位", res2.defect_after is not None and res2.defect_after["kind"] == pack["defect"]["kind"],
      str(res2.defect_after))

print("== 4. LLM 生成器在无端点时如实返回 None，不编造补丁 ==")
from playtest.llm import LLMClient
from playtest.fixer import LLMProposer

prop = LLMProposer(LLMClient())
if not prop.client.cfg.available():
    check("无端点时不产出补丁", prop.propose(pack, srcs) is None, prop.client.cfg.reason_unavailable())
else:
    p = prop.propose(pack, srcs)
    check("LLM 产出结构合法的补丁", bool(p) and "old" in (p or {}) and "new" in (p or {}))

print("== 5. 「修好了」这个词的三条腿：回归必须进布尔值，不能只进备注 ==")
# 补这条的起因（实测出来的缺陷，不是设想）：原先 fix() 里 ok=(after is None)，
# reg 只往 notes 里写 ⇒ "把这一处修好、把别处弄坏"的补丁会被界面标成「红转绿 ✅」，
# 而本模块文档串自己承诺过"少了那三条，'我修好了'就只是一句话"。
from playtest.fixer import fix_verdict

ok_a, why_a = fix_verdict(None, {"seeds": 5, "defects": {"crash": 2}, "errors": 0})
check("植入回归 ⇒ 判红（旧代码这里会判绿）", ok_a is False, str(why_a))
check("判红要说清是哪条腿", any("回归" in w for w in why_a), str(why_a))
ok_b, why_b = fix_verdict(None, {"seeds": 5, "defects": {}, "errors": 3})
check("其余种子跑炸也不算修好", ok_b is False, str(why_b))
ok_c, _ = fix_verdict({"kind": "unwinnable"}, {"seeds": 5, "defects": {}, "errors": 0})
check("缺陷仍在原位 ⇒ 判红", ok_c is False)
ok_d, why_d = fix_verdict(None, {"seeds": 5, "defects": {}, "errors": 0})
check("三条腿都过 ⇒ 判绿（正向对照，防我把判据写死成恒红）",
      ok_d is True and any("不再开火" in w for w in why_d), str(why_d))
# 现役路径真的用上了这个判定：上面第 2 节那次真修复仍然必须是绿
check("真夹具那一次仍然判绿（说明新布尔没被写死成恒红）", res.ok is True, "; ".join(res.notes))
check("真夹具的回归读数干净", res.regression["defects"] == {} and res.regression["errors"] == 0,
      json.dumps(res.regression, ensure_ascii=False))

print("== 6. 复现包 ingestion 结构校验：坏包干净拒绝，真实合法包仍接受 ==")
# 起因（真实崩溃面，不是设想）：load_pack 是裸 json.loads，do_replay 随后读
# pack["game"]["url"] ⇒ 语法合法但缺 game 键的 JSON 会 KeyError 崩，不是干净拒绝。
# 负控夹具一律从 §0 那枚真实开火的包派生（删一键/改一处类型），正对照就是它本身。
import copy

SCHEMA_WORK = WORK / "schema"
SCHEMA_WORK.mkdir(parents=True, exist_ok=True)


def _expect_clean_reject(label: str, bad: dict) -> None:
    p = SCHEMA_WORK / "bad.json"
    save_pack(bad, p)
    try:
        load_pack(p)
        check(label, False, "竟然接受了坏包（ingestion 无结构校验）")
    except ValueError as e:
        check(label, True, str(e)[:80])
    except Exception as e:
        check(label, False, f"崩成 {type(e).__name__} 而非干净拒绝：{e}")


def _mutate(fn) -> dict:
    bad = copy.deepcopy(pack)
    fn(bad)
    return bad


_expect_clean_reject("缺 game 键被干净拒绝", _mutate(lambda b: b.pop("game")))
_expect_clean_reject("缺 initial_state.seed 被干净拒绝", _mutate(lambda b: b["initial_state"].pop("seed")))
_expect_clean_reject("缺 initial_state.hash 被干净拒绝", _mutate(lambda b: b["initial_state"].pop("hash")))
_expect_clean_reject("缺 actions 被干净拒绝", _mutate(lambda b: b.pop("actions")))
_expect_clean_reject("缺 defect.signature 被干净拒绝", _mutate(lambda b: b["defect"].pop("signature")))
_expect_clean_reject("game 类型错（非 dict）被干净拒绝", _mutate(lambda b: b.__setitem__("game", "not-a-dict")))
_expect_clean_reject("actions 类型错（非 list）被干净拒绝", _mutate(lambda b: b.__setitem__("actions", {})))
_expect_clean_reject("空 actions 被干净拒绝", _mutate(lambda b: b.__setitem__("actions", [])))
_expect_clean_reject("seed 类型错（非 int）被干净拒绝", _mutate(lambda b: b["initial_state"].__setitem__("seed", "20261105")))
_expect_clean_reject("action 项缺 hash_after 被干净拒绝", _mutate(lambda b: b["actions"][0].pop("hash_after")))

good = SCHEMA_WORK / "good.json"
save_pack(pack, good)
try:
    loaded = load_pack(good)
    check("真实合法包仍被接受（正对照，防恒红把合法包也拒了）", loaded == pack)
except Exception as e:
    check("真实合法包仍被接受（正对照，防恒红把合法包也拒了）", False, f"{type(e).__name__}: {e}")

print("== 6b. UI ingestion（do_replay/do_fix）同样干净拒绝，不在坏包上开浏览器 ==")
import os
# 本机 NO_PROXY 含 [::1] 会让 gradio/httpx 在 import 期崩（实测 Invalid port ':1]'）；
# 导入 ui 前净化到 localhost，与 release_ui 子进程同口径。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
from playtest.ui import do_replay, do_fix

bad_no_game = json.dumps({k: v for k, v in pack.items() if k != "game"}, ensure_ascii=False)
try:
    md, _ = do_replay(None, bad_no_game)
    check("do_replay 缺 game 干净拒绝（不 KeyError 崩）",
          ("game" in md or "结构" in md) and "KeyError" not in md, md[:90])
except KeyError as e:
    check("do_replay 缺 game 干净拒绝（不 KeyError 崩）", False, f"崩成 KeyError: {e}")
except Exception as e:
    check("do_replay 缺 game 干净拒绝（不 KeyError 崩）", False, f"{type(e).__name__}: {e}")

bad_fix_path = SCHEMA_WORK / "bad_fix.json"
save_pack({k: v for k, v in pack.items() if k != "game"}, bad_fix_path)
try:
    md2, _ = do_fix("blocky-jump", str(bad_fix_path), "x", "y")
    check("do_fix 缺 game 干净拒绝（不裸崩）",
          ("game" in md2 or "结构" in md2) and "KeyError" not in md2, md2[:90])
except KeyError as e:
    check("do_fix 缺 game 干净拒绝（不裸崩）", False, f"崩成 KeyError: {e}")
except Exception as e:
    check("do_fix 缺 game 干净拒绝（不裸崩）", False, f"{type(e).__name__}: {e}")

print("== 6c. replay_repeat ingestion 同样干净拒绝坏包（堵 tools/replay_repeat.py:97 裸 json.loads 旁路） ==")
# 起因（两轮评审登记的非阻断 backlog P3-1/P2-1，本批闭合）：replay_repeat 用裸 json.loads 读包，
# 缺键坏包绕过 validate_pack，在 pack_id 读 pack["initial_state"]["seed"] 时 KeyError 裸崩。
# 修法：:97 改 load_pack(p) 与其余 ingestion 同口径。负控从 §0 真实包派生（缺 initial_state）；
# 正对照 stub 掉 replay_once（子进程/浏览器边界，fast 单元测试不可避）证好包仍被接受、不恒红。
import tools.replay_repeat as rr

RR_WORK = SCHEMA_WORK / "replay_repeat"
RR_WORK.mkdir(parents=True, exist_ok=True)
dummy_har = RR_WORK / "dummy.har"
dummy_har.write_bytes(b"dummy-har-bytes")
rr_out = RR_WORK / "repeat.json"

bad_rr = RR_WORK / "bad_no_initial.json"
save_pack({k: v for k, v in pack.items() if k != "initial_state"}, bad_rr)
try:
    rr.main(["--har", str(dummy_har), "--id", "test", "--pack", str(bad_rr),
             "--reps", "1", "--out", str(rr_out)])
    check("replay_repeat 坏包干净拒绝（不 KeyError 裸崩）", False, "竟然没拒绝坏包")
except ValueError as e:
    check("replay_repeat 坏包干净拒绝（不 KeyError 裸崩）", True, str(e)[:70])
except KeyError as e:
    check("replay_repeat 坏包干净拒绝（不 KeyError 裸崩）", False, f"崩成 KeyError: {e}")

good_rr = RR_WORK / "good.json"
save_pack(pack, good_rr)
_orig_replay_once = rr.replay_once
rr.replay_once = lambda p, h: {"rc": 0, "replayed": True, "initial_hash_matched": True,
                               "steps_matched": 1, "steps_total": 1, "hashes_match": True,
                               "mismatch": None, "stderr_tail": ""}
try:
    rc_good = rr.main(["--har", str(dummy_har), "--id", "test", "--pack", str(good_rr),
                       "--reps", "1", "--out", str(rr_out)])
    rr_data = json.loads(rr_out.read_text(encoding="utf-8"))
    check("replay_repeat 好包仍被接受（正对照，防恒红）",
          rc_good == 0 and rr_data["episodes"][0]["seed"] == pack["initial_state"]["seed"],
          f"rc={rc_good}")
except Exception as e:
    check("replay_repeat 好包仍被接受（正对照，防恒红）", False, f"{type(e).__name__}: {e}")
finally:
    rr.replay_once = _orig_replay_once

print()
if fails:
    print("M2 修复闭环未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M2 修复闭环全部通过")
