# Certification and External Corpus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed certification runner and a hash-validated external-game corpus without changing the game-testing core.

**Architecture:** `tools/certify.py` runs existing test commands, records real return codes and logs, hashes the current source tree, and always rewrites `evidence/latest.json`. `tools/corpus.py` validates `evidence/corpus.json`, including local artifact hashes and author-confirmation evidence. Both are standard-library-only CLI modules with standalone regression tests.

**Tech Stack:** Python >=3.10, existing standalone test scripts, JSON, hashlib, subprocess, pathlib.

**Spec:** `D:\AI游戏比赛\09-作战控制台.md`

## Global Constraints

- Do not modify `probe/`, `playtest/`, `games/`, `benchmark/`, or the existing replay/fix logic.
- `latest.json` is generated only by the certifier; it must contain source digest, commands, timestamps, real RCs, log paths, and log SHA-256 values.
- A failed step produces RC=1; a release precondition failure produces RC=2. Neither may leave an older green report looking current.
- `release` fails closed until `tests/release_ui.py`, `tests/release_public.py`, and `tests/release_quickstart.py` exist.
- External-game author confirmation remains `not_requested` unless evidence exists and validates.
- Do not commit, push, deploy, or publish without separate explicit user authorization.
- Use `PYTHONUTF8=1` for all Python verification commands.

---

### Task 1: Certification Runner

**Files:**
- Create: `tools/certify.py`
- Create: `tests/m4_certify.py`
- Modify: `.gitignore`

**Interfaces:**
- `source_digest(root: pathlib.Path) -> str`
- `steps_for(root: pathlib.Path, tier: str, python: str) -> list[Step]`
- `certify(root: pathlib.Path, tier: str, runner: Callable = run_command) -> tuple[int, dict]`
- `main(argv: list[str] | None = None) -> int`
- Report: `evidence/latest.json`, schema `playtest-certification/1`

- [ ] **Step 1: Write the failing standalone test**

Create `tests/m4_certify.py` with checks equivalent to:

```python
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools import certify


def fake_ok(step, root, log_path):
    log_path.write_text(f"PASS {step.name}\nRC=0\n", encoding="utf-8")
    return 0


def fake_fail(step, root, log_path):
    log_path.write_text(f"FAIL {step.name}\nRC=9\n", encoding="utf-8")
    return 9


def main():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "module.py").write_text("value = 1\n", encoding="utf-8")
        rc, report = certify.certify(root, "fast", runner=fake_ok)
        assert rc == 0 and report["ok"] is True
        latest = json.loads((root / "evidence" / "latest.json").read_text(encoding="utf-8"))
        assert latest["source"]["sha256"] == report["source"]["sha256"]
        assert latest["steps"][0]["rc"] == 0
        assert len(latest["steps"][0]["log_sha256"]) == 64

        before = latest["source"]["sha256"]
        (root / "module.py").write_text("value = 2\n", encoding="utf-8")
        assert certify.source_digest(root) != before
        before_ignored = certify.source_digest(root)
        ignored = root / "evidence" / "runs" / "scratch.txt"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("ignored", encoding="utf-8")
        assert certify.source_digest(root) == before_ignored

        rc, report = certify.certify(root, "fast", runner=fake_fail)
        assert rc == 1 and report["ok"] is False
        assert report["steps"][0]["rc"] == 9

        rc, report = certify.certify(root, "release", runner=fake_ok)
        assert rc == 2 and report["ok"] is False
        assert "release_ui.py" in report["error"]
    print("PASS certification runner: green, failure, hashing, release precondition")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m4_certify.py
```

Expected: non-zero exit because `tools/certify.py` does not exist.

- [ ] **Step 3: Implement the minimal certifier**

Implement `Step` as a dataclass with `name: str` and `command: tuple[str, ...]`.

Use these exact command groups:

```python
FAST_FILES = (
    "tests/m0_smoke.py",
    "tests/m1_fallback.py",
    "tests/m2_fix.py",
    "tests/m3_mirror_semantics.py",
    "tests/m4_certify.py",
    "tests/m5_corpus.py",
)
RELEASE_FILES = (
    "tests/release_ui.py",
    "tests/release_public.py",
    "tests/release_quickstart.py",
)
```

`fast` runs all `FAST_FILES` and then `tools/corpus.py`. `full` runs `fast`, then `tests/m3_offline.py`, then `python -m benchmark.run`. `release` first checks all `RELEASE_FILES`, then runs `full` as pass A, the three release files, and `full` as pass B.

Hash every file under the repository except paths whose first component is `.git`, `.venv`, `out`, `evidence`, `__pycache__`, `.pytest_cache`, `dist`, or `build`; also exclude `*.pyc` and `*.egg-info`. Hash relative path bytes, a NUL separator, and file bytes. Recompute after the run and fail if the source changed during certification.

Use `subprocess.run(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}, stdout=log, stderr=subprocess.STDOUT, check=False)`. Record the actual return code; never infer success from log text. Stop after the first failed command. Always write a fresh report with `ok=false` on failure or precondition errors.

Write logs under `evidence/runs/<UTC timestamp>-<tier>/`. Write `latest.json` atomically with a temporary file followed by `replace()`. Include `schema`, `tier`, `started_at`, `finished_at`, `source`, `environment`, `steps`, `ok`, and optional `error`. Environment contains Python version and platform; source contains the tree SHA-256 and optional `git rev-parse HEAD` result.

Append these lines to `.gitignore`:

```gitignore
evidence/runs/
evidence/latest.json
```

- [ ] **Step 4: Run the certifier test and verify GREEN**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m4_certify.py
```

Expected: `PASS certification runner: green, failure, hashing, release precondition`, RC=0.

- [ ] **Step 5: Review the diff without committing**

Run `git diff -- .gitignore tools/certify.py tests/m4_certify.py` and `git status --short`. Confirm no game-testing source changed and no commit was created.

---

### Task 2: External Corpus Ledger and Validator

**Files:**
- Create: `tools/corpus.py`
- Create: `evidence/corpus.json` (immutable local operational ledger; no `out/` artifacts)
- Create: `tests/m5_corpus.py`

**Interfaces:**
- `validate_corpus(root: pathlib.Path, corpus_path: pathlib.Path) -> list[str]`
- `main(argv: list[str] | None = None) -> int`
- Corpus schema: `playtest-corpus/1`

- [ ] **Step 1: Write the failing validator tests**

Create `tests/m5_corpus.py` with a minimal valid entry and these exact cases:

```python
import copy
import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools.corpus import validate_corpus


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entry(log):
    return {
        "id": "external-game",
        "title": "External Game",
        "source_url": "https://example.ms.show/",
        "author": {"name": "someone"},
        "ownership": "third_party",
        "permissions": {"exploration": "public_client_only", "publish_report": "not_requested"},
        "capture": None,
        "defects": [{"kind": "ui_dead_end", "evidence": "proof.log", "evidence_sha256": sha(log)}],
        "replays": [{"seed": 7, "pack": None, "level": "bit_reproducible",
                     "evidence": "proof.log", "evidence_sha256": sha(log)}],
        "author_confirmation": {"status": "not_requested", "evidence": None},
    }


