"""一键认证运行器。

三档：
  fast    跑核心套件，用于日常改动后的快速回归。
  full    fast 加真实作品离线端到端与 144 局基准，用于里程碑验收。
  release full 跑两遍，并要求 UI、公开访问、clean-room quickstart 三条发布腿。

这里刻意不重新实现任何被测逻辑。它的职责是把命令的真实退出码、日志和当次源码摘要
绑定起来；任何步骤失败都会覆盖 latest.json，旧绿牌不能冒充当前状态。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


SCHEMA = "playtest-certification/1"
ROOT = Path(__file__).resolve().parent.parent

FAST_FILES = (
    "tests/m0_smoke.py",
    "tests/m1_fallback.py",
    "tests/m2_fix.py",
    "tests/m3_mirror_semantics.py",
    "tests/m4_certify.py",
    "tests/m5_corpus.py",
    "tests/m6_benchmark_note.py",
    "tests/m7_outward_wording.py",
)
FULL_EXTRA_FILES = (
    "tests/m3_offline.py",
)
RELEASE_FILES = (
    "tests/release_ui.py",
    "tests/release_public.py",
    "tests/release_quickstart.py",
)
IGNORED_PARTS = {
    ".git", ".venv", "out", "evidence", "__pycache__",
    ".pytest_cache", "dist", "build",
}


class ReleasePreconditionError(RuntimeError):
    """release 档缺少必需的公开发布验收脚本。"""


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]


def source_digest(root: Path) -> str:
    """给"当前被测树"出一个稳定摘要，证据产物本身不参与。"""
    h = hashlib.sha256()
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if set(rel.parts) & IGNORED_PARTS:
            continue
        if any(part.endswith(".egg-info") for part in rel.parts):
            continue
        if rel.suffix.lower() == ".pyc":
            continue
        files.append(p)

    for p in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def git_head(root: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def run_command(step: Step, root: Path, log_path: Path) -> int:
    """真实执行一条命令，并把 stdout/stderr 合并进 log_path。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1"}
    with log_path.open("wb") as log:
        proc = subprocess.run(
            list(step.command), cwd=root, env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
    return proc.returncode


def _test_steps(root: Path, python: str, prefix: str = "") -> list[Step]:
    steps = []
    for rel in FAST_FILES:
        p = root / rel
        steps.append(Step(f"{prefix}{rel}", (python, str(p))))
    steps.append(Step(f"{prefix}tools/corpus.py", (python, str(root / "tools" / "corpus.py"))))
    for rel in FULL_EXTRA_FILES:
        p = root / rel
        steps.append(Step(f"{prefix}{rel}", (python, str(p))))
    steps.append(Step("benchmark.run", (python, "-m", "benchmark.run")))
    return steps


def steps_for(root: Path, tier: str, python: str | None = None) -> list[Step]:
    python = python or sys.executable
    if tier == "fast":
        return _test_steps(root, python)[:len(FAST_FILES) + 1]
    if tier == "full":
        return _test_steps(root, python)
    if tier == "release":
        missing = [rel for rel in RELEASE_FILES if not (root / rel).exists()]
        if missing:
            raise ReleasePreconditionError(
                "release 档缺少发布验收脚本: " + ", ".join(missing)
            )
        steps = _test_steps(root, python)
        steps = [Step(f"A.{s.name}", s.command) for s in steps]
        for rel in RELEASE_FILES:
            p = root / rel
            steps.append(Step(rel, (python, str(p))))
        steps.extend(Step(f"B.{s.name}", s.command) for s in _test_steps(root, python))
        return steps
    raise ReleasePreconditionError(f"未知认证档: {tier}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_report(root: Path, report: dict) -> Path:
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    latest = evidence / "latest.json"
    tmp = evidence / "latest.json.tmp"
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(latest)
    return latest


def certify(
    root: Path = ROOT,
    tier: str = "fast",
    runner: Callable[[Step, Path, Path], int] = run_command,
    python: str | None = None,
) -> tuple[int, dict]:
    started = datetime.now(timezone.utc)
    started_digest = source_digest(root)
    error: str | None = None
    rc = 0
    step_rows: list[dict] = []

    try:
        steps = steps_for(root, tier, python)
    except ReleasePreconditionError as exc:
        steps = []
        rc = 2
        error = str(exc)

    run_dir: Path | None = None
    if steps:
        run_dir = root / "evidence" / "runs" / f"{started.strftime('%Y%m%d-%H%M%S')}-{tier}"
        run_dir.mkdir(parents=True, exist_ok=True)
        for i, step in enumerate(steps, 1):
            log_path = run_dir / f"{i:02d}-{step.name.replace('/', '_')}.log"
            step_started = datetime.now(timezone.utc)
            step_rc = runner(step, root, log_path)
            step_rows.append({
                "name": step.name,
                "command": list(step.command),
                "started_at": step_started.isoformat(),
                "duration_ms": int((datetime.now(timezone.utc) - step_started).total_seconds() * 1000),
                "rc": step_rc,
                "log": log_path.relative_to(root).as_posix(),
                "log_sha256": _sha256(log_path),
            })
            if step_rc != 0:
                rc = 1
                break

    finished_digest = source_digest(root)
    if rc == 0 and finished_digest != started_digest:
        rc = 1
        error = "source changed during certification"

    finished = datetime.now(timezone.utc)
    report = {
        "schema": SCHEMA,
        "tier": tier,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "source": {
            "sha256": started_digest,
            "sha256_after": finished_digest,
            "git_head": git_head(root),
        },
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "root": str(root),
            "executable": python or sys.executable,
        },
        "steps": step_rows,
        "ok": rc == 0,
    }
    if error:
        report["error"] = error

    _write_report(root, report)
    return rc, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="运行 PlayTest 认证套件并生成 latest.json")
    ap.add_argument("tier", choices=("fast", "full", "release"), nargs="?", default="fast")
    args = ap.parse_args(argv)
    rc, report = certify(ROOT, args.tier)
    print(f"certification {args.tier}: {'PASS' if report['ok'] else 'FAIL'} rc={rc}")
    if report.get("error"):
        print(report["error"], file=sys.stderr)
    for s in report["steps"]:
        print(f"  {s['name']} rc={s['rc']} log={s['log']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
