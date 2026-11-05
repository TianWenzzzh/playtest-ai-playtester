"""BrowserEnv —— 把一个 HTML5 游戏变成确定性的 Gymnasium 风格环境。

对外只有 reset / actions / step 三个动作，但它同时保证两件事：
  1. 时间吃虚拟时钟，随机数吃种子 ⇒ 同一 (seed, 动作序列) 必然得到同一状态序列；
  2. 每一步都产出规范化哈希 ⇒ 回放校验是比字节，不是比截图。
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import sync_playwright, Error as PlaywrightError

from .hashing import digest

PROBE_DIR = pathlib.Path(__file__).resolve().parent.parent / "probe"
INIT_SCRIPTS = ["virtual_clock.js", "seeded_random.js", "probe.js"]

_FALLBACK_BROWSERS = [
    pathlib.Path.home() / "AppData/Local/ms-playwright",
    pathlib.Path("/root/.cache/ms-playwright"),
]


def resolve_browser() -> str | None:
    """找一个能用的 Chromium。

    pip 装的 playwright 会点名一个特定 revision 的 headless shell，
    机器上往往只有别的版本或只有系统 Edge —— 为了这个去下载几百兆不值得，
    而且创空间那边跑的是它自己的 Chromium，本地用哪个不影响协议语义。
    """
    import os

    env_override = os.environ.get("PLAYTEST_BROWSER")
    if env_override and pathlib.Path(env_override).exists():
        return env_override

    candidates: list[pathlib.Path] = []
    for base in _FALLBACK_BROWSERS:
        if not base.exists():
            continue
        for sub in sorted(base.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True):
            candidates.append(sub)
        for sub in sorted(base.glob("chromium-*/chrome-linux/chrome"), reverse=True):
            candidates.append(sub)
    for p in candidates:
        if p.exists():
            return str(p)

    for win in (
        pathlib.Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        pathlib.Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        pathlib.Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ):
        if win.exists():
            return str(win)
    return None


@dataclass
class Snapshot:
    frame: int
    hash: str
    data: Any
    over: bool = False
    win: bool = False
    errors: int = 0
    violations: list[str] = field(default_factory=list)
    clock: dict[str, Any] = field(default_factory=dict)
    rng: dict[str, Any] = field(default_factory=dict)
    page_exceptions: list[str] = field(default_factory=list)
    pos: str | None = None

    def eq_key(self) -> str:
        """状态等价键。有投影用投影，没有就用全量哈希。"""
        return self.pos or self.hash

    def to_json(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "hash": self.hash,
            "pos": self.pos,
            "data": self.data,
            "over": self.over,
            "win": self.win,
            "errors": self.errors,
            "violations": self.violations,
        }


_shared: dict[str, Any] = {"pw": None, "browser": None, "headless": None}


def shared_browser(headless: bool = True):
    """一个进程只起一个浏览器。

    不是优化，是正确性要求：诊断要"另开一个实例"，而它是在录制实例还开着的时候被调用的。
    Playwright 同步 API 在一个线程里只允许一个 event loop，
    嵌套 sync_playwright().start() 会当场报 "inside the asyncio loop"。
    共享 browser、每个 env 只开自己的 context，既解开嵌套也省下每次重启的开销。
    """
    if _shared["browser"] is None or _shared["headless"] != headless:
        _shared["pw"] = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-gpu", "--mute-audio", "--no-sandbox"],
        }
        exe = resolve_browser()
        if exe:
            launch_kwargs["executable_path"] = exe
        _shared["browser"] = _shared["pw"].chromium.launch(**launch_kwargs)
        _shared["headless"] = headless
    return _shared["browser"]


def shutdown_browser() -> None:
    if _shared["browser"] is not None:
        try:
            _shared["browser"].close()
        except PlaywrightError:
            pass
    if _shared["pw"] is not None:
        try:
            _shared["pw"].stop()
        except PlaywrightError:
            pass
    _shared["browser"] = None
    _shared["pw"] = None


import atexit

atexit.register(shutdown_browser)


class BrowserEnv:
    def __init__(
        self,
        game_url: str,
        headless: bool = True,
        settle_frames: int = 6,
        ticks_per_action: int | None = None,
        viewport: tuple[int, int] = (960, 600),
        browser_executable: str | None = None,
    ) -> None:
        self.game_url = game_url
        self.headless = headless
        self.settle_frames = settle_frames
        self._ticks_per_action = ticks_per_action
        self.viewport = viewport
        self.browser_executable = browser_executable

        self._browser = None
        self._context = None
        self.page = None
        self.mode = "unstarted"
        self.page_exceptions: list[str] = []
        self.started_at = 0.0

    def __enter__(self) -> "BrowserEnv":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def start(self) -> Snapshot:
        if self.page is not None:
            return self.read()
        self._browser = shared_browser(self.headless)
        self._context = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})

        for name in INIT_SCRIPTS:
            self._context.add_init_script(path=str(PROBE_DIR / name))

        self.page = self._context.new_page()
        self.page.on("pageerror", lambda exc: self.page_exceptions.append(str(exc)))
        self.page.on("requestfailed", lambda req: self.page_exceptions.append("requestfailed:" + req.url))

        self.page.goto(self.game_url, wait_until="load")
        self._js("window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.drain(240)")
        self.started_at = time.time()

        meta_mode = self._js("window.__GSPP__ ? window.__GSPP__.mode() : 'absent'")
        if meta_mode == "absent":
            raise RuntimeError(
                "probe 未注入：目标页没有 window.__GSPP__。"
                "常见原因是游戏在 iframe 里 —— 需要把 env 指向内层 frame 的 URL。"
            )
        self.mode = meta_mode
        return self.read()

    def reset(self, seed: int) -> Snapshot:
        self.page_exceptions = []
        self._js(f"window.__GSPP__.begin({int(seed)})")
        self._advance(self._settle())
        return self.read()

    def actions(self) -> list[str]:
        raw = self._js("window.__GSPP__.actions()") or []
        return [str(a["name"]) for a in raw]

    def action_details(self) -> list[dict[str, Any]]:
        return list(self._js("window.__GSPP__.actions()") or [])

    def step(self, action: str, ticks: int | None = None) -> Snapshot:
        res = self._js(f"window.__GSPP__.apply({json.dumps(action)})") or {}
        if not res.get("ok"):
            self.page_exceptions.append("apply_failed:" + str(res.get("reason", res)))
        self._advance(ticks if ticks is not None else self._settle())
        return self.read()

    def read(self) -> Snapshot:
        snap = self._js(
            "(function(){var p=window.__GSPP__;var s=p.read();"
            "return {data:s.canonical, pos:s.pos||null, over:!!s.over, win:!!s.win,"
            " errors:p.errors(), inv:p.invariants(), clock:p.clock(), rng:p.rng(), mode:p.mode()};})()"
        )
        if snap is None:
            raise RuntimeError("read() 返回空：页面可能已崩溃或被导航走")
        self.mode = snap.get("mode", self.mode)
        err_items = [str(e.get("message")) for e in snap["errors"].get("items", [])] if snap.get("errors") else []
        all_errors = err_items + list(self.page_exceptions)
        return Snapshot(
            frame=(snap.get("clock") or {}).get("frames", -1),
            hash=digest(snap["data"]),
            pos=digest(snap["pos"]) if snap.get("pos") is not None else None,
            data=snap["data"],
            over=bool(snap.get("over")),
            win=bool(snap.get("win")),
            errors=(snap.get("errors") or {}).get("count", 0) + len(self.page_exceptions),
            violations=list(snap.get("inv") or []),
            clock=snap.get("clock") or {},
            rng=snap.get("rng") or {},
            page_exceptions=all_errors,
        )

    def snapshot_state(self) -> Any:
        return self._js("window.__GSPP__.snapshot()")

    def restore_state(self, state: Any) -> bool:
        return bool(self._js(f"window.__GSPP__.restore({json.dumps(state)})"))

    def close(self) -> None:
        # 只关自己的 page 与 context；browser 是进程级共享的，谁都不许在这里关
        for attr in ("page", "_context"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.close()
            except PlaywrightError:
                pass
            setattr(self, attr, None)
        self._browser = None

    def _settle(self) -> int:
        if self._ticks_per_action is not None:
            return self._ticks_per_action
        meta = self._js("window.__GSPP__.meta()") or {}
        return int(meta.get("ticksPerAction") or self.settle_frames)

    def _advance(self, frames: int) -> None:
        self._js(f"window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.advance({int(frames)})")

    def _js(self, expr: str) -> Any:
        try:
            return self.page.evaluate(expr)
        except PlaywrightError as exc:
            raise RuntimeError(f"evaluate 失败: {exc} (expr={expr[:80]})") from exc
