"""m10 · 创作者面游戏分析（analyze_game）：解析/空code_ref丢弃/降级/报告标注。"""
import os
import pathlib
import sys

# 强制清 NO_PROXY：环境若含 [::1] 会让 gradio import 时 httpx 解析端口崩（InvalidURL ':1]'）。
# 上传验证测试 import playtest.ui（含 gradio），须在 import 前覆盖而非 setdefault。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playtest.analyze import analyze_game, render_report_md

GAMES = pathlib.Path(__file__).resolve().parent.parent / "games" / "2048" / "original"


class FakeClient:
    def __init__(self, payload, available=True):
        self._payload = payload
        self.last_max_tokens = None
        self.cfg = type("C", (), {
            "available": lambda self: available,
            "reason_unavailable": lambda self: "缺 base_url/api_key",
        })()

    def complete(self, messages, force_json=False, max_tokens=None):
        self.last_max_tokens = max_tokens
        return self._payload


GOOD = ('{"summary":"一个数字合并 puzzle","suggestions":[{"category":"UX","problem":"无撤销",'
        '"suggestion":"加撤销按钮","priority":"中","code_ref":"js/game_manager.js:move"}]}')


def test_parse_valid():
    r = analyze_game(GAMES, None, FakeClient(GOOD))
    assert r.summary == "一个数字合并 puzzle"
    assert len(r.suggestions) == 1 and r.suggestions[0].code_ref
    assert not r.degraded


def test_drop_empty_coderef():
    bad = ('{"summary":"x","suggestions":[{"category":"UX","problem":"p","suggestion":"s",'
           '"priority":"低","code_ref":""}]}')
    r = analyze_game(GAMES, None, FakeClient(bad))
    assert r.suggestions == []


def test_degraded_when_unavailable():
    r = analyze_game(GAMES, None, FakeClient(None, available=False))
    assert r.degraded and r.suggestions == []


def test_degraded_on_complete_none():
    # 端点可用但调用失败（超时/HTTPError/解析失败 → complete 返回 None）：
    # 必须降级标注，不能静默空报告伪装实跑。
    c = FakeClient(None, available=True)
    c.cfg = type("C", (), {"available": lambda self: True,
                           "reason_unavailable": lambda self: ""})()
    r = analyze_game(GAMES, None, c)
    assert r.degraded, "complete→None 必须 degraded=True"
    assert r.suggestions == []
    md = render_report_md(r)
    assert "暂不可用" in md or "失败" in md


def test_report_md_labels_advice():
    r = analyze_game(GAMES, None, FakeClient(GOOD))
    md = render_report_md(r)
    assert "供参考" in md and "撤销" in md


def test_report_md_honest_negative():
    r = analyze_game(GAMES, None, FakeClient(GOOD))
    md = render_report_md(r)
    assert "未发现崩溃/报错" in md


def test_analyze_requests_large_token_budget():
    # 报告含多条建议，400 token 会截断成无效 JSON（实测）；分析须请求更大预算。
    c = FakeClient(GOOD)
    analyze_game(GAMES, None, c)
    assert c.last_max_tokens is not None and c.last_max_tokens >= 1000


def test_bundled_2048_self_contained():
    # P1-1 回归门：srcdoc 内联产物不得含外部 @import 或非 data: url()（相对路径会 404）。
    import re
    bundled = pathlib.Path(__file__).resolve().parent.parent / "games" / "2048" / "index.html"
    text = bundled.read_text(encoding="utf-8")
    leftover = re.findall(r"@import[^;]+;|url\((?!['\"]?data:)[^)]+\)", text)
    assert not leftover, f"bundled 2048 残留外部引用: {leftover[:5]}"


def test_upload_no_input():
    from playtest.ui import do_analyze_upload
    msg = do_analyze_upload(None, "")
    assert "请上传" in msg


def test_upload_bad_url_scheme():
    from playtest.ui import do_analyze_upload
    msg = do_analyze_upload(None, "file:///etc/passwd")
    assert "http" in msg and "❌" in msg


def test_upload_zip_slip_rejected():
    import tempfile
    import zipfile
    from playtest.ui import do_analyze_upload
    with tempfile.TemporaryDirectory() as td:
        zp = pathlib.Path(td) / "evil.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("../evil.html", "<html></html>")
        msg = do_analyze_upload(str(zp), "")
        assert "越界" in msg or "❌" in msg


def test_upload_ssrf_loopback_rejected():
    from playtest.ui import do_analyze_upload
    msg = do_analyze_upload(None, "http://127.0.0.1/game.html")
    assert "内网" in msg or "回环" in msg


def test_upload_ssrf_private_rejected():
    from playtest.ui import do_analyze_upload
    msg = do_analyze_upload(None, "http://192.168.1.1/game.html")
    assert "内网" in msg or "回环" in msg


def test_upload_zip_bomb_rejected():
    import tempfile
    import zipfile
    from playtest.ui import do_analyze_upload
    with tempfile.TemporaryDirectory() as td:
        zp = pathlib.Path(td) / "bomb.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.html", b"\0" * 21_000_000)  # 解压 21MB > 20MB 限
        msg = do_analyze_upload(str(zp), "")
        assert "20MB" in msg or "❌" in msg


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("m10_analyze: PASS")