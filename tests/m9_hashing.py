"""M9 哈希判据守卫：digest 是"逐帧可复现"这句话的唯一裁判，守住它的确定性。

普查（现读 2026-11-10，grep + 逐处读）：playtest.hashing.digest 的真实调用点只有三处，
全部喂 JSON 原生值 ——
  · playtest/env.py:423   digest(snap["data"])  ← page.evaluate 的 canonical（CDP 序列化边界 ⇒ 必 JSON 原生）
  · playtest/env.py:424   digest(snap["pos"])   ← 同上（或 None）
  · playtest/trace.py:244 digest(core)          ← {url:str, seed:int, actions:[str], defect:str}
所以 canonical_bytes 的 default=_fallback（set / bytes / repr 三支）当前不可达。

🔴 潜伏洞：_fallback 的 repr(o) 支带内存地址 —— 两个"值相等"的非原生对象会哈希出不同
结果。一旦哪天有调用方喂进非原生值，"可复现"会静默塌掉而不报任何错。本守卫不改 hashing.py
（普查证明不可达，硬化属投机），而是：① 钉住"JSON 原生 ⇒ 与键序无关的确定性"这条承重
不变量；② 用负控证明键序依赖确实可被分辨，让尺子不是恒绿；③ 把潜伏洞钉成可见 tripwire
（非原生值绝不许被静默判成"可复现相等"）；④ 钉住三处调用点的输入形状都是 JSON 原生。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m9_hashing.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.hashing import canonical_bytes, digest

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    print("== 1. 承重不变量：JSON 原生值 ⇒ 与键序无关、重复稳定的哈希 ==")
    a = {"z": 1, "a": {"y": [1, 2, "é"], "x": True}, "m": None}
    b = {"m": None, "a": {"x": True, "y": [1, 2, "é"]}, "z": 1}  # 同值，键序全反
    check("键序打乱后 digest 不变", digest(a) == digest(b),
          f"{digest(a)[-12:]} vs {digest(b)[-12:]}")
    check("重复调用 digest 稳定", digest(a) == digest(a))
    d = digest(a)
    check("digest 形状 = sha256: + 64 hex",
          d.startswith("sha256:") and len(d) == 71
          and all(c in "0123456789abcdef" for c in d[7:]), d[:20])

    print("== 2. 规范化字节的硬锚（sort_keys + 紧凑分隔符 + ensure_ascii=False）==")
    got = canonical_bytes({"b": 1, "a": "é"})
    check("canonical_bytes 逐字等于排序紧凑 UTF-8", got == b'{"a":"\xc3\xa9","b":1}', repr(got))

    print("== 3. 负控：尺子必须分辨得出'键序依赖'（否则第 1 段是恒绿空转）==")
    # 故意不排序的字节表示：同值不同键序 ⇒ 不同字节。这证明"键序无关"不是 vacuous ——
    # 哪天 canonical_bytes 掉了 sort_keys，第 1/2 段会当场红，因为这对差异是真实存在的。
    nosort_a = json.dumps(a, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nosort_b = json.dumps(b, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    check("不排序时同值异序确实产生不同字节（负控开火）", nosort_a != nosort_b,
          f"{len(nosort_a)}B vs {len(nosort_b)}B")
    check("而 canonical_bytes 把这对差异抹平（正对照）",
          canonical_bytes(a) == canonical_bytes(b))

    print("== 4. 潜伏洞 tripwire：非原生值绝不许被静默判成'可复现相等' ==")
    # 两个值相等的普通 object()：当前 repr 带 id ⇒ 哈希相异（潜伏洞可见）。
    # 若将来有人把 canonical_bytes 硬化成抛错，潜伏洞被关闭，同样合格。
    # 唯一判红的情形：非原生对象被静默哈希成"相等" —— 那等于裁判谎报可复现。
    o1, o2 = object(), object()
    try:
        d1, d2 = digest(o1), digest(o2)
        check("非原生 object() 不被静默判成可复现（repr 支哈希相异，潜伏洞可见）",
              d1 != d2, f"{d1[-12:]} vs {d2[-12:]}")
    except TypeError:
        check("非原生 object() 被干净拒绝（已硬化，潜伏洞关闭）", True, "TypeError")

    print("== 5. 普查钉桩：三处真实调用点的输入形状都是 JSON 原生（不需 _fallback）==")
    shapes = {
        "env.py:423 snap[data]": {"hp": 1, "dead": False, "level": 1, "score": 13,
                                  "items": ["a", "b"], "nested": {"k": None}},
        "env.py:424 snap[pos]": {"x": 12.5, "y": -3},
        "trace.py:244 core": {"url": "file:///g/index.html", "seed": 20261105,
                              "actions": ["jump", "jump"], "defect": "unwinnable@abc|horizon=12"},
    }
    for name, x in shapes.items():
        try:
            json.dumps(x, sort_keys=True)  # 无 default ⇒ 非原生会 TypeError
            native, err = True, ""
        except TypeError as exc:
            native, err = False, str(exc)
        check(f"{name} 是 JSON 原生（json.dumps 无需 default）", native, err)

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{'=' * 46}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:120]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())