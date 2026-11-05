"""四套策略，同一把尺量。

基准表的意义就在于"同一环境、同一判据、四种策略"。
所以它们必须实现同一个 choose() 签名，且都不许偷看判据（不能因为想开火而挑动作）。
"""
from __future__ import annotations

import json
import random
from typing import Any

from .llm import LLMClient, parse_json_object

SYSTEM_PROMPT = (
    "你在替一个 HTML5 小游戏做自动试玩测试。目标不是赢，是**暴露缺陷**："
    "走到玩家会卡住、软锁、数值不一致或程序崩溃的地方。\n"
    "规则：只能从给定的 actions 里选一个 name；"
    "避开 known_dead_ends 里已经确认走不通的状态；"
    "优先去没见过的状态。\n"
    '只输出 JSON：{"reason": "<一句话>", "action": "<name>"}'
)


class RandomPolicy:
    name = "random"

    def __init__(self, seed: int = 0, **_: Any) -> None:
        self._r = random.Random(seed)

    def choose(self, actions, snap, seen, history):
        return self._r.choice(actions) if actions else "wait"

    def trace(self) -> dict[str, Any]:
        return {}


class CuriosityPolicy:
    """只按"这个动作试了几次"排序，不看游戏语义。下限基线。"""

    name = "curiosity"

    def __init__(self, seed: int = 0, **_: Any) -> None:
        self._r = random.Random(seed)
        self._trial: dict[str, int] = {}

    def choose(self, actions, snap, seen, history):
        if not actions:
            return "wait"
        counts = {a: self._trial.get(a, 0) for a in actions}
        lowest = min(counts.values())
        pick = self._r.choice([a for a, c in counts.items() if c == lowest])
        self._trial[pick] = counts[pick] + 1
        return pick

    def trace(self) -> dict[str, Any]:
        return {}


class SemanticPolicy:
    """不看模型、但看状态语义的启发式。

    它的存在是为了回答一个不好回答的问题：LLM 到底带来了什么？
    如果 LLM 打不过一个 30 行的启发式，那"AI 能力"那 25 分就是虚的。
    做法：优先选那些"能让目标相关变量发生变化"的动作 ——
    用变量名里含 goal/score/level/hp/key/exit/win 的字段变化来打分。
    """

    name = "semantic"
    GOAL_KEYS = ("goal", "score", "level", "progress", "key", "exit", "win", "reach", "hp")

    def __init__(self, seed: int = 0, **_: Any) -> None:
        self._r = random.Random(seed)
        self._value: dict[str, float] = {}
        self._last: dict[str, Any] | None = None

    def _signal(self, data: Any) -> float | None:
        if not isinstance(data, dict):
            return None
        flat = data.get("data") if isinstance(data.get("data"), dict) else data
        total = 0.0
        hit = 0
        for k in self.GOAL_KEYS:
            if k in flat and isinstance(flat[k], (int, float)) and not isinstance(flat[k], bool):
                total += float(flat[k])
                hit += 1
        return total if hit else None

    def choose(self, actions, snap, seen, history):
        if not actions:
            return "wait"
        sig = self._signal(snap.data)
        if sig is not None and self._last is not None and len(history) >= 1:
            self._value[history[-1]] = self._value.get(history[-1], 0.0) + (sig - (self._last or sig))
        self._last = sig
        ranked = sorted(actions, key=lambda a: -self._value.get(a, 0.0))
        best = self._value.get(ranked[0], 0.0)
        tied = [a for a in ranked if self._value.get(a, 0.0) == best]
        return self._r.choice(tied)

    def trace(self) -> dict[str, Any]:
        return {"action_value": {k: round(v, 3) for k, v in self._value.items()}}


class LLMPolicy:
    """模型选动作。不可用时如实降级，并把这个事实记在 trace 里。

    🔴 降级不是静默的：一旦 LLM 不可用，本策略就等价于 CuriosityPolicy，
    基准表里必须标注"这一列其实是 curiosity"，否则就是在用别人的名字报自己的成绩。
    """

    name = "llm"

    def __init__(self, seed: int = 0, client: LLMClient | None = None,
                 memory: bool = False, max_decisions: int = 400, **_: Any) -> None:
        self.client = client or LLMClient()
        self.memory = memory
        self._fallback = CuriosityPolicy(seed)
        self.decisions: list[dict[str, Any]] = []
        self.dead_ends: list[str] = []
        self._state_notes: dict[str, str] = {}
        self.llm_hits = 0
        self.llm_misses = 0
        self.max_decisions = max_decisions

    def _compact(self, data: Any) -> str:
        s = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        return s if len(s) <= 900 else s[:900] + "...(截断)"

    def choose(self, actions, snap, seen, history):
        if not actions:
            return "wait"

        if self.client.cfg.available() and len(self.decisions) < self.max_decisions:
            payload = {
                "state": self._compact(snap.data),
                "actions": actions,
                "steps_so_far": len(history),
                "distinct_states": len(seen),
                "recent_actions": history[-8:],
            }
            if self.memory:
                payload["known_dead_ends"] = self.dead_ends[-8:]
                payload["state_notes"] = list(self._state_notes.items())[-10:]
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            raw = self.client.complete(messages)
            obj = parse_json_object(raw)
            picked = obj.get("action") if obj else None
            if picked in actions:
                self.llm_hits += 1
                self.decisions.append({
                    "i": len(self.decisions), "action": picked,
                    "reason": str(obj.get("reason", ""))[:160], "source": "llm",
                })
                if self.memory:
                    self._state_notes[snap.hash[-12:]] = str(obj.get("reason", ""))[:80]
                return picked
            self.llm_misses += 1
            self.decisions.append({
                "i": len(self.decisions), "action": None,
                "reason": "模型给出的动作不在合法集合内，已回退", "source": "llm_invalid",
                "raw": str(picked)[:60],
            })

        pick = self._fallback.choose(actions, snap, seen, history)
        self.decisions.append({
            "i": len(self.decisions), "action": pick,
            "reason": self.client.stat.last_error or "回退到 curiosity", "source": "fallback",
        })
        return pick

    def note_dead_end(self, state_hash: str, kind: str) -> None:
        self.dead_ends.append(f"{kind}@{state_hash[-12:]}")

    def trace(self) -> dict[str, Any]:
        return {
            "llm_available": self.client.cfg.available(),
            "model": self.client.cfg.model,
            "llm_hits": self.llm_hits,
            "llm_misses": self.llm_misses,
            "calls": self.client.stat.as_dict(),
            "decisions": self.decisions[-40:],
            "note": (
                "LLM 全程不可用，本列实际等价于 curiosity"
                if self.client.cfg.available() is False or self.llm_hits == 0 else ""
            ),
        }


class LLMMemoryPolicy(LLMPolicy):
    """LLM + 死路记忆。用来回答"状态感知到底有没有用"。"""

    name = "llm_memory"
