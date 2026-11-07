"""把既有的复现包在**同一份离线快照**上连重放 N 遍，逐遍留读数并记输入 HAR 的哈希。

为什么要有这个工具：台账此前只能说"各一遍独立回放 RC=0"，而"同一枚包连重放三遍逐值一致"
这句话只属于更早那份快照的输入。换输入就换包身份，所以强度不能跨快照搬。
这里量的就是当前这份快照的：输入字节哈希 + 每一遍的逐帧比对结果。

跑法（工作目录 = 仓库根）：
  PYTHONUTF8=1 python tools/replay_repeat.py --har out/offline/ycxxl.har \
      --id ycxxl --title 优宠消消乐 \
      --pack out/offline/ycxxl-packs/curiosity-20261105-25dc2578c3197948.json \
      --pack out/offline/ycxxl-packs/curiosity-20261106-0aaf2d192c4051ad.json \
      --reps 3 --out out/repeat/ycxxl_repeat3.json

退出码：0 = 每一枚包每一遍都 replayed=true 且逐值一致；1 = 有任何一遍不一致（如实报，不放宽）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.trace import pack_id

SCHEMA = "playtest-offline-evidence/2"
ROOT = pathlib.Path(__file__).resolve().parent.parent

# 三个身份字段的口径随台账一起走，免得 pack_sha256 被误读成可复核绑定（#24）。
FIELD_CALIBER = {
    "pack_id": "稳定身份：digest(url, seed, 动作字符串序列, defect.signature) 前 16 位（playtest/trace.py）；"
               "不含 actions[].hash_after/frame 期望时间线 ⇒ 同 pack_id ≠ 同字节。",
    "pack_sha256": "记录时刻整份包文件的快照哈希：包内含 evidence.recorded_at 墙钟与录制侧 verification，"
                   "每次重生成必漂；指向 gitignored out/ 件，公开 clone 拿不到 ⇒ 不是可复核绑定，仅作本地快照留痕。",
    "offline_har_sha256": "可复核的输入绑定：重放实际喂的 HAR 字节哈希；台账 replays[].input_har_sha256 与它对账。",
}


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def replay_once(pack: pathlib.Path, har: pathlib.Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "playtest.cli", "replay", str(pack), "--har", str(har)],
        cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {}
    return {
        "rc": proc.returncode,
        "replayed": result.get("replayed"),
        "initial_hash_matched": result.get("initial_hash_matched"),
        "steps_matched": result.get("steps_matched"),
        "steps_total": result.get("steps_total"),
        "hashes_match": result.get("hashes_match"),
        "mismatch": result.get("mismatch"),
        "stderr_tail": (proc.stderr or "").strip()[-200:],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同一枚包在同一份快照上连重放 N 遍")
    ap.add_argument("--har", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--pack", action="append", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    har = pathlib.Path(args.har)
    if not har.is_file():
        print(f"FAIL  输入快照不存在：{har}", file=sys.stderr)
        return 1
    input_sha = sha256(har)
    print(f"输入快照 {har.name}：{har.stat().st_size} B / sha256={input_sha[:16]}…")

    packs = []
    for raw in args.pack:
        p = pathlib.Path(raw)
        packs.append((p, json.loads(p.read_text(encoding="utf-8"))))
    game = packs[0][1].get("game") or {}

    rows = []
    bad = 0
    for p, payload in packs:
        pid = pack_id(payload)
        runs = []
        for i in range(args.reps):
            started = time.time()
            run = replay_once(p, har)
            run["wall_ms"] = int((time.time() - started) * 1000)
            runs.append(run)
            print(f"  {pid} 第 {i + 1} 遍 rc={run['rc']} "
                  f"{run['steps_matched']}/{run['steps_total']} "
                  f"hashes_match={run['hashes_match']}")
        consistent = all(r["rc"] == 0 and r["replayed"] is True and r["hashes_match"] is True
                         and r["mismatch"] is None and r["initial_hash_matched"] is True
                         for r in runs)
        per_run_equal = len({(r["steps_matched"], r["steps_total"]) for r in runs}) == 1
        if not consistent:
            bad += 1
        rows.append({
            "seed": payload["initial_state"]["seed"],
            "pack_id": pid,
            "pack_sha256": sha256(p),
            "steps": len(payload.get("actions") or []),
            "defect_kind": (payload.get("defect") or {}).get("kind"),
            "defect_signature": (payload.get("defect") or {}).get("signature"),
            "runs": runs,
            "all_runs_replayed": consistent,
            "per_run_readings_identical": per_run_equal,
            "level": "bit_reproducible" if consistent else "async_dependent",
        })

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": SCHEMA,
        "game": {"id": args.id,
                 "title": (game.get("meta") or {}).get("title", ""),
                 "source_url": game.get("url", "")},
        "scope": "public_client_only_offline_replay",
        "offline_har_sha256": input_sha,
        "field_caliber": FIELD_CALIBER,
        "repetitions_requested": args.reps,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "episodes": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写 {out}：{len(rows)} 枚包 × {args.reps} 遍；不一致 {bad} 枚")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
