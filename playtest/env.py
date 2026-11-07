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


_SOFTWARE_GL = ("swiftshader", "llvmpipe", "software", "mesa offscreen", "microsoft basic")


def render_warning(info: dict[str, Any]) -> str:
    """渲染后端读数 → 该不该报警的一句话。**纯函数**，负控不用开浏览器。

    为什么要有这条：无头浏览器没有 GPU 直通时会**静默**退回软件渲染，
    canvas 像素指纹因此可能大面积同色 —— 那会被"画面没动"类判据读成游戏坏了。
    报警而不是闭眼跑，是这条判据的底线。
    """
    if not info:
        return ""
    if info.get("error"):
        return f"渲染后端读不到（`{info['error']}`）⇒ canvas 像素指纹的强度未知"
    blob = f"{info.get('renderer') or ''} {info.get('vendor') or ''}".lower()
    if any(k in blob for k in _SOFTWARE_GL):
        return (f"🔴 软件渲染（`{info.get('renderer')}`）：本机没有 GPU 直通。"
                "① Canvas2D 像素指纹仍逐像素确定（我们全部位级读数都是在这同一环境下取的，"
                "录制与回放同环境 ⇒ 确定性结论不受影响）；"
                "② 但 WebGL 类游戏的画面在这里可能被软件层改写或拖慢，"
                "**跨机复现强度弱一档**，且'画面没动'类判据可能把渲染降级误判成游戏坏了；"
                "③ 换到真 GPU（如 DGX Spark）上重跑，WebGL 指纹不保证逐位一致 —— 这条要如实写进限制，"
                "不能拿本机的绿去替别人机器背书。")
    if info.get("masked"):
        return "⚠️ 只拿到掩码后的 renderer 串 ⇒ 是否软件渲染判不准，强度降一档"
    return ""


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
    # 读这一份指纹时的资产就绪读数（不参与哈希，只用于解释证据强度）
    assets: dict[str, Any] = field(default_factory=dict)

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

# 埋点/广告/统计域名。它们的请求失败是网络环境的正常结果，不是游戏缺陷。
_TRACKER_HOSTS = ("mmstat.com", "gm.mmstat", "google-analytics", "googletagmanager",
                  "doubleclick", "cnzz.com", "umeng.com", "sentry.io", "fbevents",
                  "clarity.ms", "baidu.com/hm", "gtag")


def _is_tracker(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in _TRACKER_HOSTS)


