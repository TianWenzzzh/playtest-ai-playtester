"""规范化哈希：回放校验的判据。

比截图、比 DOM 字符串都不算数 —— 只有对同一份状态定义一个与键序无关的字节表示，
"逐帧哈希一致"这句话才有可核对的含义。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_fallback,
    ).encode("utf-8", errors="replace")


def _fallback(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(str(x) for x in o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", errors="replace")
    return repr(o)


def digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def short(h: str) -> str:
    return h.split(":")[-1][:12] if ":" in h else h[:12]
