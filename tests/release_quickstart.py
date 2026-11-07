"""本地 clean-room quickstart：新产出、真实回放与两条负控。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def run_cli(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "playtest.cli", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def output_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stdout + "\n" + proc.stderr).strip().replace("\n", " | ")
    return text[-500:]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="playtest-quickstart-") as td:
        clone = pathlib.Path(td) / "repo"
        shutil.copytree(
            ROOT,
            clone,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "out", "evidence", "__pycache__",
                ".pytest_cache", "dist", "build", "*.egg-info", "*.pyc",
            ),
        )
        check(
            "clean-room 不带旧 out 或认证产物",
            not (clone / "out").exists() and not (clone / "evidence").exists(),
            str(clone),
        )

        scan = run_cli(
            clone, "scan", "games/clicker-quest",
            "--seeds", "20261105", "--policy", "curiosity",
        )
        packs = sorted((clone / "out" / "scan").glob("curiosity-20261105-*.json"))
        check(
            "quickstart scan 从零新产出恰好一个复现包",
            scan.returncode == 0 and len(packs) == 1,
            f"rc={scan.returncode} packs={len(packs)}",
        )
        if len(packs) != 1:
            print(output_excerpt(scan))
        else:
            pack_path = packs[0]
            replay = run_cli(clone, "replay", str(pack_path))
            try:
                verification = json.loads(replay.stdout)
            except json.JSONDecodeError:
                verification = {}
            check(
                "quickstart replay 在新实例逐帧回放成功",
                replay.returncode == 0 and verification.get("replayed") is True,
                f"rc={replay.returncode} steps={verification.get('steps_matched')}/"
                f"{verification.get('steps_total')} pack={pack_path.name}",
            )
            if replay.returncode != 0 or verification.get("replayed") is not True:
                print(output_excerpt(replay))

            original = pack_path.read_bytes()
            payload = json.loads(original)
            actions = payload.get("actions") or []
            original_hash = actions[0]["hash_after"] if actions else ""
            if actions:
                payload["actions"][0]["hash_after"] = "sha256:" + "0" * 64
            tampered_path = pack_path.with_name("tampered.json")
            tampered_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            plant_applied = bool(actions) and original_hash != payload["actions"][0]["hash_after"]
            plant_applied = plant_applied and tampered_path.read_bytes() != original
            tampered = run_cli(clone, "replay", str(tampered_path))
            try:
                tampered_result = json.loads(tampered.stdout)
            except json.JSONDecodeError:
                tampered_result = {}
            check(
                "篡改复现包必须 RC=1 且报告 mismatch",
                plant_applied
                and tampered.returncode == 1
                and tampered_result.get("replayed") is False
                and tampered_result.get("mismatch") is not None,
                f"plant={plant_applied} rc={tampered.returncode} "
                f"mismatch={tampered_result.get('mismatch')}",
            )

            (clone / "games" / "clicker-quest" / "index.html").unlink()
            missing = run_cli(
                clone, "scan", "games/clicker-quest",
                "--seeds", "20261105", "--policy", "curiosity",
                "--out", "out/missing-input",
            )
            check(
                "删除必要游戏输入必须非零",
                missing.returncode != 0,
                f"rc={missing.returncode}",
            )
            if missing.returncode == 0:
                print(output_excerpt(missing))

    failed = [row for row in RESULTS if not row[1]]
    print(f"\nrelease quickstart: {'PASS' if not failed else 'FAIL'} "
          f"{len(RESULTS) - len(failed)}/{len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
