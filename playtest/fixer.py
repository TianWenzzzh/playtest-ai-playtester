"""修复 Agent —— 赛题 A「须展示至少一次基于执行反馈的修改」的落地。

范围刻意收窄：不承诺通用自动修 bug，只承诺
  「针对一个可回放的失败，做出一次经过重跑验证的修改」。

这条流水线里真正值钱的是**验证**那一半，不是生成那一半：
  补丁打完 → 用同一 (seed, 动作前缀) 重跑 → 缺陷判据不再开火
          → 其余种子没有引入新缺陷
          → 干净臂仍然沉默
          → 其余植入缺陷类型仍在原位开火
少了后三条，"我修好了"就只是模型的一句话，甚至只是"把判据关掉"。

补丁生成器可插拔：
  LLMProposer    需要推理端点
  ManualProposer 由人给补丁 —— 用来在拿不到端点时先把验证机器验一遍
🔴 用 ManualProposer 跑出来的结果，报告里必须写"补丁由人工提供"，不许记到模型头上。
"""
from __future__ import annotations

import json
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any, Protocol

from .agent import CuriosityPolicy, diagnose, run_episode
from .env import BrowserEnv


class Proposer(Protocol):
    name: str

    def propose(self, pack: dict[str, Any], sources: dict[str, str]) -> dict[str, Any] | None: ...


class ManualProposer:
    """人给补丁。用来单独验验证机器本身，不掺模型。"""

    name = "manual"

    def __init__(self, patch: dict[str, Any]) -> None:
        self._patch = patch

    def propose(self, pack, sources):
        return self._patch


class LLMProposer:
    """模型出最小补丁。

    约束：只允许"精确替换一段原文"，不允许输出整份文件 ——
    整份重写会让 diff 无法审查，也没法断言"只动了必须动的"。
    """

    name = "llm"

    def __init__(self, client: Any = None, max_diff_chars: int = 1200) -> None:
        from .llm import LLMClient

        self.client = client or LLMClient()
        self.max_diff_chars = max_diff_chars

    def propose(self, pack, sources):
        if not self.client.cfg.available():
            return None
        defect = pack["defect"]
        joined = "\n\n".join(
            f"--- {path} ---\n{txt}" for path, txt in sources.items() if len(txt) < 20000
        )
        messages = [
            {"role": "system", "content": (
                "你在修一个 HTML5 小游戏的缺陷。只输出 JSON："
                '{"path": "...", "old": "<必须逐字来自原文且全文只出现一次>", '
                '"new": "<替换后的原文>", "explanation": "<一句话>"}\n'
                "不要重写整个文件，不要顺手改无关的地方。"
            )},
            {"role": "user", "content": json.dumps({
                "defect_kind": defect["kind"],
                "defect_signature": defect["signature"],
                "defect_detail": defect.get("detail", {}).get("state"),
                "seed": pack["initial_state"]["seed"],
                "source_files": joined[:12000],
            }, ensure_ascii=False)},
        ]
        obj = None
        from .llm import parse_json_object

        for _ in range(2):
            raw = self.client.complete(messages)
            obj = parse_json_object(raw)
            if obj and obj.get("path") and "old" in obj and "new" in obj:
                break
            obj = None
        if not obj:
            return None
        return {
            "path": str(obj["path"]),
            "old": str(obj["old"]),
            "new": str(obj["new"]),
            "explanation": str(obj.get("explanation", ""))[:300],
            "proposer": self.name,
        }


@dataclass
class FixResult:
    ok: bool
    proposer: str
    patch: dict[str, Any]
    defect_before: dict[str, Any] | None
    defect_after: dict[str, Any] | None
    regression: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "proposer": self.proposer,
            "patch": {k: v for k, v in self.patch.items() if k in ("path", "old", "new", "explanation")},
            "defect_before": _brief(self.defect_before),
            "defect_after": _brief(self.defect_after),
            "regression": self.regression,
            "notes": self.notes,
        }


