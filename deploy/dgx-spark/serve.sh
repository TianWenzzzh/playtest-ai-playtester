#!/usr/bin/env bash
# 在 NVIDIA DGX Spark (aarch64 / CUDA 13 / sm_121) 上起 PlayTest 的推理后端。
#
# 🔴 三条不能照搬 x86 的地方，都写在参数上：
#   1. 镜像必须是 arm64 且带 cu130 标签（latest 不含 CUDA 13，Blackwell 上跑不起来）
#   2. --mem-fraction-static 降到 0.75：GB10 是统一内存，KV cache 挤的是系统内存，
#      按默认值配会 OOM（SGLang DGX Spark playbook 的 troubleshooting 口径）
#   3. 需要结构化 JSON 输出 ⇒ 必须 cu130（带 xGrammar）
#
# 用法：./serve.sh [A|B|C|D]     默认 A（MoE 小激活档，64GB 节点也能跑）
set -euo pipefail

TIER="${1:-A}"
PORT="${PORT:-8000}"

case "$TIER" in
  A) MODEL="Qwen/Qwen3-30B-A3B-Instruct-2507"; QUANT="nvfp4"; MEMFrac="0.75" ;;
  B) MODEL="Qwen/Qwen3-14B";                   QUANT="fp8";  MEMFrac="0.75" ;;
  C) MODEL="Qwen/Qwen3-32B";                   QUANT="nvfp4"; MEMFrac="0.70" ;;
  D) MODEL="Qwen/Qwen3-8B";                    QUANT="fp8";  MEMFrac="0.75" ;;
  *) echo "未知档位 $TIER，可选 A B C D" >&2; exit 2 ;;
esac

# 统一内存不是显存，nvidia-smi 那一栏是 Not Supported —— 用 /proc/meminfo 看水位。
AVAIL_KB=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
echo "档位 $TIER · 模型 $MODEL · 量化 $QUANT · mem-fraction $MEMFrac"
echo "当前 MemAvailable: $((AVAIL_KB / 1024 / 1024)) GB"
if [ "$AVAIL_KB" -lt $((40 * 1024 * 1024)) ]; then
  echo "⚠ 可用内存不足 40GB。官方口径：先回收页缓存再起服务"
  echo "  sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"
fi

docker run -d --rm --name "playtest-llm-$TIER" \
  --gpus all --ipc host --network host \
  --memory 96g --memory-swap 96g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${HOME}/.cache/modelscope:/root/.cache/modelscope" \
  lmsysorg/sglang:latest-cu130 \
  python3 -m sglang.launch_server \
    --model "$MODEL" \
    --quantization "$QUANT" \
    --mem-fraction-static "$MEMFrac" \
    --host 0.0.0.0 --port "$PORT"

echo "等待就绪…"
for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "✅ http://127.0.0.1:$PORT/v1"
    echo "   export PLAYTEST_BASE_URL=http://127.0.0.1:$PORT/v1"
    echo "   export PLAYTEST_API_KEY=dummy"
    echo "   export PLAYTEST_MODEL=$MODEL"
    exit 0
  fi
  sleep 2
done

echo "❌ 90 次探测内没起来，看日志：docker logs playtest-llm-$TIER" >&2
exit 1
