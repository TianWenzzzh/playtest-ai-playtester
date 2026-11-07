"""外部语料台账的红绿负控。

这里验的是"没有证据就不能升级作者确认状态"，不是替外部作品作者作证。
跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m5_corpus.py
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools.corpus import validate_corpus

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []

HAR_A = "a" * 64
HAR_B = "b" * 64


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def game(log: pathlib.Path) -> dict:
    return {
        "id": "external-game",
        "title": "External Game",
        "source_url": "https://example.ms.show/",
        "author": {"name": "someone"},
        "ownership": "third_party",
        "permissions": {"exploration": "public_client_only", "publish_report": "not_requested"},
        "defects": [{
            "kind": "ui_dead_end",
            "signature": "deadend:test",
            "evidence": "proof.log",
            "evidence_sha256": sha(log),
        }],
        "replays": [{
            "seed": 7,
            "steps": 3,
            "pack_id": "abc",
            "level": "bit_reproducible",
            "input_har_sha256": HAR_A,
            "evidence": "proof.log",
            "evidence_sha256": sha(log),
        }],
        "author_confirmation": {"status": "not_requested", "evidence": None},
    }


def main() -> int:
    print("\n== 1. 正向：字段齐全且证据哈希匹配 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("valid corpus has zero errors", errors == [], str(errors[:1])[:160])

    print("\n== 2. 负控：证据哈希漂移必须红 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        data["games"][0]["replays"][0]["evidence_sha256"] = "0" * 64
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("wrong sha256 rejected", any("sha256" in e for e in errors), str(errors[:1])[:160])

    print("\n== 3. 负控：证据文件缺失必须红 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        data["games"][0]["defects"][0]["evidence"] = "missing.log"
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("missing evidence rejected", any("missing evidence" in e for e in errors),
              str(errors[:1])[:160])

    print("\n== 4. 负控：作者确认不能没有证据 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        data["games"][0]["author_confirmation"]["status"] = "confirmed"
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("confirmed without evidence rejected",
              any("author_confirmation" in e for e in errors), str(errors[:1])[:160])

    print("\n== 5. 负控：重复外部 ID 必须红 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log), game(log)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("duplicate id rejected", any("duplicate" in e for e in errors), str(errors[:1])[:160])

    print("\n== 6. 负控：内部夹具不能冒充第三方语料 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        data["games"][0]["ownership"] = "self"
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("non-third-party rejected", any("third_party" in e for e in errors),
              str(errors[:1])[:160])

    print("\n== 7. 负控：顶层 JSON 数组必须返回错误而不是异常 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        path = root / "corpus.json"
        path.write_text("[]", encoding="utf-8")
        try:
            errors = validate_corpus(root, path)
        except AttributeError as exc:
            check("top-level array rejected without exception", False, f"raised {exc}")
        else:
            check("top-level array rejected without exception",
                  any("top-level must be an object" in e for e in errors), str(errors))

    print("\n== 8. 负控：证据路径逃出仓库必须红 ==")
    # 仓库外的同名同哈希文件今天会被照单收下 —— 匿名 clone 里没有那一份，
    # 于是"本机绿"和"别人绿"不是同一件事。这条判据管的就是那个差。
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "repo"
        root.mkdir()
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        escape = pathlib.Path(td) / "escape.log"
        escape.write_bytes(log.read_bytes())
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        for row in data["games"][0]["defects"] + data["games"][0]["replays"]:
            row["evidence"] = "../escape.log"
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("evidence outside the repo rejected",
              any("inside the repo" in e for e in errors),
              f"escape_exists={escape.is_file()} errors={str(errors[:1])[:160]}")

    print("\n== 9. 现读：模拟匿名 clone（父目录不含 raw/）台账必须自证 RC=0 ==")
    with tempfile.TemporaryDirectory() as td:
        clone = pathlib.Path(td) / "repo"
        shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(
            ".git", ".venv", "out", "__pycache__", ".pytest_cache",
            "dist", "build", "*.egg-info", "*.pyc",
            "runs", "latest.json", "playtest_placeholder",
        ))
        check("clone 里没有仓库外的 raw/", not (clone.parent / "raw").exists(),
              str(clone.parent))
        proc = subprocess.run(
            [sys.executable, "tools/corpus.py"],
            cwd=clone, env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        tail = (proc.stdout + proc.stderr).strip().replace("\n", " | ")[-400:]
        check("tools/corpus.py in simulated anonymous clone RC=0", proc.returncode == 0,
              f"rc={proc.returncode} {tail}")

    print("\n== 10. 负控：复现条目缺输入快照哈希 / 格式不对都必须红 ==")
    # "同一枚包"这句话在没有输入字节锚点时是空的：换一份快照就换一枚包，
    # 台账必须能说出这枚包是哪份快照喂出来的。
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        for row in data["games"][0]["replays"]:
            del row["input_har_sha256"]
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("replay without input_har_sha256 rejected",
              all("input_har_sha256" in e for e in errors) and bool(errors),
              str(errors[:2])[:160])

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(log)]}
        data["games"][0]["replays"][0]["input_har_sha256"] = "ZZZ"
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("non-hex input_har_sha256 rejected",
              any("input_har_sha256" in e for e in errors), str(errors[:1])[:160])

    print("\n== 11. 负控：台账钉的输入哈希与证据文件不一致必须红 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "proof.json"
        ev.write_text(json.dumps({"schema": "playtest-offline-evidence/1",
                                  "offline_har_sha256": HAR_A}), encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(ev)]}
        for row in data["games"][0]["defects"] + data["games"][0]["replays"]:
            row["evidence"] = "proof.json"
            row["evidence_sha256"] = sha(ev)
        for row in data["games"][0]["replays"]:
            row["input_har_sha256"] = HAR_B
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        n_replays = len(data["games"][0]["replays"])
        check("ledger/evidence input HAR mismatch rejected",
              len([e for e in errors if "disagree" in e]) == n_replays, str(errors[:2])[:160])

    print("\n== 12. 正对照：一致时这条判据必须闭嘴 ==")
    # 只证会红的判据是狼闸门；红在合法样本上的规则比没规则更坏。
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "proof.json"
        ev.write_text(json.dumps({"schema": "playtest-offline-evidence/1",
                                  "offline_har_sha256": HAR_A}), encoding="utf-8")
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game(ev)]}
        for row in data["games"][0]["defects"] + data["games"][0]["replays"]:
            row["evidence"] = "proof.json"
            row["evidence_sha256"] = sha(ev)
            row["input_har_sha256"] = HAR_A
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("matching input HAR passes with zero errors", errors == [], str(errors[:1])[:160])

    print("\n== 13. 负控/正对照：带 pack_sha256 的证据必须自带口径块 ==")
    # pack_sha256 是整份包文件的快照哈希（含 recorded_at 墙钟），每次重生成必漂，
    # 且指向 gitignored out/ 件 —— 没有口径块就会被误读成可复核绑定（#24）。
    def ledger_ev(path: pathlib.Path, caliber, with_pack_sha: bool = True) -> None:
        ep = {"seed": 7, "steps": 3, "pack_id": "abc"}
        if with_pack_sha:
            ep["pack_sha256"] = "c" * 64
        data = {"schema": "playtest-offline-evidence/2", "offline_har_sha256": HAR_A,
                "episodes": [ep]}
        if caliber is not None:
            data["field_caliber"] = caliber
        path.write_text(json.dumps(data), encoding="utf-8")

    def game_on_ledger(ev: pathlib.Path) -> dict:
        g = game(ev)
        for row in g["defects"] + g["replays"]:
            row["evidence"] = "ledger.json"
            row["evidence_sha256"] = sha(ev)
        for row in g["replays"]:
            row["input_har_sha256"] = HAR_A
        return g

    full_caliber = {"pack_id": "稳定身份", "pack_sha256": "快照哈希，必漂",
                    "offline_har_sha256": "可复核输入绑定"}

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "ledger.json"
        ledger_ev(ev, caliber=None)
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game_on_ledger(ev)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        # defects 与 replays 各引用一次 ⇒ 两条都要红，证明两个调用点都接了线
        check("pack_sha256 without field_caliber rejected (both call sites)",
              len([e for e in errors if "field_caliber" in e]) == 2, str(errors[:2])[:160])

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "ledger.json"
        ledger_ev(ev, caliber={"pack_id": "稳定身份", "pack_sha256": "快照哈希"})
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game_on_ledger(ev)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("field_caliber missing a key rejected",
              any("field_caliber.offline_har_sha256" in e for e in errors), str(errors[:1])[:160])

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "ledger.json"
        ledger_ev(ev, caliber=full_caliber)
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game_on_ledger(ev)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("complete field_caliber passes with zero errors", errors == [], str(errors[:1])[:160])

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        ev = root / "ledger.json"
        ledger_ev(ev, caliber=None, with_pack_sha=False)
        data = {"schema": "playtest-corpus/1", "scope": "local_operational_ledger",
                "games": [game_on_ledger(ev)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = validate_corpus(root, path)
        check("evidence without pack_sha256 stays silent", errors == [], str(errors[:1])[:160])

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 46}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:120]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
