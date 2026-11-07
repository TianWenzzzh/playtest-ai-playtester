"""本地 UI 发布腿：真实 Gradio、两条三按钮序列、故障降级与仪器负控。"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "out" / "release_ui" / (
    datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
)
KINDS = ("crash", "invariant_violation", "softlock", "unwinnable", "ui_dead_end")
RESULTS: list[dict[str, Any]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"label": label, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


def sequence_issues(name: str, data: dict[str, str]) -> list[str]:
    issues: list[str] = []
    if "本局结论" not in data["scan"] or not any(k in data["scan"] for k in KINDS):
        issues.append(f"{name} scan")
    if "回放校验：通过" not in data["replay"] or "bit_reproducible" not in data["replay"]:
        issues.append(f"{name} replay")
    if "修复结果：红转绿" not in data["fix"] or "人工提供" not in data["fix"]:
        issues.append(f"{name} fix")
    if any("运行出错" in data[key] for key in ("scan", "replay", "fix")):
        issues.append(f"{name} runtime")
    return issues


def console_issues(errors: list[dict[str, str]]) -> list[str]:
    return ["console/pageerror"] if errors else []


def wait_http(proc: subprocess.Popen[bytes], url: str, log_path: pathlib.Path) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
    raise RuntimeError(f"Gradio 未就绪，rc={proc.poll()}，日志尾：{tail}")


@contextmanager
def gradio_server(label: str, extra_env: dict[str, str]) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    for key in ("PLAYTEST_BASE_URL", "PLAYTEST_API_KEY", "MODELSCOPE_API_TOKEN"):
        env.pop(key, None)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        **extra_env,
    })
    log_path = OUT / f"server-{label}.log"
    command = [
        sys.executable,
        "-c",
        f"from playtest.ui import launch; launch(server_name='127.0.0.1', server_port={port})",
    ]
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            url = f"http://127.0.0.1:{port}/"
            wait_http(proc, url, log_path)
            yield url
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


def attach_error_recorder(page: Any, errors: list[dict[str, str]]) -> None:
    def on_console(message: Any) -> None:
        if message.type == "error":
            errors.append({"type": "console", "text": message.text})

    page.on("console", on_console)
    page.on("pageerror", lambda error: errors.append({"type": "pageerror", "text": str(error)}))


def open_page(browser: Any, url: str, errors: list[dict[str, str]]) -> Any:
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    attach_error_recorder(page, errors)
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.get_by_role("button", name="① 开始试玩", exact=True).wait_for(
        state="visible", timeout=60_000,
    )
    return page


def body_text(page: Any) -> str:
    return page.locator("body").inner_text()


def wait_body(page: Any, needles: tuple[str, ...], timeout_s: int = 240) -> str:
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = body_text(page)
        if any(needle in text for needle in needles):
            return text
        page.wait_for_timeout(250)
    raise AssertionError(f"等待页面结果超时：{needles}；文本尾：{text[-800:]}")


def pick(page: Any, label: str, value: str) -> None:
    native = page.locator(f'select[aria-label="{label}"]')
    if native.count():
        native.select_option(label=value)
        return
    field = page.locator(f'input[aria-label="{label}"]').first
    field.wait_for(state="visible", timeout=20_000)
    field.click()
    option = page.get_by_role("option").filter(has_text=value).first
    option.wait_for(state="visible", timeout=20_000)
    option.click()


def open_tab(page: Any, name: str) -> None:
    page.get_by_role("tab", name=name, exact=True).click()


def run_sequence(
    browser: Any,
    url: str,
    name: str,
    policy: str,
    seed: int,
    errors: list[dict[str, str]],
) -> dict[str, str]:
    page = open_page(browser, url, errors)
    try:
        pick(page, "夹具游戏", "blocky-jump")
        pick(page, "缺陷臂", "有缺陷臂 maxgap=6")
        pick(page, "策略", policy)
        number = page.locator('input[aria-label="随机种子"]').first
        number.fill(str(seed))
        number.press("Tab")

        page.get_by_role("button", name="① 开始试玩", exact=True).click()
        scan = wait_body(page, ("本局结论", "运行出错"))
        page.screenshot(path=str(OUT / f"{name}-1-scan.png"), full_page=True)

        open_tab(page, "复现包")
        page.get_by_role("button", name="② 一键复现", exact=True).click()
        replay = wait_body(page, ("回放校验：", "运行出错"))
        page.screenshot(path=str(OUT / f"{name}-2-replay.png"), full_page=True)

        open_tab(page, "修复补丁")
        page.get_by_role("button", name="③ 自动修复", exact=True).click()
        fix = wait_body(page, ("修复结果：", "运行出错"), timeout_s=360)
        page.screenshot(path=str(OUT / f"{name}-3-fix.png"), full_page=True)
        return {"scan": scan, "replay": replay, "fix": fix}
    finally:
        page.close()


def static_evidence_needles() -> list[str]:
    corpus = json.loads((ROOT / "evidence" / "corpus.json").read_text(encoding="utf-8"))
    game = corpus["games"][0]
    return [
        "封存证据",
        "不是本次实时运行",
        str(game["title"]),
        f"{len(game['defects'])} 条缺陷",
        f"{len(game['replays'])} 条复现记录",
        "作者确认：not_requested",
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=False)
    sequences: list[dict[str, str]] = []
    all_errors: list[dict[str, str]] = []
    try:
        from playtest.env import resolve_browser

        with sync_playwright() as playwright:
            launch_args: dict[str, Any] = {
                "headless": True,
                "args": ["--disable-gpu", "--mute-audio", "--no-sandbox", "--no-proxy-server"],
            }
            executable = resolve_browser()
            if executable:
                launch_args["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_args)
            try:
                with gradio_server("normal", {}) as url:
                    landing = open_page(browser, url, all_errors)
                    landing_text = body_text(landing)
                    landing.screenshot(path=str(OUT / "00-landing.png"), full_page=True)
                    check(
                        "首屏三按钮可见",
                        all(landing.get_by_role("button", name=name, exact=True).count() == 1
                            for name in ("① 开始试玩", "② 一键复现", "③ 自动修复")),
                    )
                    missing = [n for n in static_evidence_needles() if n not in landing_text]
                    check("首屏展示真实封存台账且标明非实时", not missing, "缺：" + repr(missing))
                    check("禁用模型时横幅明示未配置", "推理端点未配置" in landing_text)
                    landing.close()

                    for name, policy, seed in (
                        ("A-curiosity", "curiosity", 20261105),
                        ("B-clue", "clue", 20261106),
                    ):
                        data = run_sequence(browser, url, name, policy, seed, all_errors)
                        sequences.append(data)
                        issues = sequence_issues(name, data)
                        check(f"{name} 试玩→复现→人工补丁修复闭环", not issues, ",".join(issues))

                if sequences:
                    baseline = sequence_issues("A-curiosity", sequences[0])
                    check("仪器正向对照：真实首条序列无判红项", baseline == [], repr(baseline))
                    wrong_selector = dict(sequences[0], replay="错误选择器读到的不相关面板")
                    check(
                        "负控：错误选择器必须只判红 replay",
                        sequence_issues("A-curiosity", wrong_selector) == ["A-curiosity replay"],
                    )
                    empty_result = dict(sequences[0], fix="")
                    check(
                        "负控：空修复结果必须只判红 fix",
                        sequence_issues("A-curiosity", empty_result) == ["A-curiosity fix"],
                    )
                else:
                    check("仪器负控有真实序列作为正向对照", False)
                check(
                    "负控：console 异常必须判红且空列表不红",
                    console_issues([{"type": "console", "text": "planted"}]) == ["console/pageerror"]
                    and console_issues([]) == [],
                )

                with gradio_server("no-chromium", {"PLAYTEST_BROWSER": "disabled"}) as url:
                    page = open_page(browser, url, all_errors)
                    landing_text = body_text(page)
                    missing = [n for n in static_evidence_needles() if n not in landing_text]
                    check("Chromium 故障时封存证据仍在首屏", not missing, "缺：" + repr(missing))
                    page.get_by_role("button", name="① 开始试玩", exact=True).click()
                    failed = wait_body(page, ("运行出错", "本局结论"), timeout_s=120)
                    page.screenshot(path=str(OUT / "90-no-chromium.png"), full_page=True)
                    check(
                        "Chromium 故障明确报运行层且不冒充实时成功",
                        "运行出错" in failed
                        and "Chromium" in failed
                        and "PLAYTEST_BROWSER=disabled" in failed
                        and "本局结论" not in failed,
                        failed[failed.find("运行出错"):failed.find("运行出错") + 240],
                    )
                    page.close()
            finally:
                browser.close()

        (OUT / "console.json").write_text(
            json.dumps(all_errors, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        check("真实页面零 console/pageerror", not console_issues(all_errors), repr(all_errors[:3]))
    except Exception as exc:
        check("UI 验收驱动无异常", False, f"{type(exc).__name__}: {exc}"[:800])

    (OUT / "sequences.json").write_text(
        json.dumps(sequences, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    failed = [row for row in RESULTS if not row["ok"]]
    summary = {
        "ok": not failed,
        "checks": RESULTS,
        "artifacts": str(OUT),
        "visual_scope": ["桌面首屏", "两条扫描/回放/修复态", "Chromium 故障态"],
        "not_tested": ["窄屏", "打印面", "公开创空间"],
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"evidence={OUT}")
    print(f"release ui: {'PASS' if not failed else 'FAIL'} {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
