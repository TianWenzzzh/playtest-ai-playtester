# -*- coding: utf-8 -*-
r"""只读测量：**接入成本「行数」这句话的 referent 到底是几行**。

来历：仓库内外多处曾写「接入契约约 20 行」，文件名甚至叫 `integrate-in-20-lines.md`，
但从来没有一条读数量过它。11-06 深夜用一把尺量完 7 处 register 块，
total_lines 的 min/median/max = 11/29/48 —— **20 不在里面**。
于是 `tests/m7_outward_wording.py` 判据 5 直接 import 本模块现测，
门面里的成本数字必须落在这里量出来的集合里（限定词不算测量）。

口径（先写死再看读数，不许反过来调口径去凑某个数）：
  行数 = 从含 `window.__GSPP__.register({` 的那一行起，到其后第一个整行为 `});` 的那一行止，**含首尾**。
  同一口径另报「非空且非注释行」，因为散文里没说清用的是哪一种。
  `playtest/ui.py` 里那份是字符串常量（字面 `\n`），按 `\n` 切开后套同一口径。

设计为零接入的夹具（文件里自带「没有任何 GSPP 相关代码」的自述）单独归一类，
**不算抽取失败** —— 0 行是它的设计，不是解析坏了。判「没有 X」之前先量候选集。

本模块不写任何文件（除非显式给 --json）。ROOT 由 __file__ 定位，公开 clone 里照样能跑。

跑法：PYTHONUTF8=1 python tools/adapter_linecount.py [--json out/linecount.json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
START = "window.__GSPP__.register({"
END = "});"
ZERO_MARK = "没有任何 GSPP 相关代码"
RULE = "register({ 行 → 第一个整行 }); 行，含首尾；另报非空非注释行"


def is_comment(s: str) -> bool:
    return s.startswith("//") or s.startswith("/*") or s.startswith("*")


def measure(lines: list[str], i: int) -> dict | None:
    j = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == END), None)
    if j is None:
        return None
    body = lines[i:j + 1]
    code = [ln for ln in body if ln.strip() and not is_comment(ln.strip())]
    return {"start_line": i + 1, "end_line": j + 1,
            "total_lines": len(body), "code_lines": len(code)}


def scan_file(path: pathlib.Path, root: pathlib.Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rel = path.relative_to(root).as_posix()
    out = []
    for i, ln in enumerate(lines):
        # 字符串常量里的那份（ui.py）由下面的 \n 分支处理，否则这里会把一整行 Python
        # 字符串当成块首，永远等不到「整行 });」⇒ 假抽取失败 + 同一处数两遍。
        if START in ln.strip() and "```js" not in ln:
            m = measure(lines, i)
            out.append({"site": f"{rel}:{i + 1}", "kind": "block",
                        **(m or {"error": "closing anchor not found"})})
    if ZERO_MARK in text and not out:
        out.append({"site": f"{rel}:0", "kind": "zero_integration",
                    "total_lines": 0, "code_lines": 0})
    for mo in re.finditer(r'"```js\\nwindow\.__GSPP__\.register\(\{\\n', text):
        segs = text[mo.start():].split("\\n")
        end_k = next((k for k in range(1, len(segs)) if segs[k].strip().startswith(END)), None)
        if end_k is not None:
            body = segs[:end_k + 1]
            code = [s for s in body if s.strip() and not is_comment(s.strip())]
            out.append({"site": f"{rel}:str", "kind": "string_literal",
                        "total_lines": len(body), "code_lines": len(code)})
    return out


def targets(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted((root / "games").glob("*/index.html")) + [
        root / "README.md", root / "protocol" / "GSPP.md",
        root / "docs" / "integrate-in-20-lines.md", root / "playtest" / "ui.py"]


def measure_all(root: pathlib.Path = ROOT) -> dict:
    rows = []
    for p in targets(root):
        if not p.exists():
            rows.append({"site": p.name, "kind": "missing"})
            continue
        rows.extend(scan_file(p, root))
    blocks = [r for r in rows if r.get("kind") not in ("zero_integration", "missing")]
    failed = [r for r in blocks if "error" in r]
    ok = [r for r in blocks if "error" not in r]
    totals = [r["total_lines"] for r in ok]
    codes = [r["code_lines"] for r in ok]
    return {
        "rule": RULE,
        "sites_measured": len(blocks), "extraction_failures": len(failed),
        "per_site": rows,
        "total_lines": {"min": min(totals), "median": statistics.median(totals),
                        "max": max(totals)} if totals else {},
        "code_lines": {"min": min(codes), "median": statistics.median(codes),
                       "max": max(codes)} if codes else {},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="现测注册适配器的行数分布（只读）")
    ap.add_argument("--json", type=pathlib.Path, default=None, help="另存一份机读结果")
    args = ap.parse_args()
    s = measure_all(ROOT)
    for r in s["per_site"]:
        if r.get("kind") == "zero_integration":
            print(f"  {r['site']:<44} ZERO-INTEGRATION (designed) lines=0")
        elif r.get("kind") == "missing":
            print(f"  {r['site']:<44} MISSING")
        elif "error" in r:
            print(f"  {r['site']:<44} ERROR {r['error']}")
        else:
            print(f"  {r['site']:<44} lines={r['total_lines']:>3}  code={r['code_lines']:>3}")
    print(f"  rule = {s['rule']}")
    print(f"  N={s['sites_measured']} failures={s['extraction_failures']}")
    for k in ("total_lines", "code_lines"):
        v = s[k]
        print(f"  {k:<12} min/median/max = {v['min']}/{v['median']}/{v['max']}" if v
              else f"  {k:<12} n/a：没有可判对象")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved = {args.json}")
    # 判定行末行统一打印：空集合/抽取失败都不许靠 traceback 顶掉它。
    bad = s["extraction_failures"] or not s["total_lines"]
    print(f"adapter_linecount: {'FAIL' if bad else 'PASS'} "
          f"sites={s['sites_measured']} failures={s['extraction_failures']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())