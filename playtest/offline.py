"""离线取包 —— 把"线上的一次可玩"变成"本地可重复的夹具"。

**为什么需要它**（06 §8.6）：真实第三方作品几乎都在跑异步 —— 图片、字体、动画的到达顺序
会漂，于是同一 (seed, 动作序列) 两次跑不出逐帧全等，复现等级只能降到 `async_dependent`。
虚拟时钟能吃掉 rAF 和定时器，但吃不掉"响应什么时候到"。

⚠️ 11-05 夜的更正：当时我把真实作品上的分歧**全部**归给了这一层，这是错的。
其中至少两处成因在我自己这边；把回放路径对齐之后曾取得单次 `replayed=true`（23/23）。
🔴 11-06 深夜再更正：在同一份输入 HAR 上把当时台账里的 6 枚包各重放三遍，**5 枚逐值一致**，
作品 A（脱敏代号） 23 步那枚第一遍在第 18 步（场景切换）分叉 ⇒ 它现在如实记 `async_dependent`。
逐遍读数与输入快照哈希见 `evidence/ledger/*_repeat3.json`，`tools/corpus.py` 现校验。
🔴 11-08 撤回：那批读数出自双派发 click 修复之前的探针；修好后其中 3 枚在开关控件那一步
确定性失配，已从台账移除（不记 async_dependent —— 失配绑定的是仪器版本，不是游戏的异步）。
台账现存 3 枚：2 枚 bit_reproducible ＋ 1 枚 async_dependent。
逐枚处置见 evidence/ledger/2026-11-08_ui_dead_end_withdrawal.json。
26 步那枚属 11-05 旧快照（20/26→20/26→21/26，分歧点会移动），
**跨快照包身份不同，只作历史**，不许拿来给当前树背书。
剩下的异步依赖是真的，但它不再是"这条结论的天花板"，而是要逐条测出来的东西。

**做法**：用 Playwright 把整站响应原样录成一个 `.har`，之后每次都从磁盘回答。三个好处：
  1. 网络延迟这个非确定源被摘掉；
  2. URL 和 origin 一个字都不用改 —— 游戏仍然认为自己在原域名上，
     `localStorage` 的键、任何依赖 origin 的逻辑都与线上一致（对比：镜像到本地目录
     再起 http.server 就会换 origin）；
  3. 零联网 ⇒ 不会再消耗作者的推理额度。

**代价，而且是明说的**：镜像只覆盖"录制时真的被请求过"的资源。
未命中**不回源**（回源等于把非确定源请回来），而是当场断掉并计入 misses，
由调用方决定这次读数还信不信。 misses 非零的 episode，其确定性等级必须降格。
"""
from __future__ import annotations

import base64
import json
import pathlib
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .env import shared_browser

# 每次运行都会变的查询参数（缓存击穿 / 主题注入）。它们不属于应用语义，
# 归一化时剥掉；其余参数（比如 ?maxgap= 、?backend_url= 这种会改行为的）一律保留。
_VOLATILE_PARAMS = {"t", "_t", "ts", "_ts", "timestamp", "nonce", "_", "__theme", "cachebust"}

# fulfill 时不能带回去的响应头：长度/编码由 Playwright 重算，
# 带上原值会让浏览器按 gzip 的长度去解一段已经解压好的字节。
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding",
                          "connection", "keep-alive", "content-security-policy-report-only"}


def _norm_key(url: str) -> str:
    """去掉易变参数并排序后的 URL —— 第二档匹配的键。"""
    sp = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
            if k.lower() not in _VOLATILE_PARAMS]
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(sorted(kept)), ""))


def _prefetch_declared(frame) -> int:
    """把"文档里声明了、但 headless 浏览器不会主动去取"的资源补进包里。

    最典型的是 favicon：`<link rel=icon>` 写在文档里，但无头标签页不会请求它，
    于是离线重放时它会变成未命中 —— 那是包的缺陷，不是游戏的缺陷。
    只补同源资源，跨源的装饰不归我们塞。

    要逐 frame 做：创空间形态下应用文档在 iframe 里，
    顶层那份是平台壳，扫顶层永远扫不到游戏自己声明的那些链接。
    """
    try:
        return frame.evaluate(
            """() => {
                const urls = new Set();
                document.querySelectorAll('link[href],img[src],script[src]').forEach((el) => {
                    const u = el.href || el.src;
                    if (u && u.startsWith(location.origin)) urls.add(u);
                });
                urls.forEach((u) => fetch(u, { cache: 'force-cache' }));
                return urls.size;
            }"""
        )
    except Exception:  # noqa: BLE001 - 跨源/已销毁的 frame 扫不到就跳过，不影响取包
        return 0


