"""M8 模型客户端 HTTP 行为：默认不发魔搭/Qwen 不支持的 response_format，且失败时留住错误体。

这条是被一次真实事故教出来的：主臂创空间填了 token、横幅翻绿，但匿名实跑 do_scan(llm)
决策表 0 条有效命中、全是 HTTP 400/429 回退。根因是 complete() 默认 force_json=True
注入 response_format:{type:json_object}，而魔搭 API Inference 上的 Qwen 不支持该参数 ⇒ 400；
旧 except 又把 HTTPError 的响应体吞掉，只留 "HTTP Error 400"，诊断不到真因。

用一个本机假端点模拟魔搭：带 response_format 就回 400＋错误体，不带就回 200＋合法内容。
不碰任何真实凭据，不起浏览器。跑法：PYTHONUTF8=1 ../.venv/Scripts/python.exe tests/m8_llm_client.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 假端点在 127.0.0.1，必须绕过环境代理，否则 urlopen 会把本机请求送进代理而失败。
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ.get("no_proxy", "") + ",127.0.0.1,localhost"

from playtest.llm import LLMClient, LLMConfig

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(label)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    state: dict


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音，别污染判定输出
        pass

    def do_POST(self):
        st = self.server.state
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        st["bodies"].append(body)
        if st.get("reject_rf") and "response_format" in body:
            payload = json.dumps({"error": {"message": "response_format json_object is not supported by this model"}}).encode("utf-8")
            self.send_response(400)
        else:
            content = json.dumps({"action": "jump", "reason": "ok"}, ensure_ascii=False)
            payload = json.dumps({"choices": [{"message": {"content": content}}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 7}}).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.state = {"reject_rf": True, "bodies": []}
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}/v1"
    msgs = [{"role": "user", "content": "hi"}]
    try:
        print("== 1. 默认 complete() 不许发 response_format（魔搭会 400）==")
        srv.state["bodies"].clear()
        c1 = LLMClient(LLMConfig(base_url=base, api_key="test-key", model="test-model"))
        out1 = c1.complete(msgs)
        body1 = srv.state["bodies"][-1] if srv.state["bodies"] else {}
        check("默认调用拿到模型内容（未被 400 吞掉）", isinstance(out1, str) and "jump" in out1,
              f"out={out1!r} last_error={c1.stat.last_error!r}")
        check("默认请求体里没有 response_format", "response_format" not in body1,
              json.dumps(body1, ensure_ascii=False)[:160])

        print("== 2. 显式 force_json=True 仍发 response_format（保留 SGLang/DGX 能力）==")
        srv.state["reject_rf"] = False
        srv.state["bodies"].clear()
        c2 = LLMClient(LLMConfig(base_url=base, api_key="test-key", model="test-model"))
        c2.complete(msgs, force_json=True)
        body2 = srv.state["bodies"][-1] if srv.state["bodies"] else {}
        check("force_json=True 时请求体带 response_format",
              body2.get("response_format") == {"type": "json_object"},
              json.dumps(body2.get("response_format"), ensure_ascii=False))

        print("== 3. 端点回 400 时，响应体里的真因必须进 last_error（不许只留 HTTP Error 400）==")
        srv.state["reject_rf"] = True
        srv.state["bodies"].clear()
        c3 = LLMClient(LLMConfig(base_url=base, api_key="test-key", model="test-model"))
        out3 = c3.complete(msgs, force_json=True)
        check("400 时 complete 返回 None", out3 is None, f"out={out3!r}")
        check("last_error 留住响应体真因", "not supported by this model" in c3.stat.last_error,
              c3.stat.last_error)
        check("失败计数计满", c3.stat.failures == 1 and c3.stat.calls == 1,
              f"calls={c3.stat.calls} failures={c3.stat.failures}")
    finally:
        srv.shutdown()
        srv.server_close()

    print()
    if fails:
        print("M8 模型客户端未通过:", len(fails), "项 ->", "; ".join(fails))
        return 1
    print("M8 模型客户端全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())