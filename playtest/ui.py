"""创空间 Web 界面。

设计约束（不是审美选择，是平台限制逼出来的）：
  · 创空间免费档 2 vCPU / 16 GB ⇒ 同时只允许一个浏览器实例，用信号量挡；
  · 页面闲置会休眠 ⇒ 首屏不做任何重活，只列夹具；
  · 评委只有几分钟 ⇒ 主路径就是三个按钮：试玩 → 复现 → 修复。

🔴 一条诚实规矩：界面上任何"修复"都必须写明补丁来自模型还是人工。
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

import gradio as gr

from .agent import POLICIES, run_episode, verify_pack
from .env import shutdown_browser
from .fixer import LLMProposer, ManualProposer, fix
from .llm import LLMClient
from .trace import pack_id, save_pack

ROOT = pathlib.Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT / "games"
WORK = ROOT / "out" / "ui"

# 2 vCPU 上同时开两个 Chromium 会互相拖慢甚至超时；宁可排队。
_BROWSER_SLOT = threading.Semaphore(1)

# 每个夹具的缺陷开关臂（与 benchmark/run.py 的 SUITE 保持同源）
ARMS: dict[str, list[tuple[str, str]]] = {
    "blocky-jump": [("有缺陷臂 maxgap=6", "?maxgap=6"), ("干净臂 maxgap=3", "?maxgap=3")],
    "maze-exit": [("有缺陷臂 keywall", "?plant=keywall"), ("干净臂 none", "?plant=none")],
    "clicker-quest": [("有缺陷臂 deadend", "?plant=deadend"), ("干净臂 none", "?plant=none")],
}

_client = LLMClient()


def game_names() -> list[str]:
    return sorted(p.parent.name for p in GAMES_DIR.glob("*/index.html"))


def url_for(game: str, arm: str) -> str:
    return (GAMES_DIR / game / "index.html").resolve().as_uri() + arm


def web_url_for(game: str, arm: str) -> str:
    abs_path = str((GAMES_DIR / game / "index.html").resolve())
    return f"/gradio_api/file={abs_path}{arm}"


def _arm_dict(arm: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in arm.lstrip("?").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def embed(game: str, arm: str) -> str:
    """把夹具内联进 iframe，而不是让它去请求一个文件 URL。

    🔴 实测原因：`launch(allowed_paths=...)` 并没能放行 /gradio_api/file=，
    浏览器拿到 403 ⇒ 演示页只剩一个空壳，而服务本身看起来完全正常。
    这种"服务活着、内容全 403"的失败在本地点一下就能看见，在部署后才看见就晚了。

    臂的开关改成随 HTML 一起注入（window.__GSPP_ARM__），
    因为内联文档没有 query string 可用。
    """
    import html as _html

    path = GAMES_DIR / game / "index.html"
    raw = path.read_text(encoding="utf-8", errors="replace")
    inject = f"<script>window.__GSPP_ARM__={json.dumps(_arm_dict(arm))};</script>"
    if "<head>" in raw:
        raw = raw.replace("<head>", "<head>" + inject, 1)
    else:
        raw = inject + raw
    return (f'<iframe srcdoc="{_html.escape(raw, quote=True)}" width="100%" height="380" '
            f'style="border:1px solid #cbd5e1;border-radius:8px;background:#fff"></iframe>')


def _state_table(data: Any) -> str:
    if not isinstance(data, dict):
        return "```\n" + str(data)[:600] + "\n```"
    rows = "\n".join(f"| `{k}` | `{json.dumps(v, ensure_ascii=False)[:80]}` |" for k, v in data.items())
    return "| 字段 | 值 |\n|---|---|\n" + rows


def _decision_table(trace: dict[str, Any]) -> str:
    ds = trace.get("decisions") or []
    if not ds:
        return "_（本次策略不产出决策理由，见下方统计）_"
    body = "\n".join(
        f"| {d.get('i')} | `{d.get('action')}` | {d.get('source')} | {str(d.get('reason'))[:70]} |"
        for d in ds[-20:]
    )
    return "| # | 动作 | 来源 | 理由 |\n|---|---|---|---|\n" + body


def _llm_banner() -> str:
    if _client.cfg.available():
        return f"✅ 推理端点可用 · 模型 `{_client.cfg.model}`"
    return ("⚠️ **推理端点未配置** —— 当前 `llm` / `llm_memory` 两列实际等价于 `curiosity`。"
            f"原因：{_client.cfg.reason_unavailable()}。"
            "配上 `PLAYTEST_BASE_URL` / `PLAYTEST_API_KEY`（DGX Spark 上的 SGLang 或魔搭 API Inference）即为真 LLM 臂。")


def do_scan(game: str, arm: str, policy: str, seed: float) -> tuple[str, str, str, str, str | None]:
    if not _BROWSER_SLOT.acquire(timeout=90):
        return ("⏳ 前面还有任务在跑（2 vCPU 只允许一个浏览器），请稍后重试。", "", "", "", None)
    try:
        url = url_for(game, arm)
        cls = POLICIES[policy]
        kw: dict[str, Any] = {"seed": int(seed)}
        if policy in ("llm", "llm_memory"):
            kw.update({"client": _client, "memory": policy == "llm_memory"})
        res = run_episode(url, int(seed), cls(**kw), max_steps=40)

        if res.error:
            return f"❌ 运行出错：`{res.error}`", "", "", "", None

        frame_html = embed(game, arm)

        if not res.defect:
            verdict = ("## 本局结论：**未开火** ✅\n\n"
                       + ("这是**干净臂**，沉默是正确行为。" if arm.endswith("none") or "maxgap=3" in arm
                          else "这一局没找到缺陷 —— 换种子或换策略再试。"))
            return frame_html, verdict, _state_table(res.pack["initial_state"]["snapshot"] if res.pack else {}), \
                f"步数 {res.steps} · 见到状态 {res.distinct_states} · 探测 {res.probes} 次 · {res.wall_ms} ms", None

        d = res.defect
        verdict = (
            f"## 本局结论：**{d['kind']}** 🔴\n\n"
            f"- 签名：`{d['signature']}`\n"
            f"- 首次出现帧：`{d.get('first_seen_step')}`\n"
            f"- 走到那里的动作数：`{len(res.pack['actions'])}`\n\n"
            f"**判据说明**：{d.get('detail', {}).get('note', '')}\n\n"
            "下一步点 **一键复现** —— 会**新开一个浏览器实例**，"
            "用下面这份初始状态 + 种子 + 动作序列重跑，逐帧比状态哈希。"
        )
        stream = (f"步数 {res.steps} · 状态 {res.distinct_states} · 探测 {res.probes} 次 · "
                  f"{res.wall_ms} ms\n\n" + _decision_table(res.policy_trace or {}))

        WORK.mkdir(parents=True, exist_ok=True)
        path = save_pack(res.pack, WORK / f"{game}-{int(seed)}-{pack_id(res.pack)}.json")
        return frame_html, verdict, _state_table(d.get("detail", {}).get("state", {})), stream, str(path)
    finally:
        _BROWSER_SLOT.release()


def do_replay(pack_path: str | None, pasted: str) -> tuple[str, str]:
    src = pack_path if pack_path and pathlib.Path(pack_path).exists() else None
    if not src and pasted.strip():
        WORK.mkdir(parents=True, exist_ok=True)
        src = str(WORK / "pasted.json")
        pathlib.Path(src).write_text(pasted, encoding="utf-8")
    if not src:
        return ("先跑一次试玩，或把复现包 JSON 贴进来。", "")
    try:
        pack = json.loads(pathlib.Path(src).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (f"复现包读不出来：`{exc}`", "")

    if not _BROWSER_SLOT.acquire(timeout=90):
        return ("⏳ 浏览器槽位被占用，请稍后重试。", "")
    try:
        v = verify_pack(pack["game"]["url"], pack)
    finally:
        _BROWSER_SLOT.release()

    ok = v["replayed"]
    head = ("## 回放校验：**通过** ✅\n\n" if ok else "## 回放校验：**不一致** 🔴\n\n")
    detail = (
        f"- 初始状态哈希一致：`{v['initial_hash_matched']}`\n"
        f"- 动作帧哈希一致：`{v['steps_matched']}/{v['steps_total']}`\n"
        f"- mismatch：`{json.dumps(v.get('mismatch'), ensure_ascii=False)[:200]}`\n\n"
        + ("**含义**：任何人拿这份包（初始状态 + 种子 + 动作序列）重跑，"
           "都会逐帧得到同一个状态序列。这就是赛题 C 要的『可复现』。\n\n"
           "也可以把它丢进 CI：`playtest replay pack.json` 在不可复现时退出码为 1。"
           if ok else "**含义**：这份包不能保证复现，不要把它当证据。")
    )
    return (head + detail, json.dumps(v, ensure_ascii=False, indent=2))


def do_fix(game: str, pack_path: str | None, old: str, new: str) -> tuple[str, str]:
    if not pack_path or not pathlib.Path(pack_path).exists():
        return ("先跑一次试玩拿到复现包。", "")
    if not old or not new:
        return ("补丁的 old / new 都不能为空 —— 空补丁永远不会改变判据。", "")

    pack = json.loads(pathlib.Path(pack_path).read_text(encoding="utf-8"))
    proposer_name = "manual"
    proposer: Any = ManualProposer({"path": "index.html", "old": old, "new": new})
    if _client.cfg.available():
        proposer = LLMProposer(_client)
        proposer_name = "llm"

    if not _BROWSER_SLOT.acquire(timeout=120):
        return ("⏳ 浏览器槽位被占用。", "")
    try:
        res = fix(GAMES_DIR / game, pack, proposer, WORK / "fix")
    finally:
        _BROWSER_SLOT.release()

    origin = "模型生成" if proposer_name == "llm" else "**人工提供**（本次只验证闭环机器，不代表模型的修复能力）"
    md = [
        f"## 修复结果：{'红转绿 ✅' if res.ok else '仍然红 🔴'}",
        f"- 补丁来源：{origin}",
        f"- 修复前判据：`{(res.defect_before or {}).get('kind')}`",
        f"- 修复后判据：`{(res.defect_after or {}).get('kind') if res.defect_after else '不再开火'}`",
        f"- 回归（其余种子）：`{json.dumps(res.regression, ensure_ascii=False)}`",
    ]
    if res.patch:
        md.append("\n**diff**\n```diff\n- " + str(res.patch.get("old", ""))[:400] +
                  "\n+ " + str(res.patch.get("new", ""))[:400] + "\n```")
    md.append("\n" + "\n".join(f"- 备注：{n}" for n in res.notes))
    return ("\n".join(md), json.dumps(res.to_json(), ensure_ascii=False, indent=2))


def build() -> gr.Blocks:
    names = game_names()
    arms = [(lbl, val) for lst in ARMS.values() for (lbl, val) in lst]

    with gr.Blocks(title="PlayTest · AI 试玩员") as demo:
        gr.Markdown(
            "# PlayTest · AI 试玩员\n"
            "给它一个游戏，它自己玩、自己找出卡关，打包成**可以原样重放一遍**的复现包，"
            "然后自己把代码改对，改完重放确认不红。\n\n"
            "**主路径只有三个按钮**：`开始试玩` → `一键复现` → `自动修复`。"
        )
        llm_banner = gr.Markdown(_llm_banner())

        with gr.Row():
            with gr.Column(scale=2):
                game = gr.Dropdown(names, value=names[0] if names else None, label="夹具游戏")
                arm = gr.Dropdown([a[1] for a in arms], value="?maxgap=6", label="缺陷臂")
                policy = gr.Dropdown(sorted(POLICIES), value="curiosity", label="策略")
                seed = gr.Number(value=20261105, precision=0, label="随机种子")
                scan_btn = gr.Button("① 开始试玩", variant="primary")
                replay_btn = gr.Button("② 一键复现")
                fix_btn = gr.Button("③ 自动修复")
            with gr.Column(scale=3):
                embed = gr.HTML("", label="游戏画面")
                verdict = gr.Markdown("")
                state_tbl = gr.Markdown("")
                stream = gr.Markdown("")

        with gr.Tab("复现包"):
            pack_state = gr.State(None)
            pack_out = gr.File(label="下载的复现包")
            paste = gr.Textbox(label="或粘贴一份复现包 JSON", lines=6, placeholder='{ "schema": "gspp/repro/1", ... }')
            replay_md = gr.Markdown("")
            replay_json = gr.JSON(label="verification")

        with gr.Tab("修复补丁"):
            gr.Markdown(
                "配了推理端点时补丁由模型生成；**没配时由这里人工提供**，"
                "此时界面会明确标注『补丁来自人工』，不会把人的成绩记到模型头上。"
            )
            old = gr.Textbox(label="old（必须逐字来自源码且全文只出现一次）", lines=3,
                             value="var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * G.maxGap);")
            new = gr.Textbox(label="new", lines=3,
                             value="var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * Math.min(G.maxGap, G.reach));")
            fix_md = gr.Markdown("")
            fix_json = gr.JSON(label="fix result")

        with gr.Tab("基准与判据"):
            gr.Markdown(
                "## 缺陷分类\n\n"
                "| kind | 判据 | 需要声明 |\n|---|---|---|\n"
                "| `crash` | 未捕获异常 / 页面错误上升 | — |\n"
                "| `invariant_violation` | 作者声明的不变量被违反 | `invariants()` |\n"
                "| `softlock` | horizon 内到不了任何终局：赢不了也死不掉 | `snapshot/restore` + `key` |\n"
                "| `unwinnable` | 可达终局全为失败 | 同上 |\n"
                "| `ui_dead_end` | 控件可见可点，派发后指纹不变（含 40 帧二次确认） | 兜底模式 |\n\n"
                "🔴 探测被 horizon 或预算截断时**只报『该深度内未找到』，不断言『不存在赢的路径』**。\n\n"
                "## 完整跑法\n"
                "```bash\npython -m playtest.cli scan games/maze-exit --seeds 20261105\n"
                "python -m playtest.cli replay out/scan/<pack>.json   # 不可复现则退出码 1，可进 CI\n"
                "python -m benchmark.run                              # 两臂对照的基准表\n```"
            )
            gr.Markdown(_llm_banner())

        with gr.Tab("接入与限制"):
            gr.Markdown(
                "## 接入：两种模式\n\n"
                "**注册模式（约 20 行）** —— 高保真：\n"
                "```js\nwindow.__GSPP__.register({\n"
                "  meta: { id: 'my-game', ticksPerAction: 6 },\n"
                "  reset: () => newGame(),\n"
                "  read:  () => ({ level: G.level, score: G.score, hp: G.hp }),\n"
                "  actions: () => [{ name: 'jump' }, { name: 'wait' }],\n"
                "  apply: (a) => a.name === 'jump' && tryJump(),\n"
                "  isOver: (s) => s.hp <= 0 || s.won, isWin: (s) => !!s.won,\n"
                "  key: (s) => ({ level: s.level, hp: s.hp }),   // 状态等价投影\n"
                "  snapshot: () => G.serialize(), restore: (st) => G.loadFrom(st)\n});\n```\n\n"
                "**兜底模式（0 行）** —— 什么都没写的游戏自动降级："
                "从可见文本、按钮 disabled 集合、输入框值、canvas 降采样像素指纹推断状态；"
                "从可交互元素 + 常用键集推断动作。本仓库 `games/clicker-quest/` 一行协议都没接，"
                "工具照样能发现那个没有 handler 的按钮并产出可回放复现包。\n\n"
                "## 已知限制（诚实清单）\n"
                "1. 虚拟时钟把时间量化到帧栅格 ⇒ 依赖真实墙钟做手感的（节拍、连招窗口）保真度下降。\n"
                "2. `crypto.getRandomValues` 不受种子控制 ⇒ 用它随机的游戏**不可复现**，工具会明说。\n"
                "3. WebGL canvas 像素指纹跨 GPU 未必逐位一致 ⇒ 兜底模式跨机复现强度弱于注册模式。\n"
                "4. 可达性探测受 horizon/预算限制；截断时不下强结论。\n"
                "5. 状态里带单调计数器又没声明 `key` 的游戏，探测会截断而非误判 —— 宁可不报也不乱报。\n"
                "6. iframe 内游戏需把 URL 指向内层 frame；跨域 iframe 不支持。"
            )

        scan_btn.click(do_scan, [game, arm, policy, seed], [embed, verdict, state_tbl, stream, pack_state]) \
            .then(lambda p: gr.update(value=p) if p else gr.update(), pack_state, pack_out)
        replay_btn.click(do_replay, [pack_state, paste], [replay_md, replay_json])
        fix_btn.click(do_fix, [game, pack_state, old, new], [fix_md, fix_json])
    return demo


def launch(server_name: str = "0.0.0.0", server_port: int = 7860) -> None:
    build().launch(server_name=server_name, server_port=server_port,
                   theme=gr.themes.Soft(), allowed_paths=[str(GAMES_DIR)])