def main():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        log = root / "proof.log"
        log.write_text("PASS\nRC=0\n", encoding="utf-8")
        good = {"schema": "playtest-corpus/1", "games": [entry(log)]}
        path = root / "corpus.json"
        path.write_text(json.dumps(good), encoding="utf-8")
        assert validate_corpus(root, path) == []

        bad = copy.deepcopy(good)
        bad["games"][0]["defects"][0]["evidence_sha256"] = "0" * 64
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert any("sha256" in e for e in validate_corpus(root, path))

        bad = copy.deepcopy(good)
        bad["games"][0]["author_confirmation"]["status"] = "confirmed"
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert any("author_confirmation" in e for e in validate_corpus(root, path))

        bad = copy.deepcopy(good)
        bad["games"].append(copy.deepcopy(bad["games"][0]))
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert any("duplicate" in e for e in validate_corpus(root, path))
    print("PASS corpus: valid, wrong hash, unsupported author claim, duplicate id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Also test a missing evidence path and assert its error contains `missing evidence`.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m5_corpus.py
```

Expected: non-zero exit because `tools/corpus.py` does not exist.

- [ ] **Step 3: Implement validation and the real corpus**

`validate_corpus` returns human-readable errors rather than raising on ordinary invalid data. Validate:

- top-level schema and unique game IDs;
- `ownership == "third_party"` and an HTTPS `source_url`;
- each defect/replay evidence path exists after resolution and its SHA-256 matches; workspace-local `../raw` references are allowed in this local operational ledger, but must not be described as publicly downloadable artifacts;
- replay level is one of `bit_reproducible`, `async_dependent`, `unverified`;
- author status is one of `not_requested`, `confirmed`, `reproduced`, `integrated`;
- statuses other than `not_requested` require a non-null evidence object with a valid path/hash or HTTPS URL;
- `not_requested` must have `evidence: null`, preventing unsupported confirmation claims.

CLI prints one summary per game and `PASS corpus: <n> game(s)` with RC=0, or one `FAIL` per error with RC=1.

Create `evidence/corpus.json` with this measured entry, preserving these exact hashes:

```json
{
  "schema": "playtest-corpus/1",
  "scope": "local_operational_ledger",
  "games": [
    {
      "id": "work-a",
      "title": "作品 A（脱敏代号）",
      "source_url": "https://work-a.invalid/",
      "author": {"name": "author-a"},
      "ownership": "third_party",
      "permissions": {"exploration": "public_client_only", "publish_report": "not_requested"},
      "defects": [
        {
          "kind": "ui_dead_end",
          "signature": "deadend:click:div:计数型展示容器@0e09d6781b93",
          "evidence": "../raw/audit/2026-11-06_m3_offline_fresh_summary.json",
          "evidence_sha256": "ffd3faf47d9de527448f01acd13d31ed7b38c1701bd597aa51317c7058448358"
        },
        {
          "kind": "ui_dead_end",
          "signature": "deadend:click:button:场景切换按钮@88c4557ac754",
          "evidence": "../raw/audit/2026-11-06_m3_offline_fresh_summary.json",
          "evidence_sha256": "ffd3faf47d9de527448f01acd13d31ed7b38c1701bd597aa51317c7058448358"
        }
      ],
      "replays": [
        {
          "seed": 20261105,
          "steps": 20,
          "pack_id": "a668e81678650103",
          "level": "bit_reproducible",
          "evidence": "../raw/audit/2026-11-06_m3_offline_fresh_selfrebuild.log",
          "evidence_sha256": "bb464bc76aa9e685a5f3861c24acc829f817f3e2d357742ae1f4b06beb6964e7"
        },
        {
          "seed": 20261106,
          "steps": 23,
          "pack_id": "ef6b22c199b40365",
          "level": "bit_reproducible",
          "evidence": "../raw/audit/2026-11-06_m3_offline_fresh_selfrebuild.log",
          "evidence_sha256": "bb464bc76aa9e685a5f3861c24acc829f817f3e2d357742ae1f4b06beb6964e7"
        }
      ],
      "author_confirmation": {"status": "not_requested", "evidence": null}
    }
  ]
}
```

- [ ] **Step 4: Run validator tests and the real corpus check**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m5_corpus.py
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/corpus.py
```

Expected: both RC=0; the real corpus reports one game and `author_confirmation=not_requested`.

- [ ] **Step 5: Prove the real validator fires on a copy**

Copy `evidence/corpus.json` to a temporary directory, change one replay evidence hash to 64 zeroes, and run `tools/corpus.py --path <temporary-copy>`.

Expected: RC=1 with a `sha256` error. Do not modify the real corpus.

- [ ] **Step 6: Review the diff without committing**

Run `git diff -- tools/corpus.py evidence/corpus.json tests/m5_corpus.py` and `git status --short`. Confirm no author confirmation was inferred and no third-party HAR or replay pack was added to git.

---

### Task 3: Integrated Verification and Documentation Status

**Files:**
- Modify: `README.md` only to add the two tool commands and the fail-closed release precondition.
- Modify: `../09-作战控制台.md` only to mark implemented tools after their verification evidence exists.

- [ ] **Step 1: Run the fast certification**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/certify.py fast
```

Expected: RC=0; `evidence/latest.json` has `tier=fast`, `ok=true`, current source digest, and all step log hashes.

- [ ] **Step 2: Run the release negative control**

Run:

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/certify.py release
```

Expected: RC=2 because the three release scripts do not exist; `latest.json` is red and names the missing precondition. Rerun `fast` afterward so the operational latest report reflects the last meaningful certification rather than the intentional negative control.

- [ ] **Step 3: Document exact usage**

Add to `README.md`:

```bash
PYTHONUTF8=1 python tools/certify.py fast
PYTHONUTF8=1 python tools/certify.py full
PYTHONUTF8=1 python tools/certify.py release
PYTHONUTF8=1 python tools/corpus.py
```

State that `release` intentionally returns RC=2 until the UI, public-access, and clean-room quickstart scripts exist. Do not claim release certification has passed.

- [ ] **Step 4: Update only verified tool statuses**

In `09-作战控制台.md`, mark `tools/certify.py`, `evidence/latest.json`, and `evidence/corpus.json` as implemented only after Steps 1–3 pass. Link the local logs; leave UI rehearsal整理 and all product/deployment statuses unchanged.

- [ ] **Step 5: Final review**

Run `git diff --check`, `git status --short`, and the standalone `m4_certify.py` / `m5_corpus.py` tests again. Report exact commands and RCs. Do not commit or push.
