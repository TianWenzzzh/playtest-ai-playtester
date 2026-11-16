"""把 2048 多文件源打包成自包含单 HTML（供 embed() srcdoc 内联）。可重跑。

2048 原始源在 games/2048/original/（MIT，Gabriele Cirulli）；产物 games/2048/index.html
无外部渲染依赖（script/link/css 全内联或剥离），可直接被 playtest.ui.embed() 内联进 iframe。
页脚保留 MIT 署名外链（导航链接，非渲染依赖）；完整 MIT 许可正文以 HTML 注释内嵌。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "games" / "2048" / "original"
OUT = ROOT / "games" / "2048" / "index.html"
LICENSE = ROOT / "games" / "2048" / "LICENSE.txt"

# 自包含判据：外部 @import / 非 data: url() / 非 data: link href / script src 都算渲染依赖。
SELF_CONTAINED_RE = (r"@import[^;]+;|url\((?!['\"]?data:)[^)]+\)"
                     r"|<link\b[^>]*href=\"(?!data:)[^>]*>|<script\b[^>]*src=")


def bundle() -> str:
    html = (SRC / "index.html").read_text(encoding="utf-8")
    css = (SRC / "style" / "main.css").read_text(encoding="utf-8")
    # 剥离外部 @import（fonts/clear-sans.css 只在 original/ 下，srcdoc 相对路径会 404）；
    # 字体回退到 CSS 字体栈里的系统字体，产物无外部渲染依赖。
    css = re.sub(r"@import[^;]+;", "", css)
    html = html.replace(
        '<link href="style/main.css" rel="stylesheet" type="text/css">',
        f"<style>{css}</style>",
    )
    # 剥离图标/启动图 link（favicon.ico、meta/apple-touch-* 资产未打包，srcdoc 里悬空 404）。
    html = re.sub(r"<link\b[^>]*>", "", html)

    def inline(m: re.Match) -> str:
        js = (SRC / m.group(1)).read_text(encoding="utf-8")
        return f"<script>{js}</script>"

    html = re.sub(r'<script src="(js/[^"]+)"></script>', inline, html)
    # 零接入自述标记：2048 无 GSPP 适配器，用兜底模式分析。adapter_linecount 据此归类
    # zero_integration（0 行是设计，不是抽取失败），与其余零接入夹具同口径。
    marker = "<!-- 没有任何 GSPP 相关代码：2048 演示用兜底模式零接入分析（MIT, Gabriele Cirulli） -->"
    # 完整 MIT 许可正文内嵌（独立 srcdoc 分发的合规要求），不只署名标记。
    license_comment = "<!--\n" + LICENSE.read_text(encoding="utf-8").strip() + "\n-->"
    head = "<head>\n" + marker + "\n" + license_comment
    if "<head>" in html:
        html = html.replace("<head>", head, 1)
    else:
        html = marker + "\n" + license_comment + "\n" + html
    return html


if __name__ == "__main__":
    out = bundle()
    leftover = re.findall(SELF_CONTAINED_RE, out)
    if leftover or "<link" in out:
        raise SystemExit(f"bundle 非自包含，残留外部引用: {leftover[:5]}")
    OUT.write_text(out, encoding="utf-8")
    print("bundled", OUT, OUT.stat().st_size, "bytes (self-contained, MIT embedded)")