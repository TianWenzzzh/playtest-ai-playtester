"""GSPP 复现包：录制、序列化、回放校验。

赛题 C 要求「输出初始状态、适用时的随机种子及完整动作序列」。
这个模块就是那句话的数据结构落地，外加一条不许糊过去的规矩：
复现包只有在**重放逐帧哈希全等**时才算有效，否则 verification.replayed = False。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from .env import BrowserEnv, Snapshot
from .hashing import digest

SCHEMA = "gspp/repro/1"


class TraceRecorder:
    def __init__(self, env: BrowserEnv, seed: int) -> None:
        self.env = env
        self.seed = int(seed)
        self.actions: list[dict[str, Any]] = []
        self.start = env.reset(seed)
        self.initial = {
            "seed": self.seed,
            "frame": self.start.frame,
            "hash": self.start.hash,
            "snapshot": self.start.data,
            "clock": self.start.clock,
            "rng": self.start.rng,
        }
        self._prev_hash = self.start.hash
        self._frames_matched_during_record = 0

    def step(self, action: str) -> Snapshot:
        snap = self.env.step(action)
        self.actions.append(
            {
                "i": len(self.actions),
                "action": action,
                "frame": snap.frame,
                "hash_before": self._prev_hash,
                "hash_after": snap.hash,
                "over": snap.over,
                "win": snap.win,
                "errors": snap.errors,
                "violations": snap.violations,
            }
        )
        self._prev_hash = snap.hash
        return snap

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "game": {
                "url": self.env.game_url,
                "probe_mode": self.env.mode,
                "meta": self.env._js("window.__GSPP__.meta()"),
            },
            "initial_state": self.initial,
            "actions": self.actions,
            "defect": defect,
            "verification": {"replayed": False, "reason": "尚未回放"},
            "evidence": {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        }


def replay(env: BrowserEnv, pack: dict[str, Any]) -> dict[str, Any]:
    """按复现包重跑，逐帧比哈希。返回 verification 块。

    这里刻意不复用录制时的那个 env 实例 —— 调用方必须新建一个 env，
    否则"关掉重开也能复现"这句话没有被真正测过。
    """
    seed = pack["initial_state"]["seed"]
    env.reset(seed)
    # 初始状态必须在动手之前取。放在循环之后读，读到的是终局，
    # 于是"初始状态一致"这条永远为假 —— 仪器自己把可复现的东西判成不可复现。
    initial_ok = env.read().hash == pack["initial_state"]["hash"]
    expected = pack["actions"]
    matched = 0
    mismatch = None

    for i, rec in enumerate(expected):
        snap = env.step(rec["action"])
        if snap.hash == rec["hash_after"]:
            matched += 1
        else:
            mismatch = {
                "i": i,
                "action": rec["action"],
                "expected": rec["hash_after"],
                "actual": snap.hash,
            }
            break

    total = len(expected)
    return {
        "replayed": bool(initial_ok and mismatch is None and matched == total),
        "initial_hash_matched": initial_ok,
        "steps_matched": matched,
        "steps_total": total,
        "hashes_match": mismatch is None,
        "mismatch": mismatch,
    }


def save_pack(pack: dict[str, Any], path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_pack(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pack_id(pack: dict[str, Any]) -> str:
    core = {
        "url": pack["game"]["url"],
        "seed": pack["initial_state"]["seed"],
        "actions": [a["action"] for a in pack["actions"]],
        "defect": pack["defect"]["signature"],
    }
    return digest(core).split(":")[1][:16]
