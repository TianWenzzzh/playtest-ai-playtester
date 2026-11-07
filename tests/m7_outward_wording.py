r"""对外措辞闸门：无界量词、裸成本数字、以及**没量过的成本数字**都不许出现在门面里。

来历一：`06` §10.23 现读发现「任意 HTML5 游戏 20 行接入」这句无界声明落在
README 首行、定位句与 GSPP 里，而"任意"从没测过 —— 三件外部作品全是**零改动**取的包。

来历二（11-07，判据 5）：上一版把「中位数 / 约 / 契约 / 示例」当限定词放行，于是
`protocol/GSPP.md:94`「接入成本目标：中位数 20 行」被判为**正确写法**。它是错的：
`raw/audit/2026-11-06_adapter_linecount.json` 用一把尺现测 7 处 register 块，
total_lines 的 min/median/max = 11/29/48 —— **20 从来没被量出来过**。
🔴 教训：**限定词不是测量**。"中位数"只说明这句话打算描述一个统计量，
不说明那个统计量真的算过。所以判据 5 的允许集合由仓库里那把尺**当场现测**派生，
不钉字面量；门面里任何成本类行数不在集合里就红。

判据前先量过命中分布（`raw/audit/2026-11-06_gate20line_probe.md` 与 11-07 的上下文词普查）：
成本上下文词（接入/契约/注册/降级）+ `(\d+)\s*行` 在四个门面里命中 12 处，
其中 8 处是 `20`（缺陷）、4 处是 `0`（零接入，实测就是 0）。
`README.md:140`「45 行启发式」是**另一个量**（策略启发式长度），上下文词不命中 ⇒ 必须静。
所以这里**不写**"全库不许出现 任意 / 20 行"那种宽句式 ——
它会在 `README.md:60` 的反向免责（"不是我们保证任意环境下逐帧一致"）上开火。
红在合法样本上的规则比没规则更坏：它教会所有人忽略这条规则。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m7_outward_wording.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []

# 无界量词的精确形状：只禁这两种，"任意环境""任意一个"都是合法中文。
FORBIDDEN = ("任意 HTML5 游戏", "任意既有 HTML5 游戏")
# "20 行" 必须被限定成契约/目标，而不是当实测改动量卖。
# 🔴 判据 2 只管"有没有限定词"；限定词齐不齐由判据 5 接着管（量过没有）。
QUALIFIERS = ("契约", "中位数", "约", "示例")
# ui.py 是评委在 Gradio「接入与限制」页上看到的那句话，必须一起管。
# 扩面之前先量过：ui.py 里 FORBIDDEN 两串命中 0 次，唯一的「20 行」就是要修的那处。
SCOPE_GLOBS = ("README.md", "protocol/*.md", "docs/integrate-in-20-lines.md", "playtest/ui.py")
# 下限 4 = 现读的 glob 结果（README.md / GSPP.md / integrate-in-20-lines.md / ui.py），不是许愿值。
# 它的用途只有一个：抽取器坏掉、受管集合变空时不许在 0 行上打印 ok。
MIN_FILES = 4

# 判据 5：成本类数字必须落在现测集合里。上下文词把"另一个量"排除在管辖之外。
COST_CTX = ("接入", "契约", "注册", "降级")
# 区间两端都要管：只咬上界的话，「5–48 行」里没量过的 5 会被合法的 48 掩护过去。
COST_NUM = re.compile(r"(\d+)(?:\s*[–—~\-]\s*(\d+))?\s*行")
INSTRUMENT = ROOT / "tools" / "adapter_linecount.py"
REPRO_CMD = "tools/adapter_linecount.py"

# 判据 6：门面引用的路径，公开 clone 必须拿得到（或点名生产者）。
# 来历（11-07）：把抄本计数换成指针时，换成了 clone 里不存在的指针。
# 先量过命中分布：raw/audit/face_path_probe_1107.py ⇒ 6 个门面 45 处引用，
# ABSENT=2 / GENERATED=5 / OK=38。真缺陷只有 2 处（README.md:238、deploy/dgx-spark/README.md:140），
# 其余必须静。deploy:139 我一度手判为缺陷，量过才知道它合法：同文件 74 行已点名生产者。
# 🔴 探针首版假阳性 4 条：deploy/dgx-spark/README.md 里的 `bench.py`/`serve.sh`
# 是**相对该目录**的引用（真实存在）。所以解析顺序是"先引用文件所在目录、再仓根"，
# 下面有一条正对照专门守这件事 —— 拿首版读数去改门面会删掉 4 处合法引用。
FACE_GLOBS = ("README.md", "docs/*.md", "protocol/*.md", "deploy/**/*.md", "playtest/ui.py")
# 下限 6 = 现读 glob 结果，不是许愿值；抽取器坏了/受管面变空时不许在 0 处引用上打 ok。
MIN_FACES = 6
CITE = re.compile(r"`([A-Za-z0-9_./\-]+\.(?:md|py|json|log|html|har|txt|sh)|[A-Za-z0-9_./\-]+/)`")
# 生成物 → 同一文件里必须点名的生产者。派生自实际写它的那行代码：
# benchmark/run.py:128 写 out/benchmark.json；tools/certify.py 写 evidence/latest.json。
# 只放真被引用的两项，不投机预留。
GENERATED_BY = {
    "evidence/latest.json": ("tools/certify.py",),
    "out/benchmark.json": ("benchmark/run.py", "benchmark.run"),
}
DISCLOSURE = "gitignore"


def face_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for pat in FACE_GLOBS:
        found.extend(p for p in sorted(ROOT.glob(pat)) if "superpowers" not in p.parts)
    return found


def ignore_prefixes() -> list[str]:
    """从 .gitignore 取字面条目当前缀集。

    刻意不调 git：源码 tarball 里没有 .git，判据仍要能跑。
    带通配符的条目（*.pyc / *.egg-info/）不参与前缀匹配 —— 它们不会出现在门面引用里。
    """
    gi = ROOT / ".gitignore"
    if not gi.is_file():
        return []
    return [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
            and "*" not in ln and "?" not in ln]


def classify_cite(f: pathlib.Path, cite: str, prefixes: list[str]) -> str:
    # 先判生成物：out/benchmark.json 在本机存在，但 clone 里没有。
    if any(cite.startswith(p) for p in prefixes):
        return "GENERATED"
    if (f.parent / cite).exists() or (ROOT / cite).exists():
        return "OK"
    return "ABSENT"


def path_hits(files, prefixes) -> tuple[list[str], list[str]]:
    """判据 6：返回 (ABSENT 命中, 生成物没给生产者/没披露 的命中)。红要红在正确条目上。"""
    absent: list[str] = []
    unproduced: list[str] = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        text = f.read_text(encoding="utf-8")
        for i, ln in enumerate(text.splitlines(), 1):
            for m in CITE.finditer(ln):
                cite = m.group(1)
                if cite.startswith(("http", "www.", "../")):
                    continue
                kind = classify_cite(f, cite, prefixes)
                if kind == "ABSENT":
                    absent.append(f"{rel}:{i} `{cite}` 在公开 clone 里不存在（工作区件不许当证据引）")
                elif kind == "GENERATED":
                    tokens = GENERATED_BY.get(cite)
                    if tokens is not None:
                        # 生产者按文件级判：README:318 那行只写"由认证器生成"没写路径，
                        # 行级判会红在合法样本上 —— 那样的规则比没规则更坏。
                        if not any(t in text for t in tokens):
                            unproduced.append(f"{rel}:{i} `{cite}` 是生成物，同文件没点名生产者 {tokens}")
                    elif DISCLOSURE not in text:
                        unproduced.append(f"{rel}:{i} `{cite}` 是未分类生成物：补进 GENERATED_BY 或显式披露 gitignore")
    return absent, unproduced


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def scope_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for pat in SCOPE_GLOBS:
        found.extend(sorted(ROOT.glob(pat)))
    return found


def unbounded_hits(files) -> list[str]:
    """判据 1：精确串命中，返回 file:line 列表（返回列表而不是计数，红要红在正确条目上）。"""
    hits = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for needle in FORBIDDEN:
                if needle in line:
                    hits.append(f"{f.name}:{i} 含 {needle}")
    return hits


def bare_cost_hits(files) -> list[str]:
    """判据 2：出现「20 行」却没有任何限定词的行。"""
    hits = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "20 行" in line and not any(q in line for q in QUALIFIERS):
                hits.append(f"{f.name}:{i} {line.strip()[:60]}")
    return hits


def load_measured() -> dict | None:
    """现测，不抄常量：允许集合由仓库里那把尺当场量出来（公开 clone 也能跑）。"""
    if not INSTRUMENT.is_file():
        return None
    spec = importlib.util.spec_from_file_location("adapter_linecount", INSTRUMENT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter_linecount"] = mod
    spec.loader.exec_module(mod)
    return mod.measure_all(ROOT)


def allowed_numbers(summary: dict) -> set[int]:
    vals = {0}  # 0 = 零接入兜底；clicker-quest 的设计就是 0 行，实测归类为 zero_integration
    for r in summary["per_site"]:
        for k in ("total_lines", "code_lines"):
            if isinstance(r.get(k), int):
                vals.add(r[k])
    for k in ("total_lines", "code_lines"):
        vals.update(int(v) for v in summary[k].values())
    return vals


def cost_lines(f: pathlib.Path) -> list[tuple[int, str]]:
    return [(i, ln) for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if any(c in ln for c in COST_CTX) and COST_NUM.search(ln)]


def unmeasured_cost_hits(files, allowed: set[int]) -> list[str]:
    """判据 5：成本上下文里的行数不在现测集合里。返回 file:line，红要红在正确条目上。"""
    hits = []
    for f in files:
        for i, ln in cost_lines(f):
            for m in COST_NUM.finditer(ln):
                for g in (m.group(1), m.group(2)):
                    if g is not None and int(g) not in allowed:
                        hits.append(f"{f.name}:{i} 「{m.group(0).strip()}」里的 {g} "
                                    f"不在现测集合 {sorted(allowed)}")
    return hits


def main() -> int:
    print("\n== 1. 管辖面：glob 派生 + 防空转下限 ==")
    files = scope_files()
    names = [f.name for f in files]
    check(f"受管文件 ≥ {MIN_FILES} 个（少于下限说明抽取器坏了，判红而不是在 0 行上打 ok）",
          len(files) >= MIN_FILES, f"{len(files)} 个: {names}")
    check("每个受管文件都真的读得到", all(f.is_file() for f in files), str(len(files)))

    print("\n== 2. 判据自身会开火：该红的那份必须红，该静的那份必须静 ==")
    # 两份合成文件分开写：混在一份里，"合法样本保持沉默"那条会被植入句带红 ——
    # 那测的就不是判据的精确性，而是我自己把两件事写在了一起。
    bad = ROOT / "out" / "_m7_bad.md"
    good = ROOT / "out" / "_m7_good.md"
    tmps = (bad, good)
    bad.parent.mkdir(exist_ok=True)
    try:
        bad.write_text("开源 · 任意 HTML5 游戏 20 行接入（或 0 行降级接入）\n", encoding="utf-8")
        good.write_text(
            "不是我们保证任意环境下逐帧一致\n"
            "**接入成本目标：中位数 20 行。**\n"
            "### 模式一 · 注册（高保真，约 20 行）\n"
            "接入契约约 20 行，示例见本文件\n",
            encoding="utf-8")
        check("🔴 判据 1 在植入句上开火", len(unbounded_hits([bad])) == 1,
              str(unbounded_hits([bad])))
        check("🔴 判据 2 在裸『20 行』上开火", len(bare_cost_hits([bad])) == 1,
              str(bare_cost_hits([bad])))
        check("判据 1 对『任意环境』的反向免责保持沉默（精确串，不是宽句式）",
              unbounded_hits([good]) == [], str(unbounded_hits([good])))
        check("判据 2 对『中位数 / 约 / 契约 / 示例』四种限定都保持沉默",
              bare_cost_hits([good]) == [], str(bare_cost_hits([good])))
    finally:
        for f in tmps:
            f.unlink(missing_ok=True)

    print("\n== 3. 真实门面文件必须干净 ==")
    real1 = unbounded_hits(files)
    check("仓库门面里没有无界量词", len(real1) == 0, "; ".join(real1))
    real2 = bare_cost_hits(files)
    check("门面里每个「20 行」都被限定成契约/目标", len(real2) == 0, "; ".join(real2))

    print("\n== 4. 接线：只活在测试文件里的判据等于没写 ==")
    certify = (ROOT / "tools" / "certify.py").read_text(encoding="utf-8")
    check("m7 已进 tools/certify.py 的步骤清单（fast 档就会跑它）",
          "tests/m7_outward_wording.py" in certify,
          f"命中 {certify.count('tests/m7_outward_wording.py')} 次")

    print("\n== 5. 成本类数字必须量过：限定词不是测量 ==")
    check("🔴 测量仪器在仓库里（公开 clone 必须能自己现读，不许只引仓外 raw/）",
          INSTRUMENT.is_file(), REPRO_CMD)
    summary = load_measured()
    check("仪器能现测且零抽取失败",
          summary is not None and summary["extraction_failures"] == 0,
          "仪器缺失 ⇒ 无法现测" if summary is None
          else f"N={summary['sites_measured']} failures={summary['extraction_failures']}")
    targets = len(list(ROOT.glob("games/*/index.html"))) + 4
    zero = sum(1 for r in (summary or {}).get("per_site", [])
               if r.get("kind") == "zero_integration")
    check(f"现测覆盖全部 {targets} 个目标文件（块 + 零接入 ≥ 目标数，防静默漏一处）",
          summary is not None and summary["sites_measured"] >= 1
          and summary["sites_measured"] + zero >= targets,
          "仪器缺失" if summary is None else f"{summary['sites_measured']} 块 + {zero} 零接入")

    allowed = allowed_numbers(summary) if summary else set()
    # 与第 2 段同一形状：bad/good 分开写，否则"合法样本必须静"会被植入句带红。
    bad2 = ROOT / "out" / "_m7_cost_bad.md"
    good2 = ROOT / "out" / "_m7_cost_good.md"
    bad3 = ROOT / "out" / "_m7_cost_range.md"
    bad2.parent.mkdir(exist_ok=True)
    try:
        # 这句限定词齐全 ⇒ 判据 2 对它沉默。判据 5 必须抓住它，这正是上一版漏掉的那一类。
        bad2.write_text("**接入成本目标：中位数 20 行。**\n", encoding="utf-8")
        med = int(summary["total_lines"]["median"]) if summary else 0
        good2.write_text(
            f"注册适配器实测中位数 {med} 行；现读：python {REPRO_CMD}\n"
            "| `semantic` | 45 行启发式（实测类跨度） |\n"
            "### 模式二 · DOM/Canvas 兜底（0 行接入）\n",
            encoding="utf-8")
        check("🔴 判据 5 在『中位数 20 行』上开火，而判据 2 对它沉默（限定词≠量过）",
              summary is not None and len(unmeasured_cost_hits([bad2], allowed)) == 1
              and bare_cost_hits([bad2]) == [],
              str(unmeasured_cost_hits([bad2], allowed)))
        check("判据 5 对现测中位数 / 另一个量的『45 行启发式』/ 零接入『0 行』都沉默",
              summary is not None and unmeasured_cost_hits([good2], allowed) == [],
              str(unmeasured_cost_hits([good2], allowed)))
        # 区间下界必须一起管：只咬上界的判据等于半个判据，而门面里写的正是区间。
        bad3.write_text("注册适配器实测 5–48 行（中位数 29）\n", encoding="utf-8")
        check("🔴 判据 5 咬住区间**下界**（5–48 里的 5 没量过，上界合法不许掩护它）",
              summary is not None
              and any("「5」" in h or "5–48" in h for h in unmeasured_cost_hits([bad3], allowed)),
              str(unmeasured_cost_hits([bad3], allowed)))
    finally:
        bad2.unlink(missing_ok=True)
        good2.unlink(missing_ok=True)
        bad3.unlink(missing_ok=True)

    real5 = unmeasured_cost_hits(files, allowed) if summary else ["仪器缺失，无法现测"]
    check("门面里每个成本类行数都等于现测值", len(real5) == 0, "; ".join(real5))
    no_cmd = [f.name for f in files if cost_lines(f)
              and REPRO_CMD not in f.read_text(encoding="utf-8")]
    check("🔴 凡作成本声明的门面文件都带现读命令（否则读者无从核对）",
          not no_cmd, str(no_cmd))

    print("\n== 6. 门面引用的路径：公开 clone 拿不到的不许当证据引 ==")
    faces = face_files()
    check(f"受管门面 ≥ {MIN_FACES} 个（少于下限说明抽取器坏了，判红而不是在 0 处引用上打 ok）",
          len(faces) >= MIN_FACES, f"{len(faces)} 个: {[f.relative_to(ROOT).as_posix() for f in faces]}")
    check("每个受管门面都真的读得到", all(f.is_file() for f in faces), str(len(faces)))

    prefixes = ignore_prefixes()
    check("🔴 .gitignore 的字面前缀集非空且含已知生成物（被清空 ⇒ 生成物会被误判成 OK）",
          "out/" in prefixes and "evidence/latest.json" in prefixes, str(prefixes))
    rot = [k for k, toks in GENERATED_BY.items()
           if not any((ROOT / t).is_file() for t in toks)]
    check("生成物→生产者映射没腐坏（每个值至少有一个 token 是仓内真实文件）", not rot, str(rot))

    # 正对照：相对引用文件所在目录的路径必须判 OK。探针首版正是在这里假阳性 4 条。
    dep = next((f for f in faces if f.parent.name == "dgx-spark"), None)
    check("🔴 正对照：deploy/dgx-spark/ 里的 `bench.py` 判 OK（相对路径不许被误伤）",
          dep is not None and (dep.parent / "bench.py").is_file()
          and classify_cite(dep, "bench.py", prefixes) == "OK",
          "找不到该门面或它旁边的 bench.py" if dep is None
          else f"classify={classify_cite(dep, 'bench.py', prefixes)}")

    fa = ROOT / "out" / "_m7_path_absent.md"
    fg = ROOT / "out" / "_m7_path_gen.md"
    fd = ROOT / "out" / "_m7_path_disc.md"
    fa.parent.mkdir(exist_ok=True)
    try:
        fa.write_text("读数见 `raw/audit/nope.log` 18/18\n", encoding="utf-8")
        a_hit, _ = path_hits([fa], prefixes)
        check("🔴 规则 A 在工作区件引用上开火", len(a_hit) == 1, str(a_hit))

        fg.write_text("| 耗时 | 3.55 s | `out/benchmark.json` 的 `wall_ms` 逐局求 |\n", encoding="utf-8")
        _, g_hit = path_hits([fg], prefixes)
        check("🔴 规则 B 在『生成物但同文件没点名生产者』上开火", len(g_hit) == 1, str(g_hit))
        fg.write_text("跑 `python -m benchmark.run` 生成 `out/benchmark.json`（gitignored），取 `wall_ms`\n",
                      encoding="utf-8")
        _, g_ok = path_hits([fg], prefixes)
        check("规则 B 对点名了生产者的同一句保持沉默", g_ok == [], str(g_ok))

        fd.write_text("它落在 `out/`\n", encoding="utf-8")
        _, d_hit = path_hits([fd], prefixes)
        check("🔴 未分类生成物既不点名生产者也不披露 ⇒ 红（覆盖下限，防新 artifact 静默溜过）",
          len(d_hit) == 1, str(d_hit))
        fd.write_text("它落在 `out/`（已 gitignore）且从不随仓库分发\n", encoding="utf-8")
        _, d_ok = path_hits([fd], prefixes)
        check("对显式披露不分布的同一句保持沉默", d_ok == [], str(d_ok))
    finally:
        for f in (fa, fg, fd):
            f.unlink(missing_ok=True)

    real_a, real_b = path_hits(faces, prefixes)
    check("门面里没有 clone 拿不到的路径引用", not real_a, "; ".join(real_a))
    check("门面里每个生成物都点名了生产者或显式披露不分布", not real_b, "; ".join(real_b))

    failed = [r for r in results if not r[1]]
    print(f"\nm7_outward_wording: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
