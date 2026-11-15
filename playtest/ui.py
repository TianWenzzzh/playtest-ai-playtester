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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import gradio as gr

from .agent import POLICIES, run_episode, verify_pack
from .fixer import LLMProposer, ManualProposer, fix
from .llm import LLMClient
from .trace import PackValidationError, pack_id, save_pack, validate_pack

ROOT = pathlib.Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT / "games"
WORK = ROOT / "out" / "ui"

# 2 vCPU 上同时开两个 Chromium 会互相拖慢甚至超时；宁可排队。
_BROWSER_SLOT = threading.Semaphore(1)

# 🔴 Playwright 同步对象绑线程（实测：换线程复用直接 greenlet "Cannot switch to a different thread"），
# 而 Gradio 每个事件都丢给一个新的 anyio worker 线程 —— 于是"第二次点击就崩"。
# 所有碰浏览器的活都固定跑在这一条线程上，信号量继续管并发，这条线程管亲和。
_PW_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw")


def _pw(fn, /, *args, **kwargs):
    return _PW_THREAD.submit(fn, *args, **kwargs).result()

# 每个夹具的缺陷开关臂（与 benchmark/run.py 的 SUITE 保持同源）
ARMS: dict[str, list[tuple[str, str]]] = {
    "blocky-jump": [("有缺陷臂 maxgap=6", "?maxgap=6"), ("干净臂 maxgap=3", "?maxgap=3")],
    "maze-exit": [("有缺陷臂 keywall", "?plant=keywall"), ("干净臂 none", "?plant=none")],
    "clicker-quest": [("有缺陷臂 deadend", "?plant=deadend"), ("干净臂 none", "?plant=none")],
    "vault-run": [("分辨力臂 sealed", "?plant=sealed"), ("干净臂 none", "?plant=none")],
}

_client = LLMClient()

# 白话缺陷释义：把六类判据术语翻译成人话（界面结果区与「基准与判据」共用同一份）。
_KIND_PLAIN: dict[str, str] = {
    "crash": "游戏直接崩溃或报错弹出来了——就像 App 闪退。",
    "invariant_violation": "游戏违反了它自己声明的规则（比如血量不该为负，却变成了负数）。",
    "softlock": "卡死了：玩家既赢不了也死不掉，像走进一条永远出不去的死胡同。",
    "unwinnable": "怎么都赢不了：能到达的结局全是失败，再努力也没用。",
    "ui_dead_end": "有个按钮看着能点、点了却毫无反应——功能根本没接上。",
    "balance_break": "数值失衡：某个策略强得离谱或弱得没法玩，破坏了平衡。",
}

# 一键演示预设（借鉴研伴 EXAMPLE_CHIPS）：点一下选好场景，再点 ① 即可看 AI 找 bug。
# (chip 文案, 游戏, 臂 value, 策略, 种子, 选好后提示)
_PRESETS: list[tuple[str, str, str, str, int, str]] = [
    ("🎮 跳跃游戏 · 藏了个过不去的坎", "blocky-jump", "?maxgap=6", "curiosity", 20261105,
     "已选好「跳跃游戏」的有缺陷版本。点 **① 开始试玩**，看 AI 自己玩、自己发现那个跳不过去的坎。"),
    ("🧩 迷宫 · 有堵拿不到钥匙的墙", "maze-exit", "?plant=keywall", "curiosity", 20261105,
     "已选好「迷宫」的有缺陷版本。点 **① 开始试玩**，看 AI 怎么撞上那堵需要钥匙却拿不到的墙。"),
    ("🖱️ 点击游戏 · 有个点了没反应的按钮", "clicker-quest", "?plant=deadend", "curiosity", 20261105,
     "已选好「点击游戏」。点 **① 开始试玩**，看 AI 找出那个看着能点、实际没接功能的按钮。"),
]


def _apply_preset(game: str, arm: str, policy: str, seed: int, hint: str):
    return (gr.update(value=game), gr.update(value=arm), gr.update(value=policy),
            gr.update(value=seed), hint)


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
    inject = (f"<script>window.__GSPP_ARM__={json.dumps(_arm_dict(arm))};"
              # 🔴 预览这份内联副本时没有探针，游戏自己会喊
              # "Cannot read properties of undefined (reading 'register')"（实测进了评委的控制台）。
              # 给一个空实现把预览和检测隔开：预览只负责给人看，注册由服务端那个实例做。
              "window.__GSPP__=window.__GSPP__||{register:function(){return {};}};</script>")
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


def _static_evidence() -> str:
    path = ROOT / "evidence" / "corpus.json"
    try:
        corpus = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ("### 本地封存证据（不是本次实时运行）\n\n"
                f"⚠️ 封存台账不可读：`{type(exc).__name__}`。这里不伪造缺陷或复现读数。")
    rows = []
    for game in corpus.get("games") or []:
        confirmation = (game.get("author_confirmation") or {}).get("status") or "not_requested"
        levels = "、".join(sorted({r.get("level", "未分级") for r in game.get("replays") or []})) or "无"
        rows.append(
            f"| {game.get('title') or game.get('id')} | {len(game.get('defects') or [])} 条缺陷 | "
            f"{len(game.get('replays') or [])} 条复现记录 | `{levels}` | 作者确认：`{confirmation}` |"
        )
    body = ("\n".join(rows) if rows else "| 暂无 | 0 条缺陷 | 0 条复现记录 | — | — |")
    # 撤回之后表里是「0 条缺陷」。不披露撤回，读者会把它读成「没测出东西」——
    # 那是用省略造成的假印象。计数与指针都从台账现取，台账没有撤回块就不多说一句。
    wd = corpus.get("withdrawal") or {}
    disclosure = ""
    if wd.get("ledger"):
        disclosure = (
            f"\n\n🔴 {wd.get('claims_withdrawn', 0)} 条已公开主张全部撤回"
            "（复核判定为仪器假阳性，不是游戏缺陷）；逐枚成因、证据形态与仪器修复见 "
            f"`{wd['ledger']}`。表内计数是撤回后的现读。"
        )
    return (
        "### 本地封存证据（不是本次实时运行）\n\n"
        "以下来自 `evidence/corpus.json` 的封存台账；它证明对应留档，不代表公开验收或作者确认。\n\n"
        "| 外部作品 | 已留档发现 | 已留档回放 | 复现等级 | 证据边界 |\n|---|---:|---:|---|---|\n"
        + body
        + disclosure
    )


def do_scan(game: str, arm: str, policy: str, seed: float) -> tuple[str, str, str, str, str | None]:
    if not _BROWSER_SLOT.acquire(timeout=90):
        return ("⏳ 前面还有任务在跑（2 vCPU 只允许一个浏览器），请稍后重试。", "", "", "", None)
    try:
        url = url_for(game, arm)
        cls = POLICIES[policy]
        kw: dict[str, Any] = {"seed": int(seed)}
        if policy in ("llm", "llm_memory"):
            kw.update({"client": _client, "memory": policy == "llm_memory"})
        res = _pw(run_episode, url, int(seed), cls(**kw), max_steps=40)

        if res.error:
            return f"❌ 运行出错：`{res.error}`", "", "", "", None

        frame_html = embed(game, arm)

        if not res.defect:
            verdict = ("## 本局结论：**未开火** ✅\n\n"
                       "> 💡 **人话版**：这一局 AI 没发现 bug。"
                       + ("这是**干净臂**（正常游戏），没报错就是正确行为。" if arm.endswith("none") or "maxgap=3" in arm
                          else "这一局没找到缺陷 —— 换个种子或换种策略再试。"))
            return frame_html, verdict, _state_table(res.pack["initial_state"]["snapshot"] if res.pack else {}), \
                f"步数 {res.steps} · 见到状态 {res.distinct_states} · 探测 {res.probes} 次 · {res.wall_ms} ms", None

        d = res.defect
        verdict = (
            f"## 本局结论：**{d['kind']}** 🔴\n\n"
            f"> 💡 **人话版**：{_KIND_PLAIN.get(d['kind'], 'AI 发现了一个问题。')}\n\n"
            f"- 签名：`{d['signature']}`\n"
            f"- 首次出现帧：`{d.get('first_seen_step')}`\n"
            f"- 走到那里的动作数：`{len(res.pack['actions'])}`\n\n"
            f"**判据说明**：{d.get('detail', {}).get('note', '')}\n\n"
            "下一步点 **② 一键复现** —— 会**新开一个浏览器实例**，"
            "用下面这份初始状态 + 种子 + 动作序列重跑，逐帧比状态哈希，证明这个 bug 谁都能重现。"
        )
        stream = (f"步数 {res.steps} · 状态 {res.distinct_states} · 探测 {res.probes} 次 · "
                  f"{res.wall_ms} ms\n\n" + _decision_table(res.policy_trace or {}))
        if res.render_note:
            stream = f"{res.render_note}\n\n{stream}"

        WORK.mkdir(parents=True, exist_ok=True)
        path = save_pack(res.pack, WORK / f"{game}-{int(seed)}-{pack_id(res.pack)}.json")
        return frame_html, verdict, _state_table(d.get("detail", {}).get("state", {})), stream, str(path)
    finally:
        _BROWSER_SLOT.release()


def _note_json(reason: str) -> str:
    """🔴 gr.JSON 拿到空串会自己抛 "Invalid JSON string"（实测：评委不按顺序点就看见一屏红）。
    所有"这一步没做"的分支都要回一份能渲染的 JSON。"""
    return json.dumps({"skipped": True, "reason": reason}, ensure_ascii=False, indent=2)


def do_replay(pack_path: str | None, pasted: str) -> tuple[str, str]:
    src = pack_path if pack_path and pathlib.Path(pack_path).exists() else None
    if not src and pasted.strip():
        WORK.mkdir(parents=True, exist_ok=True)
        src = str(WORK / "pasted.json")
        pathlib.Path(src).write_text(pasted, encoding="utf-8")
    if not src:
        return ("先跑一次试玩，或把复现包 JSON 贴进来。", _note_json("还没有复现包"))
    try:
        pack = json.loads(pathlib.Path(src).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (f"复现包读不出来：`{exc}`", _note_json(f"复现包读不出来：{exc}"))
    try:
        validate_pack(pack)
    except PackValidationError as exc:
        return (f"复现包结构不完整：`{exc}`", _note_json(f"复现包结构不完整：{exc}"))

    if not _BROWSER_SLOT.acquire(timeout=90):
        return ("⏳ 浏览器槽位被占用，请稍后重试。", _note_json("浏览器槽位被占用"))
    try:
        v = _pw(verify_pack, pack["game"]["url"], pack)
    finally:
        _BROWSER_SLOT.release()

    ok = v["replayed"]
    head = ("## 回放校验：**通过** ✅\n\n" if ok else "## 回放校验：**不一致** 🔴\n\n")
    plain = ("> 💡 **人话版**：新开一个浏览器重跑，游戏逐帧走出一模一样的过程——"
             "这个 bug 铁证如山，拿这份记录谁都能重现。\n\n" if ok else
             "> 💡 **人话版**：这次重跑和原来对不上，这份记录不能当证据。\n\n")
    detail = (
        plain
        + f"- 初始状态哈希一致：`{v['initial_hash_matched']}`\n"
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
        return ("先跑一次试玩拿到复现包。", _note_json("还没有复现包"))
    if not old or not new:
        return ("补丁的 old / new 都不能为空 —— 空补丁永远不会改变判据。",
                _note_json("补丁两端都不能为空"))

    try:
        pack = json.loads(pathlib.Path(pack_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (f"复现包读不出来：`{exc}`", _note_json(f"复现包读不出来：{exc}"))
    try:
        validate_pack(pack)
    except PackValidationError as exc:
        return (f"复现包结构不完整：`{exc}`", _note_json(f"复现包结构不完整：{exc}"))

    proposer_name = "manual"
    proposer: Any = ManualProposer({"path": "index.html", "old": old, "new": new})
    if _client.cfg.available():
        proposer = LLMProposer(_client)
        proposer_name = "llm"

    if not _BROWSER_SLOT.acquire(timeout=120):
        return ("⏳ 浏览器槽位被占用。", _note_json("浏览器槽位被占用"))
    try:
        res = _pw(fix, GAMES_DIR / game, pack, proposer, WORK / "fix")
    finally:
        _BROWSER_SLOT.release()

    origin = "模型生成" if proposer_name == "llm" else "**人工提供**（本次只验证闭环机器，不代表模型的修复能力）"
    plain = ("> 💡 **人话版**：补丁打上去后，原来那个 bug 消失了（红转绿），"
             "而且换别的玩法重测也没把别处改坏。\n\n" if res.ok else
             "> 💡 **人话版**：补丁打上去后 bug 还在（仍然红），需要再想办法。\n\n")
    md = [
        f"## 修复结果：{'红转绿 ✅' if res.ok else '仍然红 🔴'}",
        plain,
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


# ── 视觉系统：深色智能体控制台（借鉴 Claude / Qoder / DeepSeek 的控制台语言）──
# 色板取自"深夜调试终端"：深海军蓝底 + 电蓝主色 + 紫罗兰次色 + 琥珀警示 + 绿成功 + 红危险。
_CSS = """
:root{
  --pt-bg:#0b0e14; --pt-surface:#12161f; --pt-surface2:#171c28; --pt-border:#242c3d;
  --pt-text:#e7eaf2; --pt-muted:#8f97ab; --pt-accent:#4f8cff; --pt-accent2:#8b7bff;
  --pt-ok:#34d399; --pt-warn:#fbbf24; --pt-danger:#f87171; --pt-cyan:#22d3ee;
}
.gradio-container{max-width:1480px!important;margin:0 auto!important;padding:20px 26px 40px!important;
  background:var(--pt-bg)!important;font-family:system-ui,-apple-system,'Segoe UI',sans-serif!important;color:var(--pt-text)!important;}
.gradio-container::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(900px 420px at 12% -8%,rgba(79,140,255,.14),transparent 60%),
             radial-gradient(800px 400px at 88% -10%,rgba(139,123,255,.12),transparent 60%),
             radial-gradient(700px 500px at 50% 110%,rgba(34,211,238,.06),transparent 60%);}
.gradio-container>*{position:relative;z-index:1;}
footer{display:none!important;}
/* header */
.pt-header{display:flex;align-items:center;gap:16px;padding:18px 22px;margin-bottom:14px;
  background:linear-gradient(120deg,#141a29,#10141d 55%,#131a2b);border:1px solid var(--pt-border);
  border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.35);}
.pt-logo{width:46px;height:46px;flex:0 0 auto;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--pt-accent),var(--pt-accent2));box-shadow:0 0 22px rgba(79,140,255,.45);}
.pt-name{font-size:22px;font-weight:800;letter-spacing:.2px;color:#fff;}
.pt-name em{font-style:normal;color:var(--pt-cyan);}
.pt-tag{font-size:12.5px;color:var(--pt-muted);margin-top:2px;}
.pt-badges{margin-left:auto;display:flex;gap:8px;}
.pt-badge{font:600 11px/1 ui-monospace,'Cascadia Code',Consolas,monospace;color:var(--pt-cyan);border:1px solid rgba(34,211,238,.35);
  background:rgba(34,211,238,.08);padding:6px 10px;border-radius:999px;}
.pt-badge.v{color:var(--pt-accent2);border-color:rgba(139,123,255,.35);background:rgba(139,123,255,.08);}
/* status banner */
.status-banner{border:1px solid var(--pt-border);border-left:3px solid var(--pt-ok);border-radius:12px;
  background:var(--pt-surface)!important;padding:10px 16px!important;margin-bottom:14px!important;}
.status-banner.warn{border-left-color:var(--pt-warn);}
.status-banner .prose,.status-banner p{font-size:13px!important;color:var(--pt-muted)!important;}
/* layout */
.console{gap:16px!important;align-items:stretch!important;}
.rail{background:var(--pt-surface)!important;border:1px solid var(--pt-border)!important;border-radius:16px!important;
  padding:18px!important;gap:12px!important;box-shadow:0 8px 26px rgba(0,0,0,.3);}
.rail-title{font:700 11px/1 ui-monospace,'Cascadia Code',Consolas,monospace;letter-spacing:.18em;color:var(--pt-muted);
  text-transform:uppercase;margin-bottom:4px;display:flex;align-items:center;gap:8px;}
.rail-title::after{content:"";flex:1;height:1px;background:var(--pt-border);}
.rail .block{background:transparent!important;border:none!important;box-shadow:none!important;padding:0!important;}
.rail label span,.rail .label{color:var(--pt-muted)!important;font-size:12px!important;font-weight:600!important;}
.rail input,.rail select,.rail .wrap-inner{background:var(--pt-surface2)!important;border:1px solid var(--pt-border)!important;
  color:var(--pt-text)!important;border-radius:10px!important;}
.rail input:focus,.rail select:focus{border-color:var(--pt-accent)!important;box-shadow:0 0 0 3px rgba(79,140,255,.18)!important;}
/* step buttons */
.step-btn{border-radius:12px!important;font-weight:700!important;font-size:14px!important;padding:12px 16px!important;
  border:1px solid var(--pt-border)!important;background:var(--pt-surface2)!important;color:var(--pt-text)!important;
  transition:transform .12s ease,box-shadow .12s ease,border-color .12s ease!important;text-align:left!important;}
.step-btn:hover{transform:translateY(-1px);border-color:var(--pt-accent)!important;box-shadow:0 6px 18px rgba(79,140,255,.25)!important;}
.step-1{background:linear-gradient(120deg,var(--pt-accent),#3b6fe0)!important;border:none!important;color:#fff!important;
  box-shadow:0 6px 20px rgba(79,140,255,.35)!important;}
.step-2{border-color:rgba(34,211,238,.4)!important;color:var(--pt-cyan)!important;}
.step-3{border-color:rgba(139,123,255,.4)!important;color:var(--pt-accent2)!important;}
/* stage + panels */
.stage-col{gap:14px!important;}
.stage{background:var(--pt-surface)!important;border:1px solid var(--pt-border)!important;border-radius:16px!important;
  padding:14px!important;box-shadow:0 8px 26px rgba(0,0,0,.3);}
.stage iframe{border:1px solid var(--pt-border)!important;border-radius:12px!important;background:#0d1119!important;}
.panel{background:var(--pt-surface)!important;border:1px solid var(--pt-border)!important;border-radius:14px!important;
  padding:14px 18px!important;box-shadow:0 6px 20px rgba(0,0,0,.25);}
.panel .prose h2{font-size:16px!important;color:#fff!important;border-bottom:1px solid var(--pt-border);padding-bottom:8px;}
.panel .prose,.panel p,.panel li{color:var(--pt-text)!important;font-size:13.5px!important;}
.panel code,.prose code{font-family:ui-monospace,'Cascadia Code',Consolas,monospace!important;background:var(--pt-surface2)!important;
  color:var(--pt-cyan)!important;border:1px solid var(--pt-border);border-radius:6px;padding:1px 6px;font-size:12px!important;}
.prose table{border-collapse:collapse!important;width:100%;}
.prose th{background:var(--pt-surface2)!important;color:var(--pt-muted)!important;font:600 12px ui-monospace,'Cascadia Code',Consolas,monospace!important;
  border:1px solid var(--pt-border)!important;padding:7px 10px!important;text-align:left;}
.prose td{border:1px solid var(--pt-border)!important;padding:7px 10px!important;color:var(--pt-text)!important;font-size:12.5px!important;}
.prose pre{background:var(--pt-surface2)!important;border:1px solid var(--pt-border)!important;border-radius:10px!important;}
.prose pre code{color:var(--pt-text)!important;border:none;background:transparent!important;}
/* tabs */
.console-tabs .tab-nav{border-bottom:1px solid var(--pt-border)!important;gap:4px!important;margin-bottom:14px!important;}
.console-tabs .tab-nav button{background:transparent!important;border:none!important;border-bottom:2px solid transparent!important;
  color:var(--pt-muted)!important;font-weight:600!important;font-size:13.5px!important;padding:10px 16px!important;border-radius:0!important;}
.console-tabs .tab-nav button.selected{color:var(--pt-cyan)!important;border-bottom-color:var(--pt-cyan)!important;
  background:linear-gradient(180deg,transparent,rgba(34,211,238,.07))!important;}
.tabitem{background:transparent!important;border:none!important;padding:0!important;}
/* textareas / json */
textarea,.code-json{background:var(--pt-surface2)!important;border:1px solid var(--pt-border)!important;color:var(--pt-text)!important;
  border-radius:10px!important;font-family:ui-monospace,'Cascadia Code',Consolas,monospace!important;font-size:12.5px!important;}
/* scrollbars */
*::-webkit-scrollbar{width:10px;height:10px;}
*::-webkit-scrollbar-track{background:var(--pt-bg);}
*::-webkit-scrollbar-thumb{background:var(--pt-border);border-radius:8px;border:2px solid var(--pt-bg);}
*::-webkit-scrollbar-thumb:hover{background:var(--pt-accent);}
.evidence{margin-top:16px!important;}
.pt-footer{margin-top:22px;text-align:center;color:var(--pt-muted);font:500 12px ui-monospace,'Cascadia Code',Consolas,monospace;}
/* 强制深色：Gradio 残留浅色的标签 pill / 文件组件 / 上传区 */
.label,label>span,.block-label,.file-preview label,.upload-container .label{background:transparent!important;color:var(--pt-muted)!important;border:none!important;}
[class*="label"]{background:transparent!important;background-color:transparent!important;background-image:none!important;color:var(--pt-muted)!important;border:none!important;box-shadow:none!important;}
[class*="label"] *{color:var(--pt-muted)!important;background:transparent!important;}
.block-label,.gradio-container label>span,span[data-testid="block-label"],.form .label{
  background:transparent!important;background-color:transparent!important;background-image:none!important;
  color:var(--pt-muted)!important;border:none!important;box-shadow:none!important;padding:0!important;}
.file-preview,.file-preview table,.file-preview td,.file-preview tr,.file-preview tbody,.upload-container,.file-upload{background:var(--pt-surface2)!important;color:var(--pt-text)!important;border-color:var(--pt-border)!important;}
.file-preview a,.file-name,.file-preview .file-name{color:var(--pt-cyan)!important;}
.file-preview .file-size,.file-preview td{color:var(--pt-muted)!important;}
.file-preview svg,.upload-container svg{fill:var(--pt-muted)!important;color:var(--pt-muted)!important;}
.selected,.tab-nav button.selected{color:var(--pt-cyan)!important;}
/* empty-state pipeline */
.pt-empty{padding:26px 22px;display:flex;flex-direction:column;gap:18px;min-height:340px;justify-content:center;}
.pt-empty-title{font-size:18px;font-weight:800;color:#fff;text-align:center;}
.pt-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}
.pt-step{min-width:0;background:var(--pt-surface2);border:1px solid var(--pt-border);border-radius:12px;
  padding:12px 12px;display:flex;flex-direction:column;gap:5px;transition:border-color .15s,transform .15s;}
.pt-step:hover{border-color:var(--pt-accent);transform:translateY(-2px);}
.pt-step-n{font:700 11px ui-monospace,monospace;color:var(--pt-cyan);border:1px solid rgba(34,211,238,.35);
  width:20px;height:20px;border-radius:6px;display:grid;place-items:center;}
.pt-step-t{font-weight:700;color:var(--pt-text);font-size:14px;}
.pt-step-d{color:var(--pt-muted);font-size:11.5px;}
.pt-step-arrow{display:none;}
@media (max-width:640px){.pt-steps{grid-template-columns:repeat(2,1fr);}}
.pt-empty-hint{color:var(--pt-muted);font-size:13px;}
.pt-empty-hint b{color:var(--pt-accent);}
/* hero：利益先行的白话开场（借鉴研伴 Hero） */
.pt-hero{display:flex;gap:20px;align-items:center;padding:22px 24px;margin-bottom:14px;
  background:linear-gradient(120deg,rgba(79,140,255,.13),rgba(139,123,255,.09) 58%,rgba(34,211,238,.06));
  border:1px solid var(--pt-border);border-radius:18px;box-shadow:0 8px 30px rgba(0,0,0,.3);}
.pt-mascot{flex:0 0 auto;width:88px;height:88px;display:grid;place-items:center;
  background:radial-gradient(circle at 50% 38%,rgba(79,140,255,.28),rgba(18,22,31,.55));
  border:1px solid var(--pt-border);border-radius:22px;}
.pt-hero-body{min-width:0;}
.pt-hero-title{font-size:25px;font-weight:800;line-height:1.32;color:#fff;}
.pt-hero-title em{font-style:normal;color:var(--pt-cyan);}
.pt-hero-sub{margin-top:8px;font-size:14px;line-height:1.75;color:var(--pt-muted);max-width:820px;}
.pt-hero-sub b{color:var(--pt-cyan);font-weight:700;}
.pt-hero-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:13px;}
.pt-chip{font-size:12.5px;color:var(--pt-text);background:var(--pt-surface2);border:1px solid var(--pt-border);
  padding:6px 12px;border-radius:999px;}
/* 三步上手引导 */
.pt-guide{margin-bottom:14px;padding:16px 20px;background:var(--pt-surface)!important;border:1px solid var(--pt-border);border-radius:16px;}
.pt-guide-title{font:700 12px ui-monospace,'Cascadia Code',Consolas,monospace;letter-spacing:.16em;color:var(--pt-cyan);
  text-transform:uppercase;margin-bottom:12px;}
.pt-guide-steps{display:flex;gap:12px;flex-wrap:wrap;}
.pt-gstep{flex:1;min-width:230px;display:flex;gap:12px;align-items:flex-start;background:var(--pt-surface2);
  border:1px solid var(--pt-border);border-radius:12px;padding:12px 14px;transition:border-color .15s,transform .15s;}
.pt-gstep:hover{border-color:var(--pt-accent);transform:translateY(-2px);}
.pt-gn{flex:0 0 auto;width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-weight:800;font-size:13px;color:#fff;
  background:linear-gradient(135deg,var(--pt-accent),var(--pt-accent2));}
.pt-gstep b{display:block;color:var(--pt-text);font-size:13.5px;margin-bottom:3px;}
.pt-gstep span{color:var(--pt-muted);font-size:12px;line-height:1.55;}
/* 一键演示预设 */
.preset-btn{border-radius:10px!important;font-size:12.5px!important;font-weight:600!important;text-align:left!important;
  padding:9px 12px!important;border:1px dashed rgba(34,211,238,.4)!important;background:rgba(34,211,238,.06)!important;
  color:var(--pt-text)!important;transition:border-color .12s,background .12s,transform .12s!important;}
.preset-btn:hover{border-color:var(--pt-cyan)!important;background:rgba(34,211,238,.14)!important;transform:translateY(-1px);}
.preset-hint,.preset-hint p{font-size:12px!important;color:var(--pt-muted)!important;line-height:1.6!important;}
.preset-hint b{color:var(--pt-cyan)!important;}
.rail .info{color:var(--pt-muted)!important;font-size:11px!important;line-height:1.5!important;opacity:.9;}
/* 空态吉祥物 + 引导语 */
.pt-empty-mascot{display:flex;justify-content:center;margin-bottom:2px;}
.pt-empty-lead{color:var(--pt-text);font-size:14px;line-height:1.75;text-align:center;max-width:660px;margin:0 auto;}
/* motion */
@keyframes pt-rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes pt-drift{0%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(2%,1%,0) scale(1.05)}100%{transform:translate3d(0,0,0) scale(1)}}
@keyframes pt-pulse{0%,100%{box-shadow:0 0 18px rgba(79,140,255,.40)}50%{box-shadow:0 0 30px rgba(34,211,238,.65)}}
@keyframes pt-blink{0%,100%{opacity:1}50%{opacity:.35}}
.gradio-container::before{animation:pt-drift 26s ease-in-out infinite;}
.pt-header{animation:pt-rise .5s ease both;}
.pt-hero{animation:pt-rise .5s .05s ease both;}
.pt-guide{animation:pt-rise .5s .1s ease both;}
.status-banner{animation:pt-rise .5s .08s ease both;}
.rail{animation:pt-rise .55s .14s ease both;}
.stage-col{animation:pt-rise .55s .2s ease both;}
.console-tabs{animation:pt-rise .55s .26s ease both;}
.evidence{animation:pt-rise .6s .32s ease both;}
.pt-logo{animation:pt-pulse 3.2s ease-in-out infinite;}
.pt-badge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--pt-ok);
  margin-right:6px;animation:pt-blink 2s ease-in-out infinite;}
.step-btn:active{transform:translateY(0) scale(.985);}
.panel{transition:border-color .15s ease,box-shadow .15s ease;}
.panel:hover{border-color:#33405a!important;box-shadow:0 8px 26px rgba(0,0,0,.4)!important;}
@media (prefers-reduced-motion: reduce){
  .gradio-container::before,.pt-logo,.pt-badge::before{animation:none!important;}
  .pt-header,.pt-hero,.pt-guide,.status-banner,.rail,.stage-col,.console-tabs,.evidence{animation:none!important;}
}
"""

_HEADER = """
<div class="pt-header">
  <div class="pt-logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="none">
    <path d="M6 4l12 8-12 8V4z" fill="#fff" opacity=".95"/>
    <circle cx="18.5" cy="5.5" r="2.4" fill="#22d3ee"/></svg></div>
  <div class="pt-brand"><div class="pt-name">Play<em>Test</em></div>
    <div class="pt-tag">AI 试玩员 · 自动玩游戏 · 找 bug · 可复现 · 给补丁</div></div>
  <div class="pt-badges"><span class="pt-badge">GSPP v0.1</span><span class="pt-badge v">可复现验证</span><span class="pt-badge">MIT</span></div>
</div>
"""


def _mascot(size: int) -> str:
    """友好的试玩员机器人（借鉴研伴 Owl 吉祥物：让冷工具有一张脸）。"""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 64 64" fill="none" aria-hidden="true">'
        '<line x1="32" y1="20" x2="32" y2="12" stroke="#8b7bff" stroke-width="2.5"/>'
        '<circle cx="32" cy="10" r="3" fill="#8b7bff"/>'
        '<rect x="9" y="30" width="5" height="12" rx="2.5" fill="#4f8cff"/>'
        '<rect x="50" y="30" width="5" height="12" rx="2.5" fill="#4f8cff"/>'
        '<rect x="14" y="20" width="36" height="30" rx="9" fill="#1b2333" stroke="#4f8cff" stroke-width="2"/>'
        '<circle cx="25" cy="33" r="4.5" fill="#22d3ee"/><circle cx="39" cy="33" r="4.5" fill="#22d3ee"/>'
        '<circle cx="26.5" cy="31.5" r="1.5" fill="#fff"/><circle cx="40.5" cy="31.5" r="1.5" fill="#fff"/>'
        '<path d="M25 42 q7 5 14 0" stroke="#34d399" stroke-width="2.5" fill="none" stroke-linecap="round"/></svg>'
    )


_CREATOR_HERO = """
<div class="pt-hero">
  <div class="pt-mascot">""" + _mascot(64) + """</div>
  <div class="pt-hero-body">
    <div class="pt-hero-title">上传你的游戏，<em>AI 帮你体检＋出优化方案</em></div>
    <div class="pt-hero-sub">AI 会自己玩一遍你的游戏、读懂它的代码，给一份<b>人话体检报告</b>：
      哪里有报错崩溃（可复现修复）、哪里能改进（玩法/平衡/UX/代码）。先拿下面的精品演示试试。</div>
    <div class="pt-hero-chips">
      <span class="pt-chip">🎮 真实精品演示 · 2048</span>
      <span class="pt-chip">📋 人话体检报告</span>
      <span class="pt-chip">💡 优化建议 · 引用代码</span>
      <span class="pt-chip">🔧 报错可复现修复</span>
    </div>
  </div>
</div>
"""

_GUIDE = """
<div class="pt-guide">
  <div class="pt-guide-title">三步上手 · 跟着点就行</div>
  <div class="pt-guide-steps">
    <div class="pt-gstep"><span class="pt-gn">1</span><div><b>点「① 开始试玩」</b><span>AI 自动玩游戏、找出藏着的 bug，画面和「人话版」结论实时出现在右边。</span></div></div>
    <div class="pt-gstep"><span class="pt-gn">2</span><div><b>点「② 一键复现」</b><span>证明这个 bug 不是偶然：新开一个浏览器重跑，逐帧一模一样。</span></div></div>
    <div class="pt-gstep"><span class="pt-gn">3</span><div><b>点「③ 自动修复」</b><span>给出修复补丁并验证「红转绿」，还会检查别处没被改坏。</span></div></div>
  </div>
</div>
"""

_EMPTY_STATE = """
<div class="pt-empty">
  <div class="pt-empty-mascot">""" + _mascot(56) + """</div>
  <div class="pt-empty-title">准备好了吗？点左边的 ① 开始试玩</div>
  <div class="pt-empty-lead">AI 会自己打开游戏、自己玩、自己找 bug。游戏画面和「人话版」结论会出现在这里。<br>
    不知道选什么？点左侧任一「一键演示」，再点 <b>① 开始试玩</b> 就能看效果。</div>
  <div class="pt-steps">
    <div class="pt-step"><span class="pt-step-n">1</span><span class="pt-step-t">读状态</span><span class="pt-step-d">看懂游戏现在的样子</span></div>
    <div class="pt-step-arrow">→</div>
    <div class="pt-step"><span class="pt-step-n">2</span><span class="pt-step-t">定动作</span><span class="pt-step-d">决定下一步按什么</span></div>
    <div class="pt-step-arrow">→</div>
    <div class="pt-step"><span class="pt-step-n">3</span><span class="pt-step-t">判缺陷</span><span class="pt-step-d">判断有没有 bug</span></div>
    <div class="pt-step-arrow">→</div>
    <div class="pt-step"><span class="pt-step-n">4</span><span class="pt-step-t">复现</span><span class="pt-step-d">证明谁都能重现</span></div>
    <div class="pt-step-arrow">→</div>
    <div class="pt-step"><span class="pt-step-n">5</span><span class="pt-step-t">修复</span><span class="pt-step-d">给出修复补丁</span></div>
    <div class="pt-step-arrow">→</div>
    <div class="pt-step"><span class="pt-step-n">6</span><span class="pt-step-t">回归</span><span class="pt-step-d">确认别处没改坏</span></div>
  </div>
  <div class="pt-empty-hint">这是 AI 试玩员的完整流水线：从「看懂游戏」到「修好并回归」，全程自动、全程可解释。</div>
</div>
"""


def _theme() -> gr.themes.Base:
    # 用内置 Soft() 作基底（其字体为安全的内置 Font 对象，避免自定义字符串 font
    # 触发 Gradio6 launch() is_custom_theme 比较崩溃，也不引外部 Google Fonts）。
    return gr.themes.Soft().set(
        body_background_fill="#0b0e14", body_text_color="#e7eaf2",
        block_background_fill="#12161f", block_border_color="#242c3d", block_label_text_color="#8f97ab",
        block_label_background_fill="transparent", block_label_border_color="transparent",
        block_label_shadow="none", block_title_text_color="#e7eaf2", border_color_primary="#242c3d", border_color_accent="#4f8cff",
        button_primary_background_fill="#4f8cff", button_primary_text_color="#ffffff",
        button_secondary_background_fill="#171c28", button_secondary_text_color="#e7eaf2",
        input_background_fill="#171c28", input_border_color="#242c3d", input_placeholder_color="#8f97ab",
        background_fill_primary="#0b0e14", background_fill_secondary="#12161f",
        color_accent_soft="#171c28", shadow_spread="0px",
    )


def _analyze_dir(gdir: pathlib.Path) -> str:
    from .analyze import analyze_game, render_report_md
    src = gdir / "original" if (gdir / "original").exists() else gdir
    index = gdir / "index.html"
    if not index.exists():
        return "❌ 找不到 index.html，请确认上传的是网页游戏（含 index.html）。"
    url = index.resolve().as_uri()
    if not _BROWSER_SLOT.acquire(timeout=90):
        return "⏳ 前面还有任务在跑（2 vCPU 只允许一个浏览器），请稍后重试。"
    try:
        ep = _pw(run_episode, url, 20261105,
                 POLICIES["curiosity"](seed=20261105), max_steps=20)
    finally:
        _BROWSER_SLOT.release()
    if ep.error:
        # 真实游戏可能含跳转外链/重载，试玩中断时仍基于代码阅读出报告（诚实标注，不伪装实跑）。
        report = analyze_game(src, None, _client)
        return render_report_md(report) + f"\n\n> ⚠️ 试玩中断（`{ep.error}`），以上建议基于代码阅读。"
    return render_report_md(analyze_game(src, ep, _client))


def do_analyze(game: str) -> str:
    return _analyze_dir(GAMES_DIR / game)


def _url_is_public(u: str) -> bool:
    """SSRF 防护：解析主机所有地址，任一为内网/回环/链路本地/保留/组播即拒。
    注：仅防直接内网地址；DNS rebinding 需固定解析 IP，超出本工具范围（已记限制）。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    host = urlparse(u).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def do_analyze_upload(file, url) -> str:
    import os
    import shutil
    import urllib.request
    import uuid
    import zipfile
    up = WORK / "uploads" / uuid.uuid4().hex[:8]
    up.mkdir(parents=True, exist_ok=True)
    if file is not None:
        fp = pathlib.Path(file)
        if fp.suffix.lower() == ".zip":
            with zipfile.ZipFile(fp) as z:
                base = up.resolve()
                total = 0
                for info in z.infolist():
                    dest = (up / info.filename).resolve()
                    if os.path.commonpath([str(base), str(dest)]) != str(base):
                        return "❌ zip 含越界路径，已拒绝。"
                    total += info.file_size
                    if total > 20_000_000:
                        return "❌ zip 解压后超过 20MB，已拒绝。"
                z.extractall(up)
        else:
            if fp.stat().st_size > 5_000_000:
                return "❌ HTML 文件超过 5MB，已拒绝。"
            shutil.copy(fp, up / "index.html")
    elif url and url.strip():
        u = url.strip()
        if not u.startswith(("http://", "https://")):
            return "❌ URL 须以 http:// 或 https:// 开头。"
        if not _url_is_public(u):
            return "❌ URL 指向内网/回环/保留地址，已拒绝（仅支持公网游戏）。"
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "PlayTest-Analyzer"})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read(2_000_001)
            if len(html) > 2_000_000:
                return "❌ 页面超过 2MB，已拒绝。"
            (up / "index.html").write_bytes(html)
        except Exception as exc:
            return f"❌ 抓取 URL 失败：`{exc}`"
    else:
        return "请上传游戏 HTML/zip，或填写游戏 URL。"
    if not (up / "index.html").exists():
        found = sorted(up.rglob("index.html"))
        if found:
            return _analyze_dir(found[0].parent)
        return "❌ 上传内容里找不到 index.html。"
    return _analyze_dir(up)


def build() -> gr.Blocks:
    names = game_names()
    arms = [(lbl, val) for lst in ARMS.values() for (lbl, val) in lst]

    with gr.Blocks(title="PlayTest · AI 试玩员") as demo:
        gr.HTML(_HEADER)
        gr.HTML(_CREATOR_HERO)
        llm_banner = gr.Markdown(_llm_banner(), elem_classes=["status-banner"])
        with gr.Row(elem_classes=["console"]):
            with gr.Column(scale=1, elem_classes=["rail"]):
                gr.HTML("<div class='rail-title'>AI 体检 · 选个游戏</div>")
                analysis_game = gr.Dropdown(["2048"] + [g for g in names if g != "2048"],
                                            value="2048", label="分析的游戏",
                                            info="选一个游戏让 AI 试玩＋读码分析。2048 是 MIT 精品演示，零接入即可分析。")
                analyze_btn = gr.Button("🔍 AI 试玩分析", variant="primary", elem_classes=["step-btn", "step-1"])
                gr.HTML("<div class='rail-title'>上传你的游戏 · 开发中也可以</div>")
                upload_file = gr.File(label="游戏 HTML 或 zip", file_types=[".html", ".zip"],
                                      file_count="single")
                upload_url = gr.Textbox(label="或游戏 URL", placeholder="https://你的游戏地址")
                upload_btn = gr.Button("📤 上传并分析", elem_classes=["step-btn", "step-2"])
            with gr.Column(scale=3, elem_classes=["stage-col"]):
                analysis_report = gr.Markdown(
                    "点左边 **🔍 AI 试玩分析**，AI 会玩一遍＋读码，给出人话体检报告（优化建议＋报错修复）。",
                    elem_classes=["panel"])
        gr.HTML("<div class='rail-title' style='margin:20px 0 10px'>开发者模式 · 完整复现闭环</div>")
        gr.HTML(_GUIDE)
        with gr.Row(elem_classes=["console"]):
            with gr.Column(scale=1, elem_classes=["rail"]):
                gr.HTML("<div class='rail-title'>一键演示 · 点一下选好场景</div>")
                preset_hint = gr.Markdown("不知道选什么？点下面任一演示，再点 **① 开始试玩**。",
                                          elem_classes=["preset-hint"])
                preset_btns = []
                for label, g, a, p, s, hint in _PRESETS:
                    preset_btns.append((gr.Button(label, elem_classes=["preset-btn"]), (g, a, p, s, hint)))
                gr.HTML("<div class='rail-title'>自定义 · 控制台</div>")
                game = gr.Dropdown(names, value=names[0] if names else None, label="夹具游戏",
                                   info="选一个要体检的小游戏（内置演示游戏，都是真实可玩的网页游戏）。")
                arm = gr.Dropdown(arms, value="?maxgap=6", label="缺陷臂",
                                  info="要不要故意埋个 bug 来演示？「有缺陷臂」=已埋 bug，看 AI 能否找出；「干净臂」=正常游戏，AI 应报告没问题。")
                policy = gr.Dropdown(sorted(POLICIES), value="curiosity", label="策略",
                                     info="AI 用什么思路玩：curiosity 好奇探索（推荐）· clue 按线索 · random 随机 · llm 靠大模型（需配置端点）。")
                seed = gr.Number(value=20261105, precision=0, label="随机种子",
                                 info="重玩编号：同一数字=同一种玩法可精确重放；换个数字=换种玩法再找。")
                gr.HTML("<div class='rail-title'>三步闭环 · 跟着点</div>")
                scan_btn = gr.Button("① 开始试玩", variant="primary", elem_classes=["step-btn", "step-1"])
                replay_btn = gr.Button("② 一键复现", elem_classes=["step-btn", "step-2"])
                fix_btn = gr.Button("③ 自动修复", elem_classes=["step-btn", "step-3"])
            with gr.Column(scale=3, elem_classes=["stage-col"]):
                embed = gr.HTML(_EMPTY_STATE, label="游戏画面", elem_classes=["stage"])
                verdict = gr.Markdown("", elem_classes=["panel"])
                with gr.Accordion("开发者细节（原始状态 / 决策流 / 技术披露）", open=False):
                    state_tbl = gr.Markdown("", elem_classes=["panel"])
                    stream = gr.Markdown("", elem_classes=["panel"])

        with gr.Tabs(elem_classes=["console-tabs"]):
            with gr.Tab("复现包"):
                pack_state = gr.State(None)
                pack_out = gr.File(label="下载的复现包")
                paste = gr.Textbox(label="或粘贴一份复现包 JSON", lines=6, placeholder='{ "schema": "gspp/repro/1", ... }')
                replay_md = gr.Markdown("", elem_classes=["panel"])
                replay_json = gr.JSON(label="verification")
            with gr.Tab("修复补丁"):
                gr.Markdown(
                    "配了推理端点时补丁由模型生成；**没配时由这里人工提供**，"
                    "此时界面会明确标注『补丁来自人工』，不会把人的成绩记到模型头上。",
                    elem_classes=["panel"])
                old = gr.Textbox(label="old（必须逐字来自源码且全文只出现一次）", lines=3,
                                 value="var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * G.maxGap);")
                new = gr.Textbox(label="new", lines=3,
                                 value="var gap = i === 0 ? 0 : 1 + Math.floor(Math.random() * Math.min(G.maxGap, G.reach));")
                fix_md = gr.Markdown("", elem_classes=["panel"])
                fix_json = gr.JSON(label="fix result")
            with gr.Tab("基准与判据"):
                gr.Markdown(
                    "## 用大白话讲，AI 能找出这 5 类问题\n\n"
                    "| 问题 | 通俗解释 | 专业代号 |\n|---|---|---|\n"
                    "| 崩溃 | 游戏直接报错 / 闪退 | `crash` |\n"
                    "| 违反规则 | 违反了游戏自己声明的规则（如血量变负） | `invariant_violation` |\n"
                    "| 卡死 | 赢不了也死不掉，像走进死胡同 | `softlock` |\n"
                    "| 赢不了 | 能到的结局全是失败，努力也没用 | `unwinnable` |\n"
                    "| 按钮没反应 | 看着能点、点了没反应，功能没接上 | `ui_dead_end` |\n\n"
                    "下面是给开发者看的精确判据（普通用户可跳过）。\n\n"
                    "## 缺陷分类\n\n"
                    "| kind | 判据 | 需要声明 |\n|---|---|---|\n"
                    "| `crash` | 未捕获异常 / 页面错误上升 | — |\n"
                    "| `invariant_violation` | 作者声明的不变量被违反 | `invariants()` |\n"
                    "| `softlock` | horizon 内到不了任何终局：赢不了也死不掉 | `snapshot/restore` + `key` |\n"
                    "| `unwinnable` | 可达终局全为失败 | 同上 |\n"
                    "| `ui_dead_end` | 控件可见可点，派发后指纹不变（含 40 帧二次确认），且整条 click 链上量不到监听器 | 兜底模式 |\n\n"
                    "🔴 探测被 horizon 或预算截断时**只报『该深度内未找到』，不断言『不存在赢的路径』**。\n\n"
                    "## 完整跑法\n"
                    "```bash\npython -m playtest.cli scan games/maze-exit --seeds 20261105\n"
                    "python -m playtest.cli replay out/scan/<pack>.json   # 不可复现则退出码 1，可进 CI\n"
                    "python -m benchmark.run                              # 两臂对照的基准表\n```",
                    elem_classes=["panel"])
                gr.Markdown(_llm_banner(), elem_classes=["status-banner"])
            with gr.Tab("接入与限制"):
                gr.Markdown(
                    "## 接入：两种模式\n\n"
                    "**注册模式（实测中位数 29 行）** —— 高保真：\n"
                    "```js\nwindow.__GSPP__.register({\n"
                    "  meta: { id: 'my-game', ticksPerAction: 6 },\n"
                    "  reset: () => newGame(),\n"
                    "  read:  () => ({ level: G.level, score: G.score, hp: G.hp }),\n"
                    "  actions: () => [{ name: 'jump' }, { name: 'wait' }],\n"
                    "  apply: (a) => a.name === 'jump' && tryJump(),\n"
                    "  isOver: (s) => s.hp <= 0 || s.won, isWin: (s) => !!s.won,\n"
                    "  key: (s) => ({ level: s.level, hp: s.hp }),   // 状态等价投影\n"
                    "  snapshot: () => G.serialize(), restore: (st) => G.loadFrom(st)\n});\n```\n\n"
                    "注册块行数**已实测**：11–48 行（中位数 29），非空非注释 11–44 行；"
                    "现读 `python tools/adapter_linecount.py`（限定词不算测量，旧稿那句「约 20」已撤）。\n\n"
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
                    "6. iframe 内游戏需把 URL 指向内层 frame；跨域 iframe 不支持。",
                    elem_classes=["panel"])
        gr.Markdown(_static_evidence(), elem_classes=["panel", "evidence"])
        gr.HTML("<div class='pt-footer'>PlayTest · 录屏不是证据，可复现才是 · MIT · evidence/corpus.json 现读</div>")

        analyze_btn.click(do_analyze, [analysis_game], [analysis_report])
        upload_btn.click(do_analyze_upload, [upload_file, upload_url], [analysis_report])
        scan_btn.click(do_scan, [game, arm, policy, seed], [embed, verdict, state_tbl, stream, pack_state]) \
            .then(lambda p: gr.update(value=p) if p else gr.update(), pack_state, pack_out)
        replay_btn.click(do_replay, [pack_state, paste], [replay_md, replay_json])
        fix_btn.click(do_fix, [game, pack_state, old, new], [fix_md, fix_json])
        for btn, (g, a, p, s, hint) in preset_btns:
            btn.click(lambda g=g, a=a, p=p, s=s, hint=hint: _apply_preset(g, a, p, s, hint),
                      None, [game, arm, policy, seed, preset_hint])
    return demo


def launch(server_name: str = "0.0.0.0", server_port: int = 7860) -> None:
    build().launch(server_name=server_name, server_port=server_port,
                   theme=_theme(), css=_CSS, allowed_paths=[str(GAMES_DIR)])
