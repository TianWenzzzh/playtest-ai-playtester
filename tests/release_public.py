"""公开竖切验收：匿名 clone 与公开创空间冷启动。

四条平台地址全部来自环境变量；缺配置或地址是本机/带凭据的，一律判红并退出码 1，
绝不因为"公开的东西还没上线"就跳过检查打印 PASS。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "release_public" / (
    datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
)
RESULTS: list[dict[str, Any]] = []

REQUIRED_CONFIG = (
    "PLAYTEST_PUBLIC_REPO_URL",
    "PLAYTEST_PUBLIC_URL",
    "PLAYTEST_PUBLIC_MODEL_DISABLED_URL",
    "PLAYTEST_PUBLIC_BROWSER_DISABLED_URL",
)
BUTTONS = ("① 开始试玩", "② 一键复现", "③ 自动修复")


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"label": label, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail[:300]}]" if detail else ""), flush=True)


def sanitized_env() -> dict[str, str]:
    """匿名通道：只剥掉凭据变量，代理留着——公网访问在本机要靠它。"""
    env = os.environ.copy()
    for key in ("PLAYTEST_API_KEY", "MODELSCOPE_API_TOKEN"):
        env.pop(key, None)
    env.update({"GIT_TERMINAL_PROMPT": "0", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def run_command(argv: list[str], cwd: pathlib.Path, timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=sanitized_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_s,
    )


def output_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout + "\n" + proc.stderr).strip().replace("\n", " | ")[-500:]


def anonymous_public_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme in ("http", "https") and bool(host)
            and not parts.username and not parts.password
            and host != "localhost" and not host.startswith("127.")
            and host not in {"::1", "[::1]"})


def redacted(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return parts._replace(netloc=f"{parts.hostname or ''}:{parts.port}" if parts.port
                          else (parts.hostname or ""), fragment="", query="").geturl()


def static_evidence_needles(public_root: pathlib.Path) -> list[str]:
    """needle 形状与 tests/release_ui.py:188 那条同尺，但取的是公开 clone 的台账。"""
    corpus = json.loads((public_root / "evidence" / "corpus.json").read_text(encoding="utf-8"))
    needles = ["封存证据", "不是本次实时运行"]
    for game in corpus["games"]:
        needles.extend((
            str(game["title"]),
            f"{len(game['defects'])} 条缺陷",
            f"{len(game['replays'])} 条复现记录",
            "作者确认：not_requested",
        ))
    return needles


def body_text(page: Any) -> str:
    return page.locator("body").inner_text()


def wait_body(page: Any, needles: tuple[str, ...], timeout_s: int = 300) -> str:
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = body_text(page)
        if any(needle in text for needle in needles):
            return text
        page.wait_for_timeout(250)
    raise AssertionError(f"等待公开页结果超时：{needles}；文本尾：{text[-800:]}")


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


def open_cold_page(browser: Any, url: str, label: str) -> tuple[Any, list[str], list[str]]:
    """独立 context 的全新冷启动；顺带录 pageerror 与推理请求，用于'首屏不依赖模型'那条。"""
    page_errors: list[str] = []
    inference_requests: list[str] = []

    def on_request(request: Any) -> None:
        lowered = request.url.lower()
        if "chat/completions" in lowered or "api-inference" in lowered:
            inference_requests.append(request.url)

    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    page = context.new_page()
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", on_request)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.get_by_role("button", name="① 开始试玩", exact=True).wait_for(state="visible", timeout=90_000)
        page.wait_for_timeout(2_000)
        page.screenshot(path=str(OUT / f"{label}-landing.png"), full_page=True)
        check(f"{label} HTTP 冷启动成功", response is not None and response.ok,
              f"status={getattr(response, 'status', None)}")
        check(f"{label} 首屏不发起推理请求", not inference_requests, str(inference_requests[:2]))
    except Exception:
        context.close()
        raise
    return page, page_errors, inference_requests


def verify_repo_clone(repo_url: str) -> pathlib.Path | None:
    check("公开仓库地址匿名且无内嵌凭据", anonymous_public_url(repo_url), redacted(repo_url))
    if not anonymous_public_url(repo_url):
        return None

    temp_parent = pathlib.Path(tempfile.mkdtemp(prefix="playtest-public-clone-"))
    clone = temp_parent / "repo"
    proc = run_command(["git", "clone", "--depth", "1", repo_url, str(clone)], ROOT, 180)
    check("匿名 clean clone 成功", proc.returncode == 0, output_excerpt(proc))
    if proc.returncode != 0:
        shutil.rmtree(temp_parent, ignore_errors=True)
        return None

    origin = run_command(["git", "-C", str(clone), "remote", "get-url", "origin"], clone, 10)
    check("clone 的 origin 等于配置的公开仓库",
          origin.returncode == 0 and origin.stdout.strip() == repo_url,
          origin.stdout.strip() or output_excerpt(origin))
    check("公开 clone 不携带 out/ 或 latest.json",
          not (clone / "out").exists() and not (clone / "evidence" / "latest.json").exists(),
          str(clone))
    check("公开 clone 含应用、夹具与封存台账",
          (clone / "app.py").is_file() and (clone / "games").is_dir()
          and (clone / "evidence" / "corpus.json").is_file(), str(clone))

    scan = run_command([
        sys.executable, "-m", "playtest.cli", "scan", "games/clicker-quest",
        "--seeds", "20261105", "--policy", "curiosity",
    ], clone, 180)
    packs = sorted((clone / "out" / "scan").glob("curiosity-20261105-*.json"))
    check("公开 clone 从零 scan 新产出恰好一个复现包", scan.returncode == 0 and len(packs) == 1,
          f"rc={scan.returncode} packs={len(packs)}")
    if len(packs) != 1:
        print(output_excerpt(scan))
        return clone

    replay = run_command([sys.executable, "-m", "playtest.cli", "replay", str(packs[0])], clone, 180)
    try:
        verification = json.loads(replay.stdout)
    except json.JSONDecodeError:
        verification = {}
    check("公开 clone 的新包独立逐帧回放成功",
          replay.returncode == 0 and verification.get("replayed") is True
          and verification.get("hashes_match") is True,
          f"rc={replay.returncode} steps={verification.get('steps_matched')}/{verification.get('steps_total')}")
    return clone


def verify_public_actions(browser: Any, url: str, needles: list[str]) -> list[str]:
    page, page_errors, inferences = open_cold_page(browser, url, "public-normal-actions")
    try:
        text = body_text(page)
        missing = [needle for needle in needles if needle not in text]
        check("公开首屏完整展示封存台账", not missing, "缺：" + repr(missing))
        check("公开三按钮各恰好可见一次",
              all(page.get_by_role("button", name=name, exact=True).count() == 1 for name in BUTTONS))

        pick(page, "夹具游戏", "blocky-jump")
        pick(page, "缺陷臂", "有缺陷臂 maxgap=6")
        pick(page, "策略", "clue")
        seed = page.locator('input[aria-label="随机种子"]').first
        seed.fill("20261106")
        seed.press("Tab")

        page.get_by_role("button", name="① 开始试玩", exact=True).click()
        scan_text = wait_body(page, ("本局结论", "运行出错"))
        page.screenshot(path=str(OUT / "public-normal-scan.png"), full_page=True)
        check("公开试玩返回真实缺陷而非静默", "本局结论" in scan_text and "运行出错" not in scan_text,
              scan_text[scan_text.find("本局结论"):][:260] if "本局结论" in scan_text else scan_text[-260:])

        open_tab(page, "复现包")
        page.get_by_role("button", name="② 一键复现", exact=True).click()
        replay_text = wait_body(page, ("回放校验：", "运行出错"))
        page.screenshot(path=str(OUT / "public-normal-replay.png"), full_page=True)
        check("公开一键复现逐帧通过",
              "回放校验：通过" in replay_text and "bit_reproducible" in replay_text, replay_text[:260])

        open_tab(page, "修复补丁")
        page.get_by_role("button", name="③ 自动修复", exact=True).click()
        fix_text = wait_body(page, ("修复结果：", "运行出错"), timeout_s=420)
        page.screenshot(path=str(OUT / "public-normal-fix.png"), full_page=True)
        check("公开修复显示红转绿并标明补丁来源",
              "修复结果：红转绿" in fix_text and ("人工提供" in fix_text or "模型生成" in fix_text),
              fix_text[:300])
        check("公开三按钮动作序列无页面级异常", not page_errors, str(page_errors[:2]))
        return inferences
    finally:
        page.close()


def verify_model_degraded(browser: Any, url: str, needles: list[str]) -> None:
    page, _, requests = open_cold_page(browser, url, "public-model-disabled")
    try:
        text = body_text(page)
        missing = [needle for needle in needles if needle not in text]
        check("模型禁用实例仍展示封存台账", not missing, "缺：" + repr(missing))
        check("模型禁用实例明确说明端点未配置", "推理端点未配置" in text, text[:260])
        check("模型禁用实例首屏零推理请求", not requests, str(requests[:2]))
    finally:
        page.close()


def verify_browser_degraded(browser: Any, url: str, needles: list[str]) -> None:
    page, _, _ = open_cold_page(browser, url, "public-browser-disabled")
    try:
        landing = body_text(page)
        missing = [needle for needle in needles if needle not in landing]
        check("Chromium 禁用实例仍展示封存台账", not missing, "缺：" + repr(missing))
        page.get_by_role("button", name="① 开始试玩", exact=True).click()
        failed = wait_body(page, ("运行出错",), timeout_s=180)
        page.screenshot(path=str(OUT / "public-browser-disabled-scan.png"), full_page=True)
        check("Chromium 禁用实例明确报运行层且不冒充实时成功",
              "运行出错" in failed and "PLAYTEST_BROWSER=disabled" in failed and "本局结论" not in failed,
              failed[failed.find("运行出错"):][:240])
    finally:
        page.close()


def finish() -> None:
    failed = [row for row in RESULTS if not row["ok"]]
    summary = {
        "ok": not failed,
        "checks": RESULTS,
        "artifacts": str(OUT),
        "config": {name: redacted(os.environ.get(name, "")) for name in REQUIRED_CONFIG},
        "not_tested": ["跨机真 GPU 复现", "作者确认", "第三方独立重放"],
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"evidence={OUT}")
    print(f"release public: {'PASS' if not failed else 'FAIL'} {len(RESULTS) - len(failed)}/{len(RESULTS)}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=False)
    missing = [name for name in REQUIRED_CONFIG if not os.environ.get(name)]
    if missing:
        check("公开发布配置完整", False, "缺少：" + ",".join(missing))
        (OUT / "summary.json").write_text(
            json.dumps({"ok": False, "checks": RESULTS, "missing_config": missing,
                        "not_tested": ["公开仓库", "公开创空间"],
                        "note": "公开配置缺失：未测，不能跳过后打印 PASS"},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"release public: FAIL {len(RESULTS)}/{len(RESULTS)}（公开配置缺失）")
        return 1

    invalid = [name for name in REQUIRED_CONFIG if not anonymous_public_url(os.environ[name])]
    for name in invalid:
        check(f"{name} 必须是匿名公开 HTTP(S) 地址", False, "非 http(s)、本机地址或含内嵌凭据")
    if invalid:
        finish()
        return 1

    clone = verify_repo_clone(os.environ["PLAYTEST_PUBLIC_REPO_URL"])
    try:
        if clone is None:
            finish()
            return 1
        needles = static_evidence_needles(clone)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, args=["--disable-gpu", "--no-sandbox", "--no-proxy-server"],
                )
                try:
                    normal = os.environ["PLAYTEST_PUBLIC_URL"]
                    for visit in (1, 2):
                        page, _, _ = open_cold_page(browser, normal, f"public-normal-{visit}")
                        text = body_text(page)
                        missing = [needle for needle in needles if needle not in text]
                        check(f"公开 URL 第 {visit} 次冷启动首屏台账完整", not missing, "缺：" + repr(missing))
                        page.close()
                    inferences = verify_public_actions(browser, normal, needles)
                    check("公开动作序列未静默调用模型", not inferences, str(inferences[:2]))
                    verify_model_degraded(browser, os.environ["PLAYTEST_PUBLIC_MODEL_DISABLED_URL"], needles)
                    verify_browser_degraded(browser, os.environ["PLAYTEST_PUBLIC_BROWSER_DISABLED_URL"], needles)
                finally:
                    browser.close()
        except Exception as exc:
            check("公开验收驱动无异常", False, f"{type(exc).__name__}: {exc}"[:600])
    finally:
        if clone is not None:
            shutil.rmtree(clone.parent, ignore_errors=True)

    finish()
    return 1 if [row for row in RESULTS if not row["ok"]] else 0


if __name__ == "__main__":
    raise SystemExit(main())