def shared_browser(headless: bool = True):
    """一个进程只起一个浏览器。

    不是优化，是正确性要求：诊断要"另开一个实例"，而它是在录制实例还开着的时候被调用的。
    Playwright 同步 API 在一个线程里只允许一个 event loop，
    嵌套 sync_playwright().start() 会当场报 "inside the asyncio loop"。
    共享 browser、每个 env 只开自己的 context，既解开嵌套也省下每次重启的开销。
    """
    import os

    if os.environ.get("PLAYTEST_BROWSER") == "disabled":
        raise RuntimeError("Chromium 已被 PLAYTEST_BROWSER=disabled 禁用")
    if _shared["browser"] is not None and not _shared["browser"].is_connected():
        # 崩掉的 Chromium 留在缓存里 = 之后每一次请求都必红，而且再也起不来。
        shutdown_browser()
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
        har_path: str | None = None,
        offline: Any = None,
        quiescence: bool = True,
        settle_step: int = 4,
        settle_rounds: int = 4,
        settle_poll_ms: int = 90,
    ) -> None:
        self.game_url = game_url
        self.headless = headless
        self.settle_frames = settle_frames
        self._ticks_per_action = ticks_per_action
        self.viewport = viewport
        self.browser_executable = browser_executable
        # har_path = 录制（联网跑一次，把响应存成包）；offline = 回放（只从磁盘回答，不联网）。
        # 两者互斥由调用方保证：拿着镜像还去录，录到的就是空包。
        self.har_path = har_path
        self.offline = offline
        self.quiescence = quiescence
        self.settle_step = settle_step
        self.settle_rounds = settle_rounds
        self.settle_poll_ms = settle_poll_ms
        self._res_prev = -1          # 上一次读到的资源条目数，用来判"还有没有请求在途"
        self.settle_unsettled = False  # 预算用完仍未静息（如实报，不当已静息用）
        self.last_pending: dict[str, Any] = {}
        self.last_settle_rounds = 0   # 静息判据实际用了几轮（0/1 = 一步就稳）

        self._browser = None
        self._context = None
        self.page = None
        self.frame = None
        self.mode = "unstarted"
        self.render: dict[str, Any] = {}        # 渲染后端自证（环境量，不参与哈希）
        self.render_note = ""                   # 软件渲染时该报警的那句话
        self.page_exceptions: list[str] = []
        self.asset_errors: list[str] = []
        self.relinks = 0  # 导航把 frame 句柄打碎后重新解析的次数
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
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": self.viewport[0], "height": self.viewport[1]}}
        if self.har_path:
            ctx_kwargs["record_har_path"] = str(self.har_path)
            ctx_kwargs["record_har_content"] = "embed"
        self._context = self._browser.new_context(**ctx_kwargs)
        # 镜像挂在 context 而不是 page 上：创空间那种"壳 + iframe"的形态里，
        # 真正加载资源的往往是子 frame。
        if self.offline is not None:
            self.offline.attach(self._context)

        for name in INIT_SCRIPTS:
            self._context.add_init_script(path=str(PROBE_DIR / name))

        self.page = self._context.new_page()
        self.page.on("pageerror", lambda exc: self.page_exceptions.append(str(exc)))
        # 🔴 第三方埋点/广告域名被拦是常态，不是游戏崩溃。
        # 实测在真实作品上，一个 gm.mmstat.com 的 tracking 请求失败
        # 就被判成了 crash 并产出了复现包 —— 那是纯误报。
        # 所以这里只收主源（与页面同源）的失败，其余单独归到 asset_errors。
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("response", self._on_response)

        self.page.goto(self.game_url, wait_until="load")
        self.frame = self._resolve_frame()
        self._js("window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.drain(240)")
        self._advance(0)   # 首屏也等资产就绪，否则第一次读到的就是"图还没画出来"那一瞬
        self.started_at = time.time()

        meta_mode = self._js("window.__GSPP__ ? window.__GSPP__.mode() : 'absent'")
        if meta_mode == "absent":
            raise RuntimeError(
                "probe 未注入：目标 frame 没有 window.__GSPP__。"
                "常见原因是应用还在更深层 iframe 里，或该 frame 跨域不可注入。"
            )
        self.mode = meta_mode
        # 渲染后端读一次就够（它是环境量，不是状态量）：软件渲染下 canvas 像素指纹
        # 会大面积同色，"画面没动"类判据会把渲染降级误判成游戏坏了 ⇒ 必须当场报警。
        self.render = self._js("window.__GSPP__.render ? window.__GSPP__.render() : {}") or {}
        self.render_note = render_warning(self.render)
        snap = self.read()
        # 🔴 静默失明是本工具最危险的失败模式：外层壳页面也是合法 DOM，
        # 探针会读到"33 个节点、零按钮、零文本"，然后如实报告"没有缺陷"。
        # 那不是"游戏没问题"，是"我们没在看游戏"。必须当场判成不可用。
        if self.is_blind(snap):
            raise RuntimeError(
                f"探针失明：目标 frame 里既没有可交互控件也没有可读文本"
                f"（frame={self.frame.url[:80] if self.frame else None}，"
                f"节点={self._js('document.querySelectorAll(\"body *\").length')}）。"
                f" 这通常意味着真正的应用在另一层 iframe 里，或页面尚未完成异步渲染。"
                f" 宁可拒绝测，也不要报一个'没有缺陷'。"
            )
        return snap

    def _on_request_failed(self, req) -> None:
        try:
            # 镜像未命中是**我们的包不全**，不是游戏的缺陷。归到 asset_errors，
            # 但调用方必须看 mirror.report()：misses 非零时这次确定性等级要降格。
            if self.offline is not None and self.offline.is_miss(req.url):
                self.asset_errors.append("mirror_miss:" + req.url[:120])
                return
            if _is_tracker(req.url):
                self.asset_errors.append("tracker_blocked:" + req.url[:120])
                return
            if not req.url.startswith(self.game_url.split("#")[0].rsplit("/", 1)[0]):
                self.asset_errors.append("cross_origin:" + req.url[:120])
                return
            self.page_exceptions.append("requestfailed:" + req.url[:160])
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        try:
            if resp.status >= 500 and not _is_tracker(resp.url):
                self.page_exceptions.append(f"http500:{resp.status}:{resp.url[:120]}")
        except Exception:
            pass

    def _resolve_frame(self):
        """挑出真正承载应用的那个 frame。

        魔搭创空间、以及任何"平台外壳 + iframe 内嵌应用"的形态，
        顶层页面只有一个壳：33 个节点、零按钮、零文本。
        不解析 frame 就等于对着壳做体检然后宣布健康。
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightError:
            pass
        best = None
        best_score = -1
        for fr in self.page.frames:
            try:
                probe = fr.evaluate(
                    "(function(){var q=function(s){return document.querySelectorAll(s).length};"
                    "return {has:!!window.__GSPP__, nodes:q('body *'), btns:q("
                    "'button,[role=button],a[href],input,select,canvas,[onclick],[tabindex]'),"
                    " texts:q('body *'), title:document.title||''};})()"
                )
            except PlaywrightError:
                continue
            if not probe or not probe.get("has"):
                continue
            score = int(probe.get("btns") or 0) * 10 + int(probe.get("nodes") or 0)
            if score > best_score:
                best_score = score
                best = fr
        return best or self.page.main_frame

    def is_blind(self, snap: Snapshot) -> bool:
        # 🔴 判模式要看 self.mode，不能去 data 里找 "mode" 字段 ——
        # 注册模式下 data 是作者自己的状态字典，根本没有 mode 键。
        # 第一版在这里写 data.get("mode")=="registered"，结果把 blocky-jump
        # 这类注册夹具全判成"失明"并拒绝测试：护栏自己咬了自己一口。
        if self.mode == "registered":
            return False
        data = snap.data if isinstance(snap.data, dict) else {}
        return (not data.get("buttons") and not data.get("texts")
                and not data.get("canvas") and not data.get("inputs"))

    def reset(self, seed: int, fresh_storage: bool = True) -> Snapshot:
        self.page_exceptions = []
        # 🔴 真实应用的进度普遍落在 localStorage 里，而种子管不到它。
        # 不先擦干净，"同一 seed 得到同一初始状态"这句话在真实作品上就是假的
        # —— 实测在牛马地下城上，第一遍写了存档，重放时界面已经不同。
        # 注意：这擦的是我们自己这个临时浏览器上下文里的副本，碰不到作者的服务器。
        if fresh_storage:
            self._js("window.__GSPP__ && window.__GSPP__.wipeStorage()")
        self._js(f"window.__GSPP__.begin({int(seed)})")
        self._advance(self._settle())
        return self.read()

    def persistent_state(self) -> Any:
        return self._js("window.__GSPP__ ? window.__GSPP__.storage() : null")

    def reboot(self, seed: int) -> Snapshot:
        """带着眼下的持久状态重新开机（不清存储）。

        复现包里的存档只有"应用在开机时读到它"才算真的装回去了：
        只往 localStorage 里写而不重载，游戏内存里仍是旧的那份，
        后面任何一次读档都会把回放推到录制里没有的分支上。
        """
        self.page.reload(wait_until="load")
        self.frame = self._resolve_frame()
        self._js(f"window.__GSPP__.begin({int(seed)})")
        self._advance(self._settle())
        return self.read()

    def load_persistent_state(self, state: Any) -> bool:
        if state is None:
            return False
        return bool(self._js(f"window.__GSPP__.loadStorage({json.dumps(state)})"))

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
            assets={**(self.last_pending or {}), "settle_unsettled": self.settle_unsettled},
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
        self.frame = None

    def _settle(self) -> int:
        if self._ticks_per_action is not None:
            return self._ticks_per_action
        meta = self._js("window.__GSPP__.meta()") or {}
        return int(meta.get("ticksPerAction") or self.settle_frames)

    def _advance(self, frames: int) -> None:
        """推进帧，并且把指纹读在**状态静息**的那一刻，而不是读在"第 n 帧"那一瞬。

        🔴 为什么按帧数读不够：`Date.now`/`performance.now`/rAF/定时器都已经被接管，
        按帧数读在这些游戏上是确定的。但浏览器还有一类异步是我们接管不到的——
        **解码与 suspense 边界**：`new Image()` 预加载、懒加载分块挂载、React 的异步边界，
        它们改的是 DOM/canvas 本身，完成时刻落在真实时间上。
        实测真实作品连续三个种子都在同一步（进入冒险的那一下）分歧，
        初始状态一致、前 18-21 步逐帧全等——正是"新场景正在出图"那一步。

        规则：推进 frames 帧，然后反复"推进一小段 + 重读哈希"，直到两次哈希相同
        或用完预算。动画类永不安息的游戏落到预算上限，而"第 K 帧"这个位置
        在被接管的时钟下本身就是确定的。
        """
        self._js(f"window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.advance({int(frames)})")
        if not self.quiescence:
            self.last_pending = self._pending()
            return
        prev = self.read().hash
        rounds = 0
        pend = self._pending()
        res_seen = pend.get("resources", -1)
        self.last_pending = pend
        self.settle_unsettled = False
        for rounds in range(1, self.settle_rounds + 1):
            # 🔴 为什么必须真的等一会儿：被接管的时钟只在**我们叫它走**的时候走，
            # "推进 16 帧"消耗的墙钟时间≈0，而图片解码是落在真实时间上的任务。
            # 只按帧数判静息，等于在解码还没落地的那一瞬间读指纹。
            # 历史读数：11-05 旧快照那枚 26 步包三次重放 20/26→20/26→21/26，分歧点自己移动。
            # 🔴 当前仍未闭合的那一例（牛马 23 步包第 18 步场景切换）**归因未定**：
            # 三遍 wall_ms 与是否分叉不相关，所以"解码漏进指纹"只是假设之一，
            # 另一条是 replay() 的存档注入分支。先做字段级诊断，别拿这段注释当结论。
            # 规则：**没有在途工作时一步都不等**（四个夹具零成本），
            # 只有 undecoded>0 / 资源数在涨 / 哈希还在变 时才睡一小段真实时间再判。
            if rounds > 1 or pend.get("undecoded", 0) or res_seen != self._res_prev:
                time.sleep(self.settle_poll_ms / 1000.0)
            self._js(f"window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.advance({int(self.settle_step)})")
            now = self.read().hash
            pend = self._pending()
            self.last_pending = pend
            quiet = (now == prev and not pend.get("undecoded", 0)
                     and pend.get("resources", -1) == res_seen)
            if quiet:
                break
            prev, res_seen = now, pend.get("resources", -1)
        else:
            # 用完预算仍不安息：要么是动画类（本来就一直动），要么是解码没落地。
            # 如实记一笔，让报告能说"这份读数是在没静息的地方取的"。
            self.settle_unsettled = bool(pend.get("undecoded", 0))
        self._res_prev = res_seen
        self.last_settle_rounds = rounds

    def _pending(self) -> dict[str, Any]:
        p = self._js("window.__GSPP__ ? window.__GSPP__.pending() : null")
        return p or {"undecoded": 0, "broken": 0, "resources": -1}

    def _js(self, expr: str, _retry: bool = True) -> Any:
        # 🔴 必须打在 frame 上而不是 page 上：创空间这类页面顶层只是壳，
        # 探针和真实 DOM 都在内层 frame。打在 page 上会读到壳的 33 个空节点。
        target = self.frame if self.frame is not None else self.page
        try:
            if self.frame is not None and self.frame.is_detached():
                raise PlaywrightError("Frame was detached (导航后旧句柄作废)")
            return target.evaluate(expr)
        except PlaywrightError as exc:
            # 真实游戏会"整页跳转"（重新开始、回主菜单、进下一关）。
            # 跳转把执行上下文换掉，旧 frame 句柄就此失效 —— 实测在牛马地下城上，
            # 三个种子全部在这里被判成 error，steps=0：那不是游戏的错，是我的句柄没跟上。
            # 这里重新解析一次 frame 再试，而不是把一局好端端的探索作废。
            msg = str(exc)
            if _retry and self.page is not None and ("destroyed" in msg or "detached" in msg):
                self.relinks += 1
                self.frame = self._resolve_frame()
                return self._js(expr, _retry=False)
            raise RuntimeError(f"evaluate 失败: {exc} (expr={expr[:80]})") from exc