def capture(
    url: str,
    har_path: str | pathlib.Path,
    *,
    settle_ms: int = 3000,
    warm: Callable[[Any], None] | None = None,
    headless: bool = True,
    prefetch_declared: bool = True,
    mirror: "OfflineMirror | None" = None,
) -> dict[str, Any]:
    """联网跑一次，把所有响应录进 har。

    故意**不注入探针脚本**：镜像应当是"真人打开这个页面所加载的那份内容"，
    注入虚拟时钟会改变资源请求的集合，录出来的就不是同一个包了。
    warm 是可选的热身回调（点几下菜单，让懒加载的分块也进包），由调用方决定要不要点 ——
    会调云端模型的应用，点一次就花作者一次额度，所以默认不点。
    """
    har_path = pathlib.Path(har_path)
    har_path.parent.mkdir(parents=True, exist_ok=True)
    if har_path.exists():
        har_path.unlink()

    browser = shared_browser(headless)
    ctx = browser.new_context(
        viewport={"width": 1100, "height": 760},
        record_har_path=str(har_path),
        record_har_content="embed",
    )
    page = ctx.new_page()
    if mirror is not None:
        # 补包模式：已知字节从磁盘给（快，且不重复消耗线上），未命中放行去线上取
        mirror.attach(ctx)
    page.goto(url, wait_until="load", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(settle_ms)
    declared = 0
    if prefetch_declared:
        for fr in page.frames:
            declared += _prefetch_declared(fr)
        page.wait_for_timeout(1500)
    if warm:
        warm(page)
        page.wait_for_timeout(settle_ms)
    # close 才会把 har 落盘（Playwright 语义：context 关闭时写文件）
    ctx.close()
    out = summarize(har_path)
    out["declared_prefetched"] = declared
    return out


def warm_clicks(page, rounds: int = 4, per_round: int = 5, host: str | None = None) -> int:
    """录制期热身：点几下界面，把懒加载的代码分块与美术也请求出来。

    这一步决定镜像的覆盖面有多宽 —— 只录首屏的包，跑到第二屏就会未命中。
    `host` 用来把点击限制在应用自己的 frame 里（平台壳上的"登录""点赞"不该碰）。
    只对纯客户端的应用用：会调云端模型的那些，点一次就花作者一次额度，
    宁可让包小一点，也不替别人烧额度。
    """
    clicked = 0
    for _ in range(rounds):
        frames = [f for f in page.frames
                  if (host is None or (f.url or "").startswith(host))]
        for fr in frames:
            try:
                btns = fr.query_selector_all("button")
            except Exception:  # noqa: BLE001 - frame 可能在这一轮里被换掉
                continue
            for b in btns[:per_round]:
                try:
                    if b.is_visible():
                        b.click(timeout=800)
                        clicked += 1
                        page.wait_for_timeout(350)
                except Exception:  # noqa: BLE001 - 点不动的按钮不算失败
                    pass
    return clicked


def summarize(har_path: str | pathlib.Path) -> dict[str, Any]:
    p = pathlib.Path(har_path)
    har = json.loads(p.read_text(encoding="utf-8"))
    entries = har["log"]["entries"]
    methods: dict[str, int] = {}
    types: dict[str, int] = {}
    wire = 0
    for e in entries:
        m = e["request"]["method"].upper()
        methods[m] = methods.get(m, 0) + 1
        mt = (e["response"].get("content", {}) or {}).get("mimeType", "?")
        key = str(mt).split(";")[0]
        types[key] = types.get(key, 0) + 1
        size = e["response"].get("_transferSize") or e["response"].get("bodySize") or 0
        wire += max(int(size), 0)
    return {
        "har": str(p),
        "bytes": p.stat().st_size,
        "wire_bytes": wire,
        "entries": len(entries),
        "methods": methods,
        "mime_types": types,
    }


class OfflineMirror:
    """把 har 变成"只从磁盘回答"的路由器。

    只服务 GET：POST/PUT 一律算未命中 —— 那意味着应用有服务端状态，
    离线包天然兜不住，这种情况必须被看见而不是被假装跑通。

    命中分三档，各自单独计数，因为它们的可信度不一样：
      exact      —— URL 逐字相等，最强；
      normalized —— 剥掉易变参数后相等（实测创空间那个 iframe 每次带一个新时间戳），次强；
      root-doc   —— 裸根导航命中"该 origin 自己的那份 HTML 文档"，
                    用于绕过"平台壳 302 把自己重定向回来"的循环；这一档只改导航入口，不改任何资源字节。
    """

    def __init__(self, har_path: str | pathlib.Path, root_doc: bool = True,
                 on_miss: str = "abort") -> None:
        """on_miss="abort" 是严格离线（默认）；"continue" 只给补包用：
        已知字节仍从磁盘回答，未命中的放行去线上取，顺便被录制下来。"""
        self.on_miss = on_miss
        self.root_doc = root_doc
        har = json.loads(pathlib.Path(har_path).read_text(encoding="utf-8"))
        self.index: dict[str, dict[str, Any]] = {}
        for e in har["log"]["entries"]:
            req, resp = e["request"], e["response"]
            if req["method"].upper() != "GET":
                continue
            content = resp.get("content", {}) or {}
            text = content.get("text")
            if text is None:
                body = b""
            elif content.get("encoding") == "base64":
                body = base64.b64decode(text)
            else:
                body = text.encode("utf-8")
            headers = {
                h["name"]: h["value"] for h in (resp.get("headers") or [])
                if str(h["name"]).lower() not in _DROP_RESPONSE_HEADERS
            }
            self.index[req["url"]] = {
                "status": resp.get("status", 200),
                "body": body,
                "content_type": (content.get("mimeType") or "application/octet-stream").split(";")[0],
                "headers": headers,
            }

        self._rebuild_tiers()

        self.served = 0
        self.passthrough = 0
        self.served_by = {"exact": 0, "normalized": 0, "root_doc": 0}
        self.misses: list[str] = []

    def _rebuild_tiers(self) -> None:
        """从 index 重算另外两条服务路径。

        🔴 归一档和裸根替补档都是从 index 派生的。索引被改动后若不重算，
        "删掉条目就断供"这句话只对三分之一成立 —— 实测就是这样被负控抓到的
        （只清了 index 与 root_doc_map，normalized 档还在偷偷把文档递出去）。
        """
        self.norm_index = {}
        self.ambiguous = []
        norm_src: dict[str, str] = {}
        for url, val in self.index.items():
            key = _norm_key(url)
            if key in norm_src and norm_src[key] != url:
                if self.index[norm_src[key]]["body"] == val["body"]:
                    continue
                self.ambiguous.append(key)
                self.norm_index[key] = None
                continue
            if key not in norm_src:
                norm_src[key] = url
                self.norm_index[key] = val
        self.ambiguous = sorted(set(self.ambiguous))

        # 每个 origin 自己的文档：同源 html 里 path+query 最短的那条
        self.root_doc_map = {}
        if self.root_doc:
            best: dict[str, tuple[int, str]] = {}
            for url, val in self.index.items():
                if val["status"] != 200 or "html" not in val["content_type"]:
                    continue
                sp = urlsplit(url)
                origin = f"{sp.scheme}://{sp.netloc}"
                score = len(sp.path) + len(sp.query)
                if origin not in best or score < best[origin][0]:
                    best[origin] = (score, url)
            self.root_doc_map = {o: self.index[u] for o, (_, u) in best.items()}

    def forget(self, needle: str) -> int:
        """从**全部三条**服务路径里删掉含 needle 的条目 —— 给负控用。

        负控必须一次拆掉所有能把它端出去的路径；手工清两处而漏第三处，
        测出来的"绿"是给仪器自己发的假证书。
        """
        dropped = [u for u in self.index if needle in u]
        for u in dropped:
            del self.index[u]
        if dropped:
            self._rebuild_tiers()
        return len(dropped)

    def attach(self, page_or_context) -> None:
        page_or_context.route("**/*", self._handle)

    def _handle(self, route) -> None:
        req = route.request
        url = req.url
        if url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        hit = None
        tier = ""
        if req.method.upper() == "GET":
            sp = urlsplit(url)
            bare = sp.path in ("", "/") and not sp.query
            exact = self.index.get(url)
            if bare:
                # 裸根导航：若镜像里这一条本来是个 302（平台壳把创空间重定向回自己的
                # studios 页），就直接给出该 origin 自己的文档。
                # 否则离线重放会绕回平台 SPA，而平台那侧的鉴权/接口不在包里。
                doc = self.root_doc_map.get(f"{sp.scheme}://{sp.netloc}")
                if doc is not None and (exact is None or 300 <= exact["status"] < 400):
                    hit, tier = doc, "root_doc"
            if hit is None and exact is not None:
                hit, tier = exact, "exact"
            if hit is None:
                cand = self.norm_index.get(_norm_key(url))
                if cand is not None:
                    hit, tier = cand, "normalized"
        if hit is None:
            if self.on_miss == "continue":
                self.passthrough += 1
                route.continue_()
                return
            self.misses.append(f"{req.method}:{url}"[:200])
            route.abort()
            return
        self.served += 1
        self.served_by[tier] += 1
        route.fulfill(status=hit["status"] if tier != "root_doc" else 200,
                      headers=hit["headers"], content_type=hit["content_type"],
                      body=hit["body"])

    def is_miss(self, url: str) -> bool:
        return any(url in m for m in self.misses)

    def report(self) -> dict[str, Any]:
        uniq = sorted({m.split(":", 1)[1] for m in self.misses})
        return {
            "entries": len(self.index),
            "served": self.served,
            "served_by": dict(self.served_by),
            "ambiguous_norm_keys": len(self.ambiguous),
            "miss_calls": len(self.misses),
            "distinct_misses": len(uniq),
            "miss_urls": uniq,
            "miss_examples": uniq[:8],
            "passthrough": self.passthrough,
        }


def harvest_episodes(url: str, har_path: str | pathlib.Path, *, seeds: list[int],
                     max_steps: int = 40) -> dict[str, Any]:
    """让探索自己把缺的字节请出来，再合并回包。

    为什么点击热身不够：`--warm-clicks` 只会按顺序点前几个按钮，它走不到策略会走到的地方。
    实测就是这样卡住真实作品的位级复现——进入战斗那一步要懒加载 `Card-*.js`，
    包里却没有，于是未命中的**中止时机**（真实时间）决定了那一刻的 DOM，
    同一份 (seed, 动作序列) 跨会话给出不同轨迹：覆盖率不够会伪装成"异步不可复现"。

    机制：`BrowserEnv` 同时支持 `offline`（磁盘供字节 + 未命中放行）与 `har_path`（录制），
    所以一局真实探索本身就能产出一份"走到过哪里"的 har，合并即可。
    """
    from .agent import run_episode
    from .policies import CuriosityPolicy

    har_path = pathlib.Path(har_path)
    files = [har_path]
    rows = []
    for seed in seeds:
        fresh = har_path.parent / f"{har_path.stem}.ep{seed}.har"
        res = run_episode(url, seed, CuriosityPolicy(seed), max_steps=max_steps,
                          offline=OfflineMirror(har_path, on_miss="continue"),
                          har_path=str(fresh))
        rows.append({"seed": seed, "steps": res.steps,
                     "defect": (res.defect or {}).get("kind"), "error": res.error})
        if fresh.exists():
            files.append(fresh)
    merged = merge_hars(files, har_path)
    for f in files[1:]:
        f.unlink(missing_ok=True)
    return {"episodes": rows, "merged": merged,
            "after": OfflineMirror(har_path).report()}


def merge_hars(paths: list[str | pathlib.Path], out: str | pathlib.Path) -> dict[str, Any]:
    """按 (method, url) 去重合并若干份 har。

    后出现的优先：补包时新取到的字节更贴近探索那一刻的界面。
    只保留有内容可给的条目（status 200 且带 body），其余（-1/401/302）仍保留 302 与 401
    ——它们记录了"这个应用确实做过这件事"，丢掉就等于伪造覆盖面。
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for p in paths:
        har = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
        for e in har["log"]["entries"]:
            key = (e["request"]["method"].upper(), e["request"]["url"])
            seen[key] = e
    entries = list(seen.values())
    out = pathlib.Path(out)
    out.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}},
                              ensure_ascii=False), encoding="utf-8")
    return {"entries": len(entries), "bytes": out.stat().st_size, "har": str(out)}


def _origin_of(url: str) -> str:
    sp = urlsplit(url)
    return f"{sp.scheme}://{sp.netloc}"


def top_up(url: str, har_path: str | pathlib.Path, *, rounds: int = 6, per_round: int = 8,
           settle_ms: int = 2500, host: str | None = None) -> dict[str, Any]:
    """补包：用现有镜像供已知字节、未命中放行联网并被录制，走过一遍更深的界面再合并回包。

    为什么需要：只录首屏的包跑到第二屏就会未命中，而未命中的是**代码分块**时
    那一轮读数直接作废（实测：卡组/战斗界面的 Card-*.js 不在包里，探索就到不了那里，
    于是"真实作品离线开火"这一腿被覆盖面卡住，不是被算法卡住）。
    """
    har_path = pathlib.Path(har_path)
    before = OfflineMirror(har_path).report()
    fresh = har_path.parent / (har_path.stem + ".topup.har")
    filler = OfflineMirror(har_path, on_miss="continue")
    info = capture(url, fresh, settle_ms=settle_ms,
                   warm=lambda p: warm_clicks(p, rounds=rounds, per_round=per_round,
                                              host=host or _origin_of(url)),
                   mirror=filler)
    merged = merge_hars([har_path, fresh], har_path)
    after = OfflineMirror(har_path).report()
    fresh.unlink(missing_ok=True)
    return {"before": before, "after": after, "filled": filler.report(),
            "recaptured": info, "merged": merged}
