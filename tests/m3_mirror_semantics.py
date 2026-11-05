"""离线镜像的匹配语义 —— 不开浏览器，纯逻辑，快。

为什么单独一份：`tests/m3_offline.py` 证明的是「真实应用离线后逐帧全等」，
它依赖网络、浏览器和几分钟的时间；而三档命中（exact / normalized / root_doc）
以及「撞车就拒绝服务」这条守卫，必须在没有浏览器的时候也能被反复验证。
🔴 拒绝型守卫尤其要正/负两条一起跑：只测「会拒绝」，它可能把正常请求也一起拒了
   （这个坑在本项目里踩过一次 —— 失明护栏上线当天误杀了自己的正样本）。

跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m3_mirror_semantics.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playtest.offline import OfflineMirror, _norm_key  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


class FakeRequest:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url


class FakeRoute:
    """只实现 Playwright Route 的那三个动作，用来观察镜像走了哪条分支。"""

    def __init__(self, method: str, url: str) -> None:
        self.request = FakeRequest(method, url)
        self.fulfilled: dict | None = None
        self.aborted = False
        self.continued = False

    def fulfill(self, **kw) -> None:
        self.fulfilled = kw

    def abort(self, *_a) -> None:
        self.aborted = True

    def continue_(self, *_a) -> None:
        self.continued = True


def serve(mirror: OfflineMirror, url: str, method: str = "GET") -> FakeRoute:
    r = FakeRoute(method, url)
    mirror._handle(r)
    return r


def make_har(entries) -> str:
    out = []
    for method, url, status, body, mime, headers in entries:
        out.append({
            "request": {"method": method, "url": url},
            "response": {
                "status": status,
                "headers": [{"name": k, "value": v} for k, v in headers],
                "content": {"mimeType": mime, "text": body, "size": len(body)},
                "bodySize": len(body),
            },
        })
    fd, path = tempfile.mkstemp(suffix=".har")
    pathlib.Path(path).write_text(json.dumps({"log": {"entries": out}}), encoding="utf-8")
    return path


O = "https://game.test"
HTML = "text/html"
JS = "text/javascript"


def main() -> int:
    print("\n== 1. exact 档：逐字相等的 GET 直接从磁盘给字节 ==")
    p = make_har([("GET", f"{O}/app.js", 200, "console.log(1)", JS, [])])
    m = OfflineMirror(p)
    r = serve(m, f"{O}/app.js")
    check("命中并给出原字节", r.fulfilled is not None and r.fulfilled["body"] == b"console.log(1)",
          str(r.fulfilled and r.fulfilled["body"]))
    check("分档记成 exact", m.served_by["exact"] == 1, str(m.served_by))

    print("\n== 2. normalized 档：易变时间戳每次都不一样，仍然命中同一份内容 ==")
    p = make_har([
        ("GET", f"{O}/?t=1793881532392&__theme=light&backend_url=/", 200, "<html>APP</html>", HTML, []),
    ])
    m = OfflineMirror(p)
    r = serve(m, f"{O}/?t=9999999999999&__theme=dark&backend_url=/")
    check("新时间戳也命中（normalized）", r.fulfilled is not None
          and r.fulfilled["body"] == b"<html>APP</html>", str(m.served_by))
    check("归一键不吞掉语义参数 backend_url",
          "backend_url" in _norm_key(f"{O}/?t=1&backend_url=/")
          and "t=" not in _norm_key(f"{O}/?t=1&backend_url=/"), _norm_key(f"{O}/?t=1&backend_url=/"))

    print("\n== 3. 反向：语义参数不同的两个臂不许混用 ==")
    p = make_har([("GET", f"{O}/arm?a=planted", 200, "PLANTED", HTML, [])])
    m = OfflineMirror(p)
    r = serve(m, f"{O}/arm?a=clean")
    check("a=clean 没被当成 a=planted 端出去", r.aborted and not r.fulfilled)
    check("未命中被记录", len(m.misses) == 1, str(m.misses))

    print("\n== 4. 撞车守卫（负）：两条不同 URL 归一后同键且字节不同 ⇒ 拒绝服务 ==")
    p = make_har([
        ("GET", f"{O}/page?t=1", 200, "AAA", HTML, []),
        ("GET", f"{O}/page?t=2", 200, "BBB", HTML, []),
    ])
    m = OfflineMirror(p)
    check("被识别为撞车", len(m.ambiguous) == 1, str(m.ambiguous))
    r = serve(m, f"{O}/page?t=3")
    check("撞车键拒绝服务而不是随便挑一条", r.aborted and not r.fulfilled)

    print("\n== 5. 撞车守卫（正）：字节相同的两条记录不算撞车，归一照样能服务 ==")
    p = make_har([
        ("GET", f"{O}/page?t=1", 200, "SAME", HTML, []),
        ("GET", f"{O}/page?t=2", 200, "SAME", HTML, []),
    ])
    m = OfflineMirror(p)
    check("同字节撞车不误报", m.ambiguous == [], str(m.ambiguous))
    r = serve(m, f"{O}/page?t=3")
    check("归一档仍能命中", r.fulfilled is not None and r.fulfilled["body"] == b"SAME",
          str(m.served_by))

    print("\n== 6. root_doc 档：入口被 302 甩进平台壳时，裸根导航给该 origin 自己的文档 ==")
    doc_url = f"{O}/?t=111&backend_url=/"
    entries = [
        ("GET", f"{O}/", 302, "", HTML, [("Location", "https://platform.test/studios/x/y?mode=full")]),
        ("GET", doc_url, 200, "<html>THE GAME</html>", HTML, []),
        ("GET", f"{O}/assets/index-abc.js", 200, "game()", JS, []),
    ]
    m = OfflineMirror(make_har(entries))
    r = serve(m, f"{O}/")
    check("裸根命中 root_doc 并返回 200 文档",
          r.fulfilled is not None and r.fulfilled["status"] == 200
          and r.fulfilled["body"] == b"<html>THE GAME</html>", str(m.served_by))
    check("分档记成 root_doc", m.served_by["root_doc"] == 1, str(m.served_by))

    print("\n== 7. root_doc=False 时忠实重放 302，且响应头按白名单留下 ==")
    m = OfflineMirror(make_har(entries), root_doc=False)
    r = serve(m, f"{O}/")
    hdrs = (r.fulfilled or {}).get("headers", {})
    check("忠实给出 302", r.fulfilled is not None and r.fulfilled["status"] == 302,
          str(r.fulfilled and r.fulfilled["status"]))
    check("Location 被保留（丢了它导航就断在半路）",
          any(k.lower() == "location" for k in hdrs), str(hdrs))
    check("换包时不许带上原 content-length/encoding",
          not any(k.lower() in ("content-length", "content-encoding") for k in hdrs), str(hdrs))

    print("\n== 8. 严格离线：POST 一律未命中，data: 不拦 ==")
    m = OfflineMirror(make_har(entries))
    r = serve(m, f"{O}/api/save", method="POST")
    check("有服务端状态就让它显形（POST 算未命中）", r.aborted and len(m.misses) == 1)
    r = serve(m, "data:text/javascript,alert(1)")
    check("data: URL 交回浏览器，不当成缺失资源", r.continued and not r.aborted)

    print("\n== 9. forget：拆条目必须一次拆掉全部三条服务路径 ==")
    # 这条是被真实失败教出来的：端到端负控只清了 index 与 root_doc_map，
    # normalized 档还在把文档递出去，于是"断网后页面起不来"判成了假绿。
    m = OfflineMirror(make_har([
        ("GET", f"{O}/index.html?arm=1", 200, "<html>DOC</html>", HTML, []),
        ("GET", f"{O}/app.js", 200, "x", JS, []),
    ]))
    n = m.forget("index.html")
    check("forget 报出删了几条", n == 1, f"n={n}")
    check("三条路径都不再服务这份文档",
          not any(u.endswith("index.html?arm=1") for u in m.index)
          and not any(v is not None and v["body"] == b"<html>DOC</html>" for v in m.norm_index.values())
          and not any(v["body"] == b"<html>DOC</html>" for v in m.root_doc_map.values()))
    r = serve(m, f"{O}/index.html?arm=1")
    check("再请求这份文档就是未命中", r.aborted and not r.fulfilled)
    r = serve(m, f"{O}/app.js")
    check("其余条目不受影响", r.fulfilled is not None and r.fulfilled["body"] == b"x")

    failed = [x for x in results if not x[1]]
    print(f"\n{'=' * 46}\n{len(results) - len(failed)}/{len(results)} PASS")
    for label, _, detail in failed:
        print(f"  FAIL  {label}  [{detail[:120]}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
