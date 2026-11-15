"""把 2048 多文件源打包成自包含单 HTML（供 embed() srcdoc 内联）。可重跑。

2048 原始源在 games/2048/original/（MIT，Gabriele Cirulli）；产物 games/2048/index.html
无外部 src/href 依赖，可直接被 playtest.ui.embed() 内联进 iframe。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "games" / "2048" / "original"
OUT = ROOT / "games" / "2048" / "index.html"


def bundle() -> str:
    html = (SRC / "index.html").read_text(encoding="utf-8")
    css = (SRC / "style" / "main.css").read_text(encoding="utf-8")
    html = html.replace(
        '<link href="style/main.css" rel="stylesheet" type="text/css">',
        f"<style>{css}</style>",
    )

    def inline(m: re.Match) -> str:
        js = (SRC / m.group(1)).read_text(encoding="utf-8")
        return f"<script>{js}</script>"

    html = re.sub(r'<script src="(js/[^"]+)"></script>', inline, html)
    # 零接入自述标记：2048 无 GSPP 适配器，用兜底模式分析。adapter_linecount 据此归类
    # zero_integration（0 行是设计，不是抽取失败），与其余零接入夹具同口径。
    marker = "<!-- 没有任何 GSPP 相关代码：2048 演示用兜底模式零接入分析（MIT, Gabriele Cirulli） -->"
    if "<head>" in html:
        html = html.replace("<head>", "<head>\n" + marker, 1)
    else:
        html = marker + "\n" + html
    return html


if __name__ == "__main__":
    OUT.write_text(bundle(), encoding="utf-8")
    print("bundled", OUT, OUT.stat().st_size, "bytes")