"""外部作品语料台账。

这不是排行榜，也不替作者宣布结论。每条记录只说四类事实：
来源、我们得到的操作边界、扫描/回放证据、作者是否真的确认。
缺证据就保持未确认；证据文件漂移就报红。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any


SCHEMA = "playtest-corpus/1"
ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_REPLAY_LEVELS = {"bit_reproducible", "async_dependent", "unverified"}
ALLOWED_EXPLORATION = {"public_client_only", "author_granted", "not_requested"}
ALLOWED_PUBLISH = {"not_requested", "granted", "denied"}
ALLOWED_AUTHOR = {"not_requested", "confirmed", "reproduced", "integrated"}
FIELD_CALIBER_KEYS = ("pack_id", "pack_sha256", "offline_har_sha256")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _check_evidence(
    root: pathlib.Path,
    evidence: Any,
    sha: Any,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(evidence, str) or not evidence:
        errors.append(f"{label}: missing evidence path")
        return
    if not _is_sha(sha):
        errors.append(f"{label}: evidence sha256 must be 64 lowercase hex chars")
        return
    p = (root / evidence).resolve()
    # 逃到仓库外的路径本机读得到、匿名 clone 读不到 —— 那种绿只属于这台机器的目录形状。
    if not p.is_relative_to(root.resolve()):
        errors.append(f"{label}: evidence path must stay inside the repo: {evidence}")
        return
    if not p.is_file():
        errors.append(f"{label}: missing evidence file {evidence}")
        return
    if _sha256(p) != sha:
        errors.append(f"{label}: evidence sha256 mismatch for {evidence}")


def _evidence_input_har_sha(root: pathlib.Path, evidence: Any) -> str | None:
    """证据文件若是 JSON 且自带 offline_har_sha256，就把它读出来供台账对账。"""
    if not isinstance(evidence, str) or not evidence:
        return None
    p = (root / evidence).resolve()
    if not p.is_file() or p.suffix.lower() != ".json":
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and _is_sha(data.get("offline_har_sha256")):
        return data["offline_har_sha256"]
    return None


def _check_pack_caliber(
    root: pathlib.Path,
    evidence: Any,
    label: str,
    errors: list[str],
) -> None:
    """证据 JSON 的 episodes 带 pack_sha256 时，必须自带 field_caliber 口径块。

    pack_sha256 是整份包文件的快照哈希（含 recorded_at 墙钟与录制侧 verification），
    每次重生成必漂，且指向 gitignored out/ 件、公开 clone 拿不到。没有口径块它就会被
    误读成可复核绑定，产出假"输入漂了"警报（#24）。判据只认结构（三键非空串），不钉文案。
    """
    if not isinstance(evidence, str) or not evidence:
        return
    p = (root / evidence).resolve()
    if not p.is_relative_to(root.resolve()):
        return
    if not p.is_file() or p.suffix.lower() != ".json":
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    episodes = data.get("episodes")
    if not isinstance(episodes, list):
        return
    if not any(isinstance(e, dict) and "pack_sha256" in e for e in episodes):
        return
    caliber = data.get("field_caliber")
    if not isinstance(caliber, dict):
        errors.append(f"{label}: evidence carries pack_sha256 but no field_caliber block")
        return
    for key in FIELD_CALIBER_KEYS:
        v = caliber.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{label}: field_caliber.{key} must be a non-empty string")


def _validate_author(
    root: pathlib.Path,
    game_id: str,
    author: Any,
    errors: list[str],
) -> None:
    label = f"{game_id}.author_confirmation"
    if not isinstance(author, dict):
        errors.append(f"{label}: must be an object")
        return
    status = author.get("status")
    if status not in ALLOWED_AUTHOR:
        errors.append(f"{label}: status must be one of {sorted(ALLOWED_AUTHOR)}")
        return
    evidence = author.get("evidence")
    if status == "not_requested":
        if evidence is not None:
            errors.append(f"{label}: not_requested must have evidence null")
        return
    if not isinstance(evidence, dict):
        errors.append(f"{label}: {status} requires non-null evidence")
        return
    url = evidence.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        return
    _check_evidence(root, evidence.get("path"), evidence.get("sha256"), label, errors)


def validate_corpus(root: pathlib.Path, corpus_path: pathlib.Path) -> list[str]:
    try:
        data = json.loads(pathlib.Path(corpus_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"corpus unreadable: {exc}"]
    if not isinstance(data, dict):
        return ["top-level must be an object"]

    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if data.get("scope") != "local_operational_ledger":
        errors.append("scope must be local_operational_ledger")
    games = data.get("games")
    if not isinstance(games, list):
        errors.append("games must be a list")
        return errors

    seen: set[str] = set()
    for i, g in enumerate(games):
        if not isinstance(g, dict):
            errors.append(f"games[{i}]: must be an object")
            continue
        gid = g.get("id") or f"games[{i}]"
        if not isinstance(g.get("id"), str) or not gid:
            errors.append(f"games[{i}]: id is required")
            continue
        if gid in seen:
            errors.append(f"{gid}: duplicate id")
        seen.add(gid)

        if not isinstance(g.get("title"), str) or not g["title"]:
            errors.append(f"{gid}: title is required")
        url = g.get("source_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"{gid}: source_url must be an HTTPS URL")
        if g.get("ownership") != "third_party":
            errors.append(f"{gid}: corpus entries must be third_party")
        if not isinstance(g.get("author"), dict) or not g["author"].get("name"):
            errors.append(f"{gid}: author.name is required")

        perms = g.get("permissions")
        if not isinstance(perms, dict):
            errors.append(f"{gid}: permissions must be an object")
        else:
            if perms.get("exploration") not in ALLOWED_EXPLORATION:
                errors.append(f"{gid}: permissions.exploration is invalid")
            if perms.get("publish_report") not in ALLOWED_PUBLISH:
                errors.append(f"{gid}: permissions.publish_report is invalid")

        defects = g.get("defects")
        if not isinstance(defects, list):
            errors.append(f"{gid}: defects must be a list")
        else:
            for j, d in enumerate(defects):
                label = f"{gid}.defects[{j}]"
                if not isinstance(d, dict):
                    errors.append(f"{label}: must be an object")
                    continue
                if not isinstance(d.get("kind"), str) or not d["kind"]:
                    errors.append(f"{label}: kind is required")
                if not isinstance(d.get("signature"), str) or not d["signature"]:
                    errors.append(f"{label}: signature is required")
                _check_evidence(root, d.get("evidence"), d.get("evidence_sha256"), label, errors)
                _check_pack_caliber(root, d.get("evidence"), label, errors)

        replays = g.get("replays")
        if not isinstance(replays, list):
            errors.append(f"{gid}: replays must be a list")
        else:
            for j, r in enumerate(replays):
                label = f"{gid}.replays[{j}]"
                if not isinstance(r, dict):
                    errors.append(f"{label}: must be an object")
                    continue
                if not isinstance(r.get("seed"), int):
                    errors.append(f"{label}: seed must be int")
                if not isinstance(r.get("steps"), int):
                    errors.append(f"{label}: steps must be int")
                if not isinstance(r.get("pack_id"), str) or not r["pack_id"]:
                    errors.append(f"{label}: pack_id is required")
                if r.get("level") not in ALLOWED_REPLAY_LEVELS:
                    errors.append(f"{label}: level is invalid")
                _check_evidence(root, r.get("evidence"), r.get("evidence_sha256"), label, errors)
                _check_pack_caliber(root, r.get("evidence"), label, errors)
                har = r.get("input_har_sha256")
                if not _is_sha(har):
                    errors.append(f"{label}: input_har_sha256 must be 64 lowercase hex chars")
                    continue
                stated = _evidence_input_har_sha(root, r.get("evidence"))
                if stated is not None and stated != har:
                    errors.append(f"{label}: input_har_sha256 disagrees with evidence file "
                                  f"(evidence says {stated[:12]}…, ledger says {har[:12]}…)")

        _validate_author(root, gid, g.get("author_confirmation"), errors)

    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验证外部作品语料台账")
    ap.add_argument("--path", default=str(ROOT / "evidence" / "corpus.json"))
    args = ap.parse_args(argv)

    path = pathlib.Path(args.path)
    errors = validate_corpus(ROOT, path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    for g in data.get("games", []):
        if isinstance(g, dict) and g.get("id"):
            print(f"{g['id']}: defects={len(g.get('defects', []))} "
                  f"replays={len(g.get('replays', []))} "
                  f"author_confirmation={g.get('author_confirmation', {}).get('status', 'missing')}")

    for e in errors:
        print(f"FAIL  {e}", file=sys.stderr)
    if errors:
        return 1

    games = data.get("games", []) if isinstance(data.get("games"), list) else []
    print(f"PASS corpus: {len(games)} game(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
