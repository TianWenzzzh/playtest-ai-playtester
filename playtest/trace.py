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
            # 持久状态单独存一份：真实应用的进度落在 localStorage 里，种子覆盖不到它。
            # 复现包不自带这份，拿到别人机器上就装不回同一个界面。
            "persistent": env.persistent_state(),
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
    # 先干净起，再决定要不要把复现包里的持久状态装回去
    env.reset(seed, fresh_storage=True)
    baseline = env.read().hash
    persistent = pack["initial_state"].get("persistent")
    has_storage = bool(persistent and (persistent.get("localStorage")
                                       or persistent.get("sessionStorage")))
    injected = False
    if has_storage:
        env.load_persistent_state(persistent)
        # 🔴 装上去之后局面没变，说明包里那份存档就是"干净开机自己会写出来的那份"——
        # 这时什么都不该再做。原先这里无条件再推进一段帧，等于给回放加了一段
        # 录制里不存在的时间；实测真实作品因此在第 18 步（进入冒险那一下）分歧。
        # 真变了才 reboot：重载一次页面，让应用**在开机时**读到这份存档，
        # 否则游戏内存里仍是旧的，后面任何一次读档都会走偏。
        if env.read().hash != baseline:
            env.reboot(seed)
            injected = True
    # 初始状态必须在动手之前取。放在循环之后读，读到的是终局，
    # 于是"初始状态一致"这条永远为假 —— 仪器自己把可复现的东西判成不可复现。
    initial_ok = env.read().hash == pack["initial_state"]["hash"]
    expected = pack["actions"]
    matched = 0
    mismatch = None

    for i, rec in enumerate(expected):
        snap = env.step(rec["action"])
        # 🔴 复现包记的是"这一步读到第几帧"，回放就要补到那一帧再比。
        # 不能假设两边推进量一样：录制时检测器自己也会推进帧
        # （ui_dead_end 要空推 40 帧确认"真的没反应"），
        # 动画/飘字类界面差 1 帧就对不上，而少推进的那段是**观测的成本**，
        # 不是游戏的动作 —— 把它记进时间线里才是诚实的做法。
        want = rec.get("frame")
        if want is not None:
            drift = int(want) - int(snap.frame)
            if drift > 0:
                env._advance(drift)
                snap = env.read()
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
    replayed = bool(initial_ok and mismatch is None and matched == total)
    return {
        "replayed": replayed,
        "initial_hash_matched": initial_ok,
        "steps_matched": matched,
        "steps_total": total,
        "hashes_match": mismatch is None,
        "mismatch": mismatch,
        "storage_injected": injected,
        "determinism_class": _classify(env, replayed, initial_ok, persistent),
    }


def _classify(env: BrowserEnv, replayed: bool, initial_ok: bool, persistent: Any) -> dict[str, Any]:
    """给这份证据定级。

    🔴 复现失败有几种完全不同的成因，混成一个 false 会误导使用者：
      · 持久状态没装回去 —— 补上 persistent 就能复现；
      · 应用有异步依赖（promise / React 调度器 / 网络响应顺序）。
        🔴 这一条只有在"同一条回放路径重跑两次仍然分歧"时才允许写。
        11-05 夜实测：先前归给异步的分歧，真因是**回放路径与录制路径不一致**
        （无条件装载持久状态并多推一段帧、且没按包里记录的帧时间线补齐）。
        归因归错了，比不归因更糟——它会让人放弃一个其实可以闭合的判据。
    真正的异步依赖不是包坏了，是证据强度本来就低一档 —— 它仍然证明"这个缺陷存在过"，
    只是不能保证"逐帧重放必然再现"。必须自己说清楚，别等别人去发现。
    """
    if replayed:
        return {"level": "bit_reproducible",
                "claim": "同一 (初始状态, 种子, 动作序列) 在新实例里逐帧哈希全等"}
    if not initial_ok:
        return {"level": "unverified",
                "claim": "连初始状态都对不上 —— 这份包不能作为证据使用，先查探针注入与 frame 解析"}
    storage_present = bool(persistent and (persistent.get("localStorage") or persistent.get("sessionStorage")))
    return {
        "level": "async_dependent",
        "claim": "缺陷存在过（初始状态一致、前缀可重放），但同一回放路径重跑仍在此处分歧，"
                 "因此不保证位级再现",
        "storage_captured": storage_present,
        "not_caused_by": "离线包覆盖不全 —— 未命中为 0、命中全在 exact 档时才会走到这一级",
        "first_check": "先确认录制与回放走的是同一条路径（观测不得推进录制实例的时钟、"
                       "回放要按包里记录的帧时间线补齐）——这两处修好后，"
                       "真实第三方作品 26/26 与 23/23 步逐帧全等，曾是这一级的那两份包升到 bit_reproducible",
        "mitigation": "要拿到 bit_reproducible：游戏侧注册 GSPP adapter 并提供 snapshot/restore，"
                      "或把页面与资源取到本地离线跑（capture --har）",
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