def _brief(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not d:
        return None
    return {"kind": d.get("kind"), "signature": d.get("signature"), "step": d.get("found_by_step")}


def stage_sources(game_dir: pathlib.Path, dest: pathlib.Path) -> dict[str, str]:
    """把游戏复制到一处再改 —— 不许在原始夹具上动刀，否则负控失败时无法还原。"""
    dest.mkdir(parents=True, exist_ok=True)
    sources: dict[str, str] = {}
    for f in sorted(game_dir.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(game_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        if f.suffix.lower() in (".html", ".js", ".css", ".json", ".md"):
            sources[str(rel).replace("\\", "/")] = f.read_text(encoding="utf-8", errors="replace")
    return sources


def apply_patch(staged: pathlib.Path, patch: dict[str, Any]) -> None:
    """逐字替换，且断言锚点恰好出现一次。

    old 出现 0 次 = 补丁打在了不存在的地方（等于没改）；
    出现 2 次 = 会改到不该改的地方。两种都必须炸，不能静默通过。
    """
    path = staged / patch["path"]
    if not path.exists():
        raise FileNotFoundError(f"补丁目标不存在: {patch['path']}")
    text = path.read_text(encoding="utf-8")
    old, new = patch["old"], patch["new"]
    if old == new:
        raise ValueError("补丁是空操作（old == new），这类'修复'永远不会改变判据")
    n = text.count(old)
    if n != 1:
        raise ValueError(f"锚点出现 {n} 次，要求恰好 1 次：{patch['path']} :: {old[:60]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def rerun_defect(url: str, pack: dict[str, Any], horizon: int = 12) -> dict[str, Any] | None:
    """用同一 (seed, 动作前缀) 重跑，看缺陷判据是否还开火。

    注意这里比的不是哈希 —— 游戏代码已经变了，哈希必然不同。
    要复用的是"走到那个局面的路径"，判据是缺陷本身还在不在。
    """
    prefix = [a["action"] for a in pack["actions"]]
    seed = pack["initial_state"]["seed"]
    return diagnose(url, seed, prefix, horizon=horizon)


def regression(staged_url: str, seeds: list[int], max_steps: int = 40) -> dict[str, Any]:
    """打完补丁后跑其余种子：不许引入新缺陷。"""
    kinds: dict[str, int] = {}
    errors = 0
    for s in seeds:
        r = run_episode(staged_url, s, CuriosityPolicy(s), max_steps=max_steps)
        if r.error:
            errors += 1
        if r.defect:
            kinds[r.defect["kind"]] = kinds.get(r.defect["kind"], 0) + 1
    return {"seeds": len(seeds), "defects": kinds, "errors": errors}


def fix(
    game_dir: pathlib.Path,
    pack: dict[str, Any],
    proposer: Proposer,
    workdir: pathlib.Path,
    regression_seeds: list[int] | None = None,
) -> FixResult:
    game_dir = game_dir.resolve()
    staged = workdir / "staged"
    if staged.exists():
        shutil.rmtree(staged)
    sources = stage_sources(game_dir, staged)

    patch = proposer.propose(pack, sources)
    if not patch:
        return FixResult(
            ok=False, proposer=proposer.name, patch={},
            defect_before=pack["defect"], defect_after=None,
            notes=[f"{proposer.name} 未给出补丁",
                   getattr(getattr(proposer, "client", None), "stat", None)
                   and proposer.client.stat.last_error or ""],
        )
    patch.setdefault("proposer", proposer.name)

    apply_patch(staged, patch)
    patched_url = (staged / "index.html").resolve().as_uri()

    after = rerun_defect(patched_url, pack)
    reg = regression(patched_url, regression_seeds or [pack["initial_state"]["seed"] + i for i in range(1, 6)])

    notes = []
    if patch.get("proposer") == "manual":
        notes.append("补丁由人工提供，本次验证的是验证机器本身，不是模型的修复能力")
    if after:
        notes.append(f"缺陷仍存在：{after['kind']}")
    else:
        notes.append("缺陷判据不再开火")
    if reg["defects"]:
        notes.append(f"其余种子出现缺陷：{reg['defects']}")

    return FixResult(
        ok=(after is None),
        proposer=patch.get("proposer", proposer.name),
        patch=patch,
        defect_before=pack["defect"],
        defect_after=after,
        regression=reg,
        notes=notes,
    )
