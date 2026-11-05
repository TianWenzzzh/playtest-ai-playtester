# 在单台 NVIDIA DGX Spark 上部署 PlayTest 的推理后端

> 赛题硬要求原文：「AI 原生游戏工具与 Agent 方向：**核心 AI 推理及相关服务须提供单台 DGX Spark 的部署方案**。」
> 这份文档就是那个方案。
>
> 🔴 **诚实状态标注**：截至本文撰写，本仓库尚未获得 Spark 节点，
> 因此下面的命令是**按 NVIDIA 官方 DGX Spark playbook 组装的、尚未在本机跑通的配置**。
> 拿到节点后的第一件事就是跑 `bench.py` 把 `models.tsv` 里的空读数填满，
> 并在手记里区分"实测"与"推算"。在此之前，任何地方都不许把它写成实测结果。

---

## 1. 为什么 Spark 上不能照搬 x86 配置

| 事实 | 来源 | 对部署的实际影响 |
|---|---|---|
| CPU 是 **20 核 Arm**（10×Cortex-X925 + 10×Cortex-A725），即 **aarch64** | NVIDIA DGX Spark 规格页 | 镜像必须 arm64；上游 x86 预编译 wheel 装不上 |
| GPU 计算能力 **sm_121 / compute_cap 12.1，CUDA 13.0** | SGLang DGX Spark playbook 注释 | 从源码编译要 `CMAKE_CUDA_ARCHITECTURES=121a-real` |
| **128 GB LPDDR5x 统一内存，256-bit，273 GB/s**（2026-10 起另有 64 GB 版） | 规格页 / LeadTek | 🔴 不是"显存"，是跟系统共用的内存；且**节点可能是 64GB** ⇒ 模型档位必须留 64GB 兜底 |
| 最高 **1 PFLOP FP4**（低密度理论值） | 规格页 | 量化格式选 NVFP4 / FP8 |
| OS 是 **DGX OS（Ubuntu 系）**，**不是 JetPack** | 规格页零次出现 JetPack/L4T | 别照 Jetson 教程配 |
| `nvidia-smi` 的显存栏读不到（Not Supported） | SGLang playbook troubleshooting | 内存监控要看 `/proc/meminfo`，不能靠 nvidia-smi |
| 内存压力大时官方要求 `sync; echo 3 > /proc/sys/vm/drop_caches` | 同上 | 写进 `serve.sh` 的运维说明 |
| SGLang 在 Spark 上建议 `--mem-fraction-static ≈ 0.75` | 同上 | 照默认值配会 OOM |

---

## 2. 模型选型：为什么是 MoE 小激活，不是稠密大模型

试玩决策的负载形状是：**每局调用几百次、每次只出几十个 token、要求首 token 时延稳定**。
这种负载下 prefill 便宜、decode 完全被内存带宽绑死。

273 GB/s 这个带宽意味着稠密大模型解码会很慢（社区有 Llama-3.3-70B 约 3 tok/s 的二手读数，**未经我们实测**）。
所以默认档选**激活参数少的 MoE** —— 这也正是 NVIDIA 与华硕官方教程自己的默认选型。

| 档 | 模型 | 量化 | 用途 | 64 GB 兜底 |
|---|---|---|---|---|
| **A 默认** | Qwen3-30B-A3B 一类 MoE | NVFP4 | 试玩决策 + 代码修复 | ✅ |
| B | Qwen3-14B | FP8 | 决策（修复走云端 API） | ✅ |
| C | Qwen3-32B | NVFP4 | 修复质量优先 | 勉强 |
| D | Qwen3-8B | FP8 | 快速迭代 / CI | ✅ |

SGLang 官方 playbook 的「DGX Spark 已验证」表里包含 Qwen3-8B/14B FP8·NVFP4、Qwen3-32B NVFP4、
GPT-OSS-20B/120B MXFP4、Llama-3.1-8B、Llama-3.3-70B NVFP4、Phi-4 系列、Nemotron-3-Nano-Omni-30B-A3B。

🔴 **动作决策必须是结构化 JSON** ⇒ 需要 xGrammar，即 `latest-cu130` 镜像（不是 `latest`）。

---

## 3. 起服务

