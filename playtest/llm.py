"""模型客户端：只认 OpenAI 兼容接口，配置全走环境变量。

写这个模块时机器上没有任何可用推理端点，所以这里刻意不做的事：
  · 不硬编码 key、不内置任何默认 token；
  · 不在没有端点时假装调用成功；
  · 不把"用了大模型"当成主张 —— 拿不到读数就标 unavailable，让基准表如实缺一列。

后端优先级：
  1) PLAYTEST_BASE_URL / PLAYTEST_API_KEY  —— 通用 OpenAI 兼容（DGX Spark 上的 SGLang 走这条）
  2) MODELSCOPE_API_TOKEN                  —— 魔搭 API Inference（创空间那边唯一能实时调的通道）
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

MODELSCOPE_BASE = "https://api-inference.modelscope.cn/v1"


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 400
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        key = os.environ.get("PLAYTEST_API_KEY") or os.environ.get("MODELSCOPE_API_TOKEN", "")
        base = os.environ.get("PLAYTEST_BASE_URL", "")
        if not base and key and not os.environ.get("PLAYTEST_API_KEY"):
            base = MODELSCOPE_BASE
        # 默认须是魔搭 API Inference 现役 ID；Spark 本地 serving 另设 PLAYTEST_MODEL，不用此默认。
        return cls(
            base_url=base.rstrip("/"),
            api_key=key,
            model=os.environ.get("PLAYTEST_MODEL", "Qwen/Qwen3.5-35B-A3B"),
            temperature=float(os.environ.get("PLAYTEST_TEMP", "0.2")),
            max_tokens=int(os.environ.get("PLAYTEST_MAX_TOKENS", "400")),
        )

    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def reason_unavailable(self) -> str:
        miss = [n for n, v in (("base_url", self.base_url), ("api_key", self.api_key), ("model", self.model)) if not v]
        return "缺少配置: " + ",".join(miss) if miss else "ok"


@dataclass
class CallStat:
    calls: int = 0
    failures: int = 0
    total_ms: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "avg_ms": round(self.total_ms / self.calls) if self.calls else 0,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "last_error": self.last_error,
        }


class LLMClient:
    def __init__(self, config: LLMConfig | None = None) -> None:
        self.cfg = config or LLMConfig.from_env()
        self.stat = CallStat()

    def complete(self, messages: list[dict[str, str]], force_json: bool = False) -> str | None:
        if not self.cfg.available():
            self.stat.last_error = self.cfg.reason_unavailable()
            return None

        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            self.cfg.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # 创空间文档明说后端不得占用 Authorization / X-modelscope-* / X-studio-*，
                # 这里是我们自己出站的请求头，不受该限制；但仍刻意只用标准 Authorization。
                "Authorization": "Bearer " + self.cfg.api_key,
            },
            method="POST",
        )
        t0 = time.time()
        self.stat.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choice = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            self.stat.total_ms += int((time.time() - t0) * 1000)
            self.stat.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.stat.total_completion_tokens += int(usage.get("completion_tokens") or 0)
            return choice
        except urllib.error.HTTPError as exc:
            self.stat.failures += 1
            # 状态行（HTTP Error 400）会盖住真因；错误体里才有端点拒绝的具体理由。
            detail = exc.read().decode("utf-8", "replace")
            self.stat.last_error = f"HTTPError {exc.code}: {detail}"[:200]
            return None
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            self.stat.failures += 1
            self.stat.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """模型偶尔会把 JSON 包在 ```json 里，或前后带解释。

    容错只做"取第一个花括号块"这一层，不做猜测性修复 —— 猜出来的 action 会污染轨迹。
    """
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
