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


class SweepPolicy:
    """轮转把每个动作都试一遍 —— ui_dead_end 类判据的正向对照。

    🔴 为什么 oracle 不能当这个对照：oracle 恒选"语义上最正确"的那一个动作，
    于是它永远不会去点那个没接逻辑的按钮，判据自然不开火。
    这不是检测器坏了，是**对照用错了类**：
      · softlock / unwinnable 的瓶颈是"走得到那个深度" ⇒ 对照要会走 ⇒ oracle
      · ui_dead_end 的瓶颈是"每个控件都点一遍" ⇒ 对照要会扫 ⇒ sweep
    混用一个对照，会把"对照本身不适配"误读成"仪器有缺陷"。
    """

    name = "sweep"

    def __init__(self, seed: int = 0, **_: Any) -> None:
        self._count: dict[tuple, int] = {}

    def choose(self, actions, snap, seen, history):
        # 🔴 覆盖必须按"动作集签名"分别计数。
        # 第一版用全局计数器 i % len(actions)，实测 0/3 不开火：
        # 界面一切换（进商店后按钮集合变了），索引就对不上原来那套动作，
        # 那个坏按钮永远轮不到。
        if not actions:
            return "wait"
        sig = tuple(actions)
        i = self._count.get(sig, 0)
        self._count[sig] = i + 1
        return actions[i % len(actions)]

    def trace(self) -> dict[str, Any]:
        return {"note": "覆盖型正向对照，不参与策略排名",
                "distinct_action_sets": len(self._count)}


class OraclePolicy:
    """总是走"语义上正确"的那扇门 —— 不读任何线索，硬编码第 0 项。

    🔴 它是**检测器的正向对照**，不是被评测的策略。
    作用是把两个问题分开：
      · 仪器能不能判出第 17 层的封死门？—— 用 oracle 走到那里，看它开不开火。
      · 策略能不能走到那里？—— 用其余五列去量。
    如果 oracle 也 0，那说明检测器有问题；如果 oracle 全中而别的策略全 0，
    那说明瓶颈在语义能力，不在仪器。混在一起就永远分不清是哪种失败。
    """

    name = "oracle"

    def __init__(self, seed: int = 0, **_: Any) -> None:
        pass

    def choose(self, actions, snap, seen, history):
        return actions[0] if actions else "wait"

    def trace(self) -> dict[str, Any]:
        return {"note": "检测器正向对照，不参与策略排名"}


class CluePolicy:
    """读门上的中文线索选动作 —— 不依赖任何模型的语言基线。

    🔴 它的存在是为了让基准表有分辨力，也是为了给 LLM 臂立一个可测的靶：
    如果一个小词表启发式就能读对线索，那"LLM 带来语义理解"这个主张在这个夹具上
    就不成立，得换更难的语言材料。反过来，如果只有它能到第 17 层，
    而 random/curiosity 全 0，这张表才第一次量出了差别。

    带失败记忆：实测第一版没有记忆，在同一扇错门上连撞三次把命耗尽 ——
    而"反馈修正"本身就是评分表里明写的能力项，不记住失败的策略不配谈修正。
    """

    name = "clue"
    NEGATIVE = ("封", "死", "锈", "断", "没", "旧", "碎", "停", "画", "塌", "弯",
                "蛛网", "霉", "发闷", "发乌", "焊", "塞", "拉着", "夹着", "结", "积灰",
                "水渍", "一堵墙", "只", "厚", "冰凉", "朝外")
    POSITIVE = ("风", "灯", "声", "温", "光", "脚印", "流水", "新", "开着", "延伸",
                "响", "天光", "火光", "钟表在走", "阳台", "有人", "上来", "透出")

    def __init__(self, seed: int = 0, **_: Any) -> None:
        self._r = random.Random(seed)
        self._failed: set[str] = set()
        self._last: tuple[str, str] | None = None

    def _score(self, label: str) -> int:
        s = 0
        for w in self.POSITIVE:
            if w in label:
                s += 1
        for w in self.NEGATIVE:
            if w in label:
                s -= 2
        return s

    def choose(self, actions, snap, seen, history):
        if not actions:
            return "wait"
        # 上一步掉了命 ⇒ 那个动作在当前层是错的，记住它
        if self._last and len(history) and history[-1] == self._last[1]:
            before = self._last[0]
            if isinstance(snap.data, dict) and isinstance(before, dict):
                if snap.data.get("lives", 0) < before.get("lives", 0):
                    self._failed.add(f"{before.get('floor')}:{self._last[1]}")
        data = snap.data if isinstance(snap.data, dict) else {}
        doors = data.get("doors") or []
        floor = data.get("floor")
        self._last = (data, None)
        if not doors:
            return self._r.choice(actions)

        scored = []
        for i, a in enumerate(actions):
            label = doors[i] if i < len(doors) else ""
            s = self._score(label)
            if f"{floor}:{a}" in self._failed:
                s -= 100
            scored.append((s, a))
        best = max(x[0] for x in scored)
        tied = [x[1] for x in scored if x[0] == best]
        pick = self._r.choice(tied)
        self._last = (data, pick)
        return pick

    def trace(self):
        return {"lexicon_positive": len(self.POSITIVE), "lexicon_negative": len(self.NEGATIVE),
                "failed_marked": sorted(self._failed)}


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