```bash
# arm64 镜像 + CUDA 13 标签；--mem-fraction-static 降到 0.75
docker run -d --name playtest-llm \
  --gpus all --ipc host --network host \
  --memory 96g --memory-swap 96g \
  -v ~/.cache/modelscope:/root/.cache/modelscope \
  lmsysorg/sglang:latest-cu130 \
  python3 -m sglang.launch_server \
    --model Qwen/Qwen3-30B-A3B-Instruct-2507 \
    --quantization nvfp4 \
    --mem-fraction-static 0.75 \
    --host 0.0.0.0 --port 8000
```

`serve.sh` 是上面这条的可执行版本，带档位参数。

接上 PlayTest：

```bash
export PLAYTEST_BASE_URL=http://127.0.0.1:8000/v1
export PLAYTEST_API_KEY=dummy            # SGLang 默认不鉴权，客户端要求非空
export PLAYTEST_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
python -m benchmark.run
```

---

## 4. 与创空间的协同（这条链路为什么长这样）

🔴 **关键事实待确认**：官方节点按教程是 **SSH 远程接入 + 端口转发**（`ssh -L`），
**很可能没有公网入站**。而魔搭创空间跑在它自己的 **x86** 容器里（免费档 2 vCPU / 16 GB）。
⇒ **创空间实时回连 Spark 大概率做不到**，Spark 的 arm64 镜像也搬不进创空间。

所以按"实时/离线"切开，而不是硬连：

```
创空间（x86, Gradio, 0.0.0.0:7860）
  ├─ 确定性回放：复现包是静态 JSON，重放只要 Chromium ⇒ CPU 够用
  ├─ 证据浏览器 + 决策流可视化
  ├─ 在线试玩：调 魔搭 API Inference（外部 API 明确被支持）
  └─ 夹具游戏静态托管（/gradio_api/file=，不用 file:// 以免被 https 页面拦）

DGX Spark（arm64, GB10）
  ├─ SGLang 常驻推理（档位 A）
  ├─ 批量扫描：几十个游戏 × 多种子，7×24 跑（正是官方给的用法）
  ├─ 修复 Agent：读代码 + 打补丁 + 回放验证
  └─ bench.py 出实测数字

离线交换：扫描结果与复现包 JSON 提交进仓库；实测数字写进手记与 README
```

🔴 这不是退让，这就是"云端与本地协同"在本赛题下的真实形状。
手记里要写清：**为什么不能实时、切开的代价是什么、哪些读数来自 Spark、哪些来自云端 API。**

**待向主办方确认的一条**（问错了会白排一周）：节点有无公网入站 / 能否建反向隧道。

---

## 5. 要填的实测读数

跑 `python deploy/dgx-spark/bench.py --games 20 --seeds 4` 后填 `models.tsv`：

| 列 | 含义 |
|---|---|
| `tok_per_sec` | 解码吞吐 |
| `prefill_tok_per_sec` | prefill 吞吐 |
| `ttft_p50_ms` / `ttft_p95_ms` | 首 token 时延分位（试玩是交互式，p95 比均值重要） |
| `decisions_per_min` | 每分钟能推进多少步决策（= 批量扫描吞吐的真实单位） |
| `mem_used_gb` | 走 `/proc/meminfo`，**不是** nvidia-smi |
| `episodes_per_hour` | 端到端：一小时能扫多少个游戏 |

参考口径（华硕 GX10 教程给出的实测数）：35B-A3B 档约 **110 tok/s 生成、1900 tok/s prefill**，
与文生图同跑时 60–80 °C 连续运行。**这是别人机器的读数，不能当我们的结果。**

---

## 6. 拿不到节点时怎么办

官方明说：「未获得算力支持不影响参赛，可使用其他开发资源完成作品。」

1. 推理全部走魔搭 API Inference —— 策略层同一套代码，只换 `PLAYTEST_BASE_URL`；
2. 本目录仍作为**方案 + 等价验证**交付：在本地 x86 上跑同一套 SGLang 配置，
   把 `sm_121` / 统一内存 / arm64 wheel 三处差异逐条列出并说明推算依据；
3. 🔴 README 与手记里必须写明"这是等价验证，不是 Spark 实测"。
