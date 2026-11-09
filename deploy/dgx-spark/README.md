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

## 5.1 提交材料里那张表（一页、全是数字、每格都标明来源）

评审要的是"单台 DGX Spark 部署方案"这一格。🔴 **这表的规矩是：每一格必须能指出它来自哪条命令**，
拿不到读数就写「未测」并附取数命令，**绝不用形容词占位**。
本机列是 x86 + 软件渲染（SwiftShader）上的真实读数，只能当**下界与形状**，不能当 Spark 的结果。

| 项 | 本机实测（11-06） | Spark 目标格 | 取数命令 / 未测原因 |
|---|---|---|---|
| 部署形态 | 单进程 Gradio + 共享单浏览器（信号量 1） | 四容器 compose | `deploy/dgx-spark/serve.sh` 已有；节点未开通 ⇒ **未测** |
| 基座模型 | 11-06 本地那轮无端点 ⇒ LLM 臂等价 `curiosity`；11-07 创空间已配 API Inference 端点并真实调用（llm 臂有命中） | 主循环 MoE 小激活 + 重推理档 | 见 §2 选型论证 |
| 出词速度 | — | `___ tok/s` | 🔴 **未测**：`bench.py --rounds`（需端点）；官方教程口径 35B-A3B ≈110 tok/s 是**别人机器**的数 |
| 端到端耗时（单局试玩） | **中位 3.55 s / 均值 4.26 s / p95 11.2 s / 最大 14.3 s**（144 局） | `___` | `out/benchmark.json` 的 `wall_ms` 逐局求；144 局局内计时合计 613 s |
| 整轮基准墙钟 | **12 分 09 秒**（11-06 上午那次独立跑批：09:46:54 → 09:58:58，144 局 + 54 次复现校验；起止取自开发工作区日志 mtime，**该日志不随仓库分发**） | `___` | 仓内复算：跑 `PYTHONUTF8=1 python tools/certify.py full`，读 `evidence/latest.json` 里 `benchmark.run` 那一步的 `duration_ms`。🔴 那是**另一次跑批**的数，与左格不可互借（认证器逐步记 `started_at`/`duration_ms`，所以这个量现在仓内可复算） |
| 内存峰值（空载 / 单局 / 并发 2 路） | 未测 | `___ / ___ / ___ GB` | `bench.py` 的 `mem_snapshot()` 走 `/proc/meminfo`（**不是** nvidia-smi，GB10 统一内存没有独立显存） |
| 并发能力 | **1**（设计值，不是测值：2 vCPU 档同时开两个 Chromium 会互相拖到超时） | `___` | 需要真节点才能测出实际上限 |
| 稳定性 | 🔴 **144 局 error = 0**；各套件断言数与逐条 RC **以运行输出为准，这里不抄**（现读 `evidence/latest.json` 的 `steps[].rc` 与 `evidence/runs/<run>/*.log`，由 `tools/certify.py` 生成） | 连续 10 次生成 / 24 h 挂机 | 24 h 挂机 **未测** |
| 启动时间（冷 / 热） | 未测 | `___ / ___ s` | 冷启动含模型加载 ⇒ 需端点 |
| 资源回收 | 已实现：浏览器实例进程内共享、`shutdown_browser()` + `atexit`、崩溃后 `is_connected()` 判死重建 | 模型按需加载/卸载 + 日志滚动 | 🔴 模型侧卸载**未实现**（我们没有常驻模型） |
| **离线能力** | ✅ **已实测**：真实第三方作品取成 `.har` 后零回源跑通 —— 未命中 **0**、命中 **561 全在 exact 档**、全程不消耗作者额度 | 断网完成生成与验证 | `playtest.cli capture` + `--har`；判据在 `tests/m3_offline.py` |
| 复现等级（本工具自己的指标） | 🔴 **不是每枚都位级复现**：作品 A（脱敏代号） 23 步那枚在同一份输入 HAR 上重放三遍，第一遍在第 18 步（场景切换）分叉 ⇒ 如实记 `async_dependent`；其余各枚三遍逐值一致。逐枚等级与计数**现读台账，不在此抄** | 同 | `evidence/corpus.json` + `evidence/ledger/*_repeat3.json`（`tools/corpus.py` 现校验） |
| 渲染后端 | 🔴 **SwiftShader 软件渲染**：`ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)` | 现读同一字段 | `window.__GSPP__.render()`；**这条决定 WebGL 类指纹能不能跨机背书**，拿到节点第一件事就是读它 |

**为什么"离线能力"这一行值得单独摆出来**：如果核心推理全走云端 API，这台机器就是多余的。
而本工具已经实测过"把一个线上应用整站取到本地、零联网重放并逐帧复现"——
同机共存（推理 + 两个无头浏览器 + 素材服务）与"用户的工程一个字节都不出本机"这两条，
是本地推理在这个任务上不可被云替代的部分。这一行有读数，不是话术。

**按角色准备（不是按姓名 —— 官网今日仍写"评委阵容待补充"，外部材料里具名的 7 位不予采信）**：

| 关切 | 递过去的东西 |
|---|---|
| 赞助方（这台机器值不值得给） | 离线能力行 + 渲染后端行 + 端到端耗时行；断网跑一次生成与验证 |
| 平台方（创空间能不能点开） | M3.1 预演 13/13 PASS 的截图与三按钮路径；评审期五天零改动的冻结计划 |
| 学术/方法论（可不可信） | 两臂对照的基准表 + 三组负控 + 已知限制清单（含我们**自己**被推翻的两条记录） |
| 工程/部署（稳不稳） | 稳定性行 + 144 局 error 0 + 内存三档（未测就写未测） |
| 运营/合规 | `ASSETS.md` 八条验证命令全部 0 命中 + AI 生成声明 + 素材授权清单 |

---

## 6. 拿不到节点时怎么办

官方明说：「未获得算力支持不影响参赛，可使用其他开发资源完成作品。」

1. 推理全部走魔搭 API Inference —— 策略层同一套代码，只换 `PLAYTEST_BASE_URL`；
2. 本目录仍作为**方案 + 等价验证**交付：在本地 x86 上跑同一套 SGLang 配置，
   把 `sm_121` / 统一内存 / arm64 wheel 三处差异逐条列出并说明推算依据；
3. 🔴 README 与手记里必须写明"这是等价验证，不是 Spark 实测"。
