"""探索主体：策略 + 主循环 + 缺陷落盘与回放校验。

架构上有一条不能让步的规矩：**探测会改变世界的，不许在录制轨迹的同时做**。
可达性探测要穷举动作、要推进虚拟时钟，如果它在录制循环里跑，
录下来的"动作序列"就不再等于重放时要执行的动作序列 —— 复现包当场作废，
而且是那种"本地全绿、评委一跑就红"的作废。
所以这里分成两步：先纯录制，再另开一个实例做诊断。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import detectors
from .env import BrowserEnv, Snapshot
from .policies import (CluePolicy, CuriosityPolicy, LLMMemoryPolicy, LLMPolicy,
                       OraclePolicy, RandomPolicy, SemanticPolicy, SweepPolicy)
from .trace import TraceRecorder, replay, save_pack

__all__ = [
    "CluePolicy", "CuriosityPolicy", "LLMMemoryPolicy", "LLMPolicy", "OraclePolicy",
    "RandomPolicy", "SemanticPolicy", "SweepPolicy",
    "EpisodeResult", "run_episode", "diagnose", "verify_pack", "sweep", "POLICIES",
]

POLICIES: dict[str, type] = {
    "random": RandomPolicy,
    "curiosity": CuriosityPolicy,
    "semantic": SemanticPolicy,
    "clue": CluePolicy,
    "oracle": OraclePolicy,
    "sweep": SweepPolicy,
    "llm": LLMPolicy,
    "llm_memory": LLMMemoryPolicy,
}


@dataclass
class EpisodeResult:
    seed: int
    policy: str
    steps: int
    frames: int
    distinct_states: int
    reached_over: bool
    reached_win: bool
    defect: dict[str, Any] | None = None
    pack: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    wall_ms: int = 0
    error: str | None = None
    probes: int = 0
    policy_trace: dict[str, Any] = None  # type: ignore[assignment]
    # 渲染后端报警（软件渲染时非空）。不参与任何哈希，只影响证据强度的表述。
    render_note: str = ""


def _trace(policy: Any) -> dict[str, Any]:
    trace = getattr(policy, "trace", None)
    if not callable(trace):
        return {}
    try:
        return dict(trace() or {})
    except Exception as exc:  # noqa: BLE001 - 策略自述失败不该拖垮整局
        return {"trace_error": f"{type(exc).__name__}: {exc}"}


def diagnose(
    game_url: str,
    seed: int,
    prefix: list[str],
    horizon: int = 12,
    headless: bool = True,
    offline: Any = None,
    har_path: str | None = None,
) -> dict[str, Any] | None:
    """新开一个实例，重放到 prefix 末端，再做可达性探测。

    单独开实例不是为了省事，是因为探测本身要消耗帧数并回滚状态 ——
    留在录制实例里就会污染那条要被复现的轨迹。
    """
    env = BrowserEnv(game_url, headless=headless, offline=offline, har_path=har_path)
    try:
        env.start()
        env.reset(seed)
        for a in prefix:
            env.step(a)
        snap = env.read()
        if snap.over:
            return None
        return detectors.probe_deadend(env, snap, horizon=horizon)
    finally:
        env.close()


def run_episode(
    game_url: str,
    seed: int,
    policy: Policy,
    max_steps: int = 120,
    stagnation_trigger: int = 4,
    probe_every: int = 5,
    horizon: int = 12,
    headless: bool = True,
    extra_defect_check: Callable[[Snapshot], dict[str, Any] | None] | None = None,
    offline: Any = None,
    har_path: str | None = None,
) -> EpisodeResult:
    t0 = time.time()
    env = BrowserEnv(game_url, headless=headless, offline=offline, har_path=har_path)
    recorder: TraceRecorder | None = None
    try:
        env.start()
        render_note = env.render_note
        recorder = TraceRecorder(env, seed)
        seen: set[str] = {recorder.start.hash}
        history: list[str] = []
        snap = recorder.start
        prev_errors = 0
        stale = 0
        novel_since_probe = 0
        probes = 0
        # 本局里"曾经起过作用"的动作集合。ui_dead_end 要靠它把
        # "这次没变化"升级成"从来没变化"，否则状态相关的合法空操作会被误报。
        ever_effective: set[str] = set()

        for step_i in range(max_steps):
            if snap.over:
                break

            action = policy.choose(env.actions(), snap, seen, history)
            before = snap
            snap = recorder.step(action)
            history.append(action)
            if snap.hash != before.hash:
                ever_effective.add(action)

            defect = detectors.check_step(env, snap, prev_errors)
            if not defect:
                defect = detectors.check_ui_dead_end(env, action, before, snap, ever_effective)
            if not defect and extra_defect_check:
                defect = extra_defect_check(snap)
            prev_errors = snap.errors

            if snap.hash in seen:
                stale += 1
            else:
                seen.add(snap.hash)
                stale = 0
                novel_since_probe += 1

            should_probe = (stale >= stagnation_trigger) or (novel_since_probe >= probe_every)
            if not defect and should_probe and not snap.over:
                probes += 1
                novel_since_probe = 0
                stale = 0
                defect = diagnose(game_url, seed, list(history), horizon=horizon,
                                  headless=headless, offline=offline)

            if defect:
                defect["found_by_step"] = step_i
                defect["detail"] = {**defect.get("detail", {}), "witness_prefix": list(history)}
                if hasattr(policy, "note_dead_end"):
                    policy.note_dead_end(snap.hash, defect["kind"])
                result = EpisodeResult(
                    seed=seed, policy=policy.name, steps=len(recorder.actions), frames=snap.frame,
                    distinct_states=len(seen), reached_over=snap.over, reached_win=snap.win,
                    defect=defect, probes=probes, policy_trace=_trace(policy),
                    render_note=render_note,
                )
                result.pack = recorder.record_defect(defect)
                result.wall_ms = int((time.time() - t0) * 1000)
                return result

        result = EpisodeResult(
            seed=seed, policy=policy.name, steps=len(recorder.actions), frames=snap.frame,
            distinct_states=len(seen), reached_over=snap.over, reached_win=snap.win,
            defect=None, probes=probes, policy_trace=_trace(policy),
            render_note=render_note,
        )
        # 🔴 尸检：局以"输"结束不等于局面本来可赢。
        # 实测漏报就是这么来的 —— blocky-jump 第 5 层间隙超限，角色连摔三次致死，
        # episode 因为 over 提前结束，可达性探测一次都没跑，缺陷被判成"没有缺陷"。
        # 死亡会把不可通关掩盖掉，而"会死"和"赢不了"是两件必须分开说的事。
        if snap.over and not snap.win and recorder.actions:
            probes += 1
            result.probes = probes
            prefix = [a["action"] for a in recorder.actions][:-1]
            if prefix:
                post = diagnose(game_url, seed, prefix, horizon=horizon,
                                headless=headless, offline=offline)
                if post:
                    post["found_by_step"] = len(prefix)
                    post["detail"] = {**post.get("detail", {}),
                                      "witness_prefix": list(prefix),
                                      "post_mortem": True,
                                      "note": post["detail"].get("note", "") +
                                              "（本局以失败告终，尸检确认此前已无赢的路径）"}
                    result.defect = post
                    result.pack = recorder.record_defect(post)
        result.wall_ms = int((time.time() - t0) * 1000)
        return result
    except Exception as exc:  # noqa: BLE001 - 探索器不能因为一个游戏炸掉整批评测
        return EpisodeResult(
            seed=seed, policy=policy.name, steps=0, frames=0, distinct_states=0,
            reached_over=False, reached_win=False,
            error=f"{type(exc).__name__}: {exc}", wall_ms=int((time.time() - t0) * 1000),
        )
    finally:
        env.close()


def verify_pack(game_url: str, pack: dict[str, Any], headless: bool = True,
                offline: Any = None) -> dict[str, Any]:
    """新开一个浏览器实例重放 —— 不复用录制时的进程，否则"关掉重开也能复现"没被真的测。"""
    env = BrowserEnv(game_url, headless=headless, offline=offline)
    try:
        env.start()
        return replay(env, pack)
    finally:
        env.close()


def sweep(
    game_url: str,
    seeds: list[int],
    policy_factory: Callable[[int], Policy],
    out_dir: Path,
    max_steps: int = 120,
    headless: bool = True,
    offline: Any = None,
) -> dict[str, Any]:
    """一个游戏 × 多个种子扫一遍，落盘所有开火的复现包。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for seed in seeds:
        res = run_episode(game_url, seed, policy_factory(seed), max_steps=max_steps,
                          headless=headless, offline=offline)
        row: dict[str, Any] = {
            "seed": seed, "policy": res.policy, "steps": res.steps, "frames": res.frames,
            "distinct_states": res.distinct_states, "reached_over": res.reached_over,
            "reached_win": res.reached_win, "wall_ms": res.wall_ms, "probes": res.probes,
            "policy": res.policy, "policy_trace": res.policy_trace or {},
            "defect": None, "pack_id": None, "replayed": None, "error": res.error,
            "render_note": res.render_note,
        }
        if res.defect:
            row["defect"] = {"kind": res.defect["kind"], "signature": res.defect["signature"]}
            if res.pack:
                v = verify_pack(game_url, res.pack, headless=headless, offline=offline)
                res.pack["verification"] = v
                from .trace import pack_id

                pid = pack_id(res.pack)
                save_pack(res.pack, out_dir / f"{res.policy}-{seed}-{pid}.json")
                row["pack_id"] = pid
                row["replayed"] = v["replayed"]
        rows.append(row)
    return {"game": game_url, "episodes": rows}
