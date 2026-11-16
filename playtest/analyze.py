"""创作者面游戏分析：读码＋试玩观察 → LLM 人话体检报告。

诚实口径（不可削弱）：
  · 优化建议是 LLM 主观产出，每条标「AI 建议·供参考」，且必须带 code_ref（空则丢弃，防空泛幻觉）。
  · 确定性报错/崩溃单独分区（来自 run_episode.defect，可复现）；无则如实写「未发现」（诚实负）。
  · 模型端点不可用 → degraded 标注，不伪装实跑。
  · 不冒领「AI 更会发现缺陷／试玩臂语义增量」（#36 实测负）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMClient, parse_json_object

CATEGORIES = ("玩法", "平衡", "UX", "代码")


@dataclass
class Suggestion:
    category: str
    problem: str
    suggestion: str
    priority: str
    code_ref: str


@dataclass
class AnalysisReport:
    summary: str
    detected_issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    play_stats: dict = field(default_factory=dict)
    degraded: bool = False
    degrade_reason: str = ""
    unconfigured: bool = False


def _read_sources(game_dir: Path, limit: int = 22000) -> str:
    # 优先核心游戏逻辑（玩法/平衡/输入/渲染），polyfill 与 HTML/CSS 排后，
    # 避免截断预算被兼容代码吃掉、读不到真正的游戏机制。
    # 22000 覆盖 2048 全部核心 js（实测合计 ≈20746 含头/分隔），输入/存储/瓦片不再被切。
    core = ("game_manager", "grid", "tile", "html_actuator", "keyboard_input_manager",
            "local_storage_manager", "application")

    def prio(p: Path) -> tuple:
        name = p.stem
        if p.suffix == ".js" and any(c in name for c in core):
            return (0, name)
        if p.suffix == ".js" and "polyfill" in name:
            return (3, name)
        if p.suffix == ".js":
            return (1, name)
        return (2, name)

    files = [p for p in game_dir.rglob("*")
             if p.is_file() and p.suffix in (".html", ".js", ".css")]
    parts = [f"// {p.relative_to(game_dir)}\n" + p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(files, key=prio)]
    return "\n\n".join(parts)[:limit]


def _prompt(sources: str, obs: dict) -> list:
    sys = ("你是资深游戏优化顾问。基于游戏源码与 AI 试玩观察，给出具体、可落地的优化建议。"
           "只输出 JSON。每条建议必须带 code_ref（具体 文件:行 或 试玩第N步观察到的行为），不得空泛。"
           "category 限于 玩法/平衡/UX/代码。建议是主观参考，不是缺陷断言。")
    usr = (f"游戏源码:\n{sources}\n\nAI 试玩观察:\n{obs}\n\n"
           '输出 JSON: {"summary":"一句话人话总结","suggestions":[{"category":"","problem":"",'
           '"suggestion":"","priority":"高/中/低","code_ref":""}]}')
    return [{"role": "system", "content": sys}, {"role": "user", "content": usr}]


def analyze_game(game_dir: Path, episode: Any, client: LLMClient) -> AnalysisReport:
    detected: list = []
    stats: dict = {}
    if episode is not None:
        stats = {"steps": episode.steps, "states": episode.distinct_states,
                 "reached_over": getattr(episode, "reached_over", None),
                 "reached_win": getattr(episode, "reached_win", None)}
        if getattr(episode, "defect", None):
            detected.append(episode.defect)
    if not client.cfg.available():
        return AnalysisReport(summary="（模型端点未配置，仅给出确定性检测；优化建议需配置模型）",
                              detected_issues=detected, play_stats=stats,
                              degraded=True, degrade_reason=client.cfg.reason_unavailable(),
                              unconfigured=True)
    obs = {"stats": stats, "defect": detected[0] if detected else None,
           "render_note": getattr(episode, "render_note", "") if episode else ""}
    raw = client.complete(_prompt(_read_sources(game_dir), obs), force_json=True, max_tokens=2000)
    data = parse_json_object(raw)
    if data is None:
        # 端点可用但调用失败（超时/HTTPError）或返回无法解析 ⇒ 降级标注，不静默空报告伪装实跑。
        err = getattr(getattr(client, "stat", None), "last_error", "") or "模型返回无法解析"
        return AnalysisReport(summary="（模型调用失败，仅给出确定性检测；优化建议暂不可用）",
                              detected_issues=detected, play_stats=stats,
                              degraded=True, degrade_reason=str(err)[:120])
    suggestions = []
    for s in data.get("suggestions", []):
        if not isinstance(s, dict) or not s.get("code_ref"):
            continue
        suggestions.append(Suggestion(str(s.get("category", "代码")), str(s.get("problem", "")),
                                      str(s.get("suggestion", "")), str(s.get("priority", "中")),
                                      str(s.get("code_ref", ""))))
    return AnalysisReport(summary=str(data.get("summary", "")), detected_issues=detected,
                          suggestions=suggestions, play_stats=stats)


def render_report_md(r: AnalysisReport) -> str:
    lines = ["## 🎮 AI 体检报告", "", r.summary or "（无总结）", ""]
    if r.play_stats:
        lines.append(f"AI 试玩了 {r.play_stats.get('steps', 0)} 步，见到 {r.play_stats.get('states', 0)} 种状态。")
        lines.append("")
    lines.append("### 🔴 检测到的报错/崩溃（确定性，可复现）")
    if r.detected_issues:
        for d in r.detected_issues:
            lines.append(f"- `{d.get('kind')}`：{d.get('detail', {}).get('note', '')}")
    else:
        lines.append("- 未发现崩溃/报错。（诚实负：不代表证明无缺陷，只是本次视界内未发现）")
    lines.append("")
    lines.append("### 💡 优化建议（AI 建议·供参考）")
    if r.degraded:
        if r.unconfigured:
            lines.append(f"- ⚠️ 模型端点未配置，优化建议暂不可用（{r.degrade_reason}）。")
        else:
            lines.append(f"- ⚠️ 模型调用失败，优化建议暂不可用（{r.degrade_reason}）。")
    elif r.suggestions:
        for s in r.suggestions:
            lines.append(f"- **[{s.category}·{s.priority}]** {s.problem} → {s.suggestion}（`{s.code_ref}`）")
    else:
        lines.append("- 本次未产出具体建议。")
    return "\n".join(lines)