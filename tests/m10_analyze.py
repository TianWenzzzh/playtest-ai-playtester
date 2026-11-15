"""m10 · 创作者面游戏分析（analyze_game）：解析/空code_ref丢弃/降级/报告标注。"""
import pathlib
import sys

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("m10_analyze: PASS")