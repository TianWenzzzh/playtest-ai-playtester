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

print()
if fails:
    print("M2 修复闭环未通过:", len(fails), "项 ->", "; ".join(fails))
    sys.exit(1)
print("M2 修复闭环全部通过")
