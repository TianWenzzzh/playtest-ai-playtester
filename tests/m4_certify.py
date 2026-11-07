"""认证工装的红绿负控。

这里验的是认证器会不会把"没认证成"如实报红，不是替游戏逻辑背书。
跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m4_certify.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools import certify

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def fake_ok(step, root, log_path):
    log_path.write_text(f"PASS {step.name}\nRC=0\n", encoding="utf-8")
    return 0


def fake_fail(step, root, log_path):
    log_path.write_text(f"FAIL {step.name}\nRC=9\n", encoding="utf-8")
    return 9


def fake_change_source(step, root, log_path):
    (root / "module.py").write_text("value = changed\n", encoding="utf-8")
    log_path.write_text(f"PASS {step.name}\nRC=0\n", encoding="utf-8")
    return 0


def main() -> int:
    print("\n== 1. 成功认证必须写出自洽的 latest.json ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        digest_before = certify.source_digest(root)
        rc, report = certify.certify(root, "fast", runner=fake_ok)
        latest_path = root / "evidence" / "latest.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        check("green RC=0", rc == 0 and latest["ok"] is True, str(rc))
        check("报告绑定当前源码摘要", latest["source"]["sha256"] == digest_before,
              latest["source"]["sha256"][:16])
        check("步骤记录真实 RC", all(step["rc"] == 0 for step in latest["steps"]),
              str([step["rc"] for step in latest["steps"]]))

        first_log = root / latest["steps"][0]["log"]
        check("日志哈希与文件一致", hashlib.sha256(first_log.read_bytes()).hexdigest()
              == latest["steps"][0]["log_sha256"], latest["steps"][0]["log_sha256"][:16])

    print("\n== 2. 证据目录不许污染源码摘要 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        before = certify.source_digest(root)
        ignored = root / "evidence" / "runs" / "scratch.txt"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("ignored", encoding="utf-8")
        check("新增 evidence/runs 后源码摘要不变", certify.source_digest(root) == before,
              certify.source_digest(root)[:16])

    print("\n== 3. 任一命令失败必须覆盖旧绿牌 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        rc, report = certify.certify(root, "fast", runner=fake_fail)
        latest = json.loads((root / "evidence" / "latest.json").read_text(encoding="utf-8"))
        check("failed RC=1", rc == 1 and latest["ok"] is False, str(rc))
        check("失败步骤保留 RC=9", latest["steps"][0]["rc"] == 9, str(latest["steps"][0]))

    print("\n== 4. release 没有验收脚本必须拒绝发绿牌 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        rc, report = certify.certify(root, "release", runner=fake_ok)
        latest = json.loads((root / "evidence" / "latest.json").read_text(encoding="utf-8"))
        missing_ok = all(name in report.get("error", "") for name in (
            "tests/release_ui.py", "tests/release_public.py", "tests/release_quickstart.py"))
        check("release precondition RC=2", rc == 2 and latest["ok"] is False, str(rc))
        check("红牌点名三条缺失验收", missing_ok, report.get("error", "")[:120])

    print("\n== 5. 认证期间源码变化必须作废 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        rc, report = certify.certify(root, "fast", runner=fake_change_source)
        latest = json.loads((root / "evidence" / "latest.json").read_text(encoding="utf-8"))
        check("运行中源码改动 RC=1", rc == 1 and latest["ok"] is False, str(rc))
        check("错误明确归因为源码漂移", "source changed" in latest.get("error", ""),
              latest.get("error", "")[:120])

    print("\n== 6. *.egg-info 构建元数据不许污染源码摘要 ==")
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        before = certify.source_digest(root)
        metadata = root / "demo.egg-info" / "PKG-INFO"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("Name: demo\n", encoding="utf-8")
        check("新增 egg-info 目录后源码摘要不变", certify.source_digest(root) == before,
              certify.source_digest(root)[:16])

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 46}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:120]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
