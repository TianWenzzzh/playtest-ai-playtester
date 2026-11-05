# PlayTest · AI 试玩员

> 给它一个游戏，它自己玩、自己找出卡关，打包成**可以原样重放一遍**的复现包，
> 然后自己把代码改对，改完重放确认不红。
>
> 开源 · MIT · 任意 HTML5 游戏 **20 行接入**（或 **0 行**降级接入）

---

## 30 秒上手

```bash
pip install playwright && playwright install chromium
git clone <this-repo> && cd playtest

# 扫一个游戏，产出复现包
python -m playtest.cli scan games/clicker-quest --seeds 20261105 --policy curiosity

# 校验某个复现包是否真的可复现（退出码 0=可复现，1=不可）—— 可以直接进 CI
python -m playtest.cli replay out/scan/curiosity-20261105-<id>.json
```

跑通第一条命令的典型耗时：**< 15 秒**（本地文件、无模型调用）。

---

## 它解决的具体问题

做完一个游戏，你不知道它哪里坏。传统做法要么靠人试玩，要么靠截图录屏说"这里好像崩了"。
**录屏不是证据** —— 没人能按你录的那段重跑一遍。

PlayTest 把缺陷固化成一个**可确定性重放的复现包**：

```jsonc
{
  "schema": "gspp/repro/1",
  "initial_state": { "seed": 20261105, "snapshot": {...}, "hash": "sha256:..." },
  "actions": [ { "i":0, "action":"click:button:神秘门", "hash_after":"sha256:..." } ],
  "defect":    { "kind":"ui_dead_end", "signature":"deadend:click:button:神秘门@bfc68d817f16" },
  "verification": { "replayed": true, "steps_matched": 1, "steps_total": 1, "hashes_match": true }
}
```

任何人拿这三个字段（初始状态 / 随机种子 / 动作序列）重跑，**逐帧状态哈希必然一致**。
这才是"复现"两个字的含义。

---

## 核心机制：不改游戏代码，把不确定性从下面接管

浏览器里的游戏默认不可复现，因为时间吃墙钟、随机吃系统熵。
PlayTest 在游戏脚本执行**之前**注入三个垫片：

| 垫片 | 接管什么 | 换来什么 |
|---|---|---|
| `probe/virtual_clock.js` | `requestAnimationFrame` / `setTimeout` / `setInterval` / `performance.now` / `Date.now` | 时间改由宿主按固定帧栅格推进；`document.hidden` 强制可见 |
| `probe/seeded_random.js` | `Math.random` → mulberry32(seed) | 关卡生成可控；并**标记** `crypto.getRandomValues` 这类不受种子控制的调用 |
| `probe/probe.js` | 状态读写与动作下发 | 注册模式（高保真）或 DOM/Canvas 兜底模式（零接入） |

🔴 **作者不需要重构游戏**。这是它和"必须用我的引擎"类方案的根本区别。

---

## 接入：两种模式

### 模式一 · 注册（高保真，约 20 行）

```js
window.__GSPP__.register({
  meta: { id: "my-game", ticksPerAction: 6 },
  reset: () => newGame(),
  read:  () => ({ level: G.level, score: G.score, hp: G.hp, nextGap: G.nextGap() }),
  actions: () => [{ name: "jump" }, { name: "wait" }],
  apply: (a) => { if (a.name === "jump") tryJump(); },
  isOver: (s) => s.hp <= 0 || s.won,
  isWin:  (s) => !!s.won,
  invariants: (s) => (s.hp < 0 || s.hp > 3 ? ["hp 越界: " + s.hp] : []),
  snapshot: () => G.serialize(),
  restore:  (st) => G.loadFrom(st)
});
```

`read / actions / apply` 是全部必需项。其余可选，但：
- 没有 `isOver/isWin` ⇒ 判不了 softlock 与 unwinnable；
- 没有 `snapshot/restore` ⇒ 做不了可达性探测（探完回不来）；
- 没有 `invariants` ⇒ 数值类缺陷只能靠崩溃间接发现。

完整规范见 [`protocol/GSPP.md`](protocol/GSPP.md)。

### 模式二 · DOM/Canvas 兜底（0 行接入）

什么都没写的游戏自动降级：从可见文本、按钮 `disabled` 集合、输入框值、
canvas 降采样像素指纹推断状态；从可交互元素 + 常用键集推断动作；
用真实 `pointerdown/mouseup/click` 与 `keydown/keyup` 下发动作。

实测（本仓库自带 `games/clicker-quest/`，**一行协议都没接**）：
推断出 14 个动作，发现一个"可见可点但没有任何 handler"的死路按钮，
复现包在新实例里逐帧哈希全等。

保真度低于注册模式，但这是能用在别人游戏上的唯一办法。

---

## 缺陷分类

| kind | 判据 | 需要 |
|---|---|---|
| `crash` | 未捕获异常 / 页面错误数上升 | — |
| `invariant_violation` | 作者声明的不变量被违反 | `invariants()` |
| `softlock` |  horizon 步内到不了任何终局：赢不了也死不掉 | `snapshot/restore` |
| `unwinnable` | 可达终局全为失败：这局面已经不可能通关 | `snapshot/restore` |
| `ui_dead_end` | 控件可见可点，派发后状态指纹纹丝不动（含 40 帧二次确认） | 兜底模式 |

🔴 `horizon` 是启发式代价，代码里如实暴露：探测被截断时**只报"该深度内未找到"，
不断言"不存在赢的路径"**。

---

## 策略：同一把尺量五种

| 策略 | 是什么 | 为什么要有它 |
|---|---|---|
| `random` | 随机选 | 下限 |
| `curiosity` | 优先选试得最少的动作 | 不看语义的基线 |
| `semantic` | 30 行启发式：追目标相关变量的变化 | 🔴**如果 LLM 打不过它，"AI 能力"那部分就是虚的** |
| `llm` | 模型看结构化状态选动作，输出 JSON | 主臂 |
| `llm_memory` | 同上 + 死路记忆 | 证明"状态感知"真的有用 |

```bash
python -m playtest.cli bench --game games/blocky-jump --game games/clicker-quest \
        --policies random,curiosity,semantic,llm,llm_memory --seeds 20261105,20261106
```

模型端点走 OpenAI 兼容接口，全部用环境变量配置，**不内置任何 key**：

```bash
export PLAYTEST_BASE_URL=http://<your-dgx-spark>:8000/v1     # SGLang on DGX Spark
export PLAYTEST_API_KEY=...
export PLAYTEST_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
# 或者用魔搭 API Inference：只设 MODELSCOPE_API_TOKEN 即可
```

🔴 端点不可用时策略**如实降级**并在 trace 里标注
`"LLM 全程不可用，本列实际等价于 curiosity"` —— 不用别人的名字报自己的成绩。

---

## 正对照与正向对照：两件事都要有

**每个样例游戏都有"干净臂"** —— 工具在上面必须保持沉默：

```bash
# 有缺陷臂：间隙可达 6 而角色最多跳 3 ⇒ 必然出现不可通关局面
python -m playtest.cli scan games/blocky-jump --seeds 20261105

# 干净臂：把间隙上限压到 3 ⇒ 必定可通关，工具必须沉默
python -m playtest.cli scan "games/blocky-jump?maxgap=3" --seeds 20261105,20261106,20261107
```

只报召回不报误报的表没有意义。`tests/m0_smoke.py` 第 6 组就是这条。

**`oracle` 策略是检测器的正向对照，不参与策略排名。**
它恒选语义正确的那个动作，专门用来回答一个否则无法回答的问题：
一次 0 分到底是"仪器判不出"还是"策略走不到"？
`games/vault-run/` 把缺陷放在第 17 层（超出探测视界），
`oracle` 能走到并判出 ⇒ 于是其余策略的 0 分被确定地归因为**语义能力缺口**，而不是仪器坏了。

## 分辨力夹具：为什么需要它

前三个夹具的缺陷都是"一步可见"的，结果五套策略得分一模一样 ——
**那张表没有分辨力，它只证明了仪器正确，没证明模型带来任何增量。**
根因是状态空间小的时候，穷举可达性探测本身就是求解器，语义没有用武之地。

`games/vault-run/` 把两件事叠在一起：缺陷在搜索视界之外（必须真把游戏打上去），
而打上去要求连续读懂中文线索（随机撞中概率约 1e-8）。
⇒ 从此 `random`/`curiosity` 必然 0，能读语义的策略才有分。LLM 臂有了可测的靶。


---

## 已实测的能力边界（诚实清单）

1. **虚拟时钟把时间量化到帧栅格**。非整帧的 `setTimeout` 会被推迟到下一帧边界。
   ⇒ 依赖真实墙钟做手感判断的游戏（节拍、连招窗口）保真度下降。这是确定性的代价。
2. `crypto.getRandomValues` 不受种子控制 ⇒ 用它随机的游戏**不可复现**，
   工具会明确报出来，不会假装复现成功。
3. WebGL canvas 的像素指纹跨 GPU 未必逐位一致 ⇒ 兜底模式跨机复现强度弱于注册模式。
4. 可达性探测受 `horizon` 限制，状态空间大的游戏会截断。
5. iframe 内游戏需把 URL 指向内层 frame；跨域 iframe 不支持。

---

## 在 NVIDIA DGX Spark 上跑

赛题要求"核心 AI 推理须提供单台 DGX Spark 的部署方案"，见
[`deploy/dgx-spark/`](deploy/dgx-spark/)。要点：

- DGX Spark 是 **aarch64**（20 核 Arm）+ **CUDA 13.0 / sm_121** ⇒ 镜像与 wheel 必须是 arm64；
- **128 GB LPDDR5x 统一内存、273 GB/s** ⇒ 稠密大模型解码会被带宽打死，
  选 **MoE 小激活**（如 Qwen3-30B-A3B NVFP4）而不是稠密 70B；
- `nvidia-smi` 读不到显存（统一内存），SGLang 的 `--mem-fraction-static` 要降到 ~0.75。

---

## 与既有工作的边界

| | Ludra | GBQA | TITAN | WeTest Acorn | **PlayTest** |
|---|---|---|---|---|---|
| 接既有 HTML5 游戏 | ❌ 要求用它的引擎 | 部分 | ❌ 闭源商业游戏 | ❌ 需接入 | ✅ 20 行 / 0 行 |
| 确定性回放 | ✅ | ❌ | ❌ | （无 seed） | ✅ 逐帧哈希校验 |
| 复现包可进 CI | 附复现输入 | ❌ | ❌ | ❌ | ✅ `replay` 命令带退出码 |
| LLM 语义策略 | ❌ | ✅ 仅发现 | ✅ 但闭源 | 知识图谱 | ✅ 五策略同表对比 |
| **发现→打补丁→回放验证** | ❌ | ❌ | ❌ | ❌ | ✅ |
| 本地单卡部署 | — | — | — | 云 SaaS | ✅ |

定位一句话：**把 Gymnasium 的 `reset(seed)/step` 契约搬到浏览器里任意既有 HTML5 游戏上，
靠注入垫片而不是引擎改造来换确定性，把复现包当一等交付物，再接上修复腿。**

---

## 目录

```
probe/            三个注入垫片（浏览器侧，纯 JS，无依赖）
protocol/GSPP.md  协议规范与复现包 schema
playtest/         Python 主体：env / trace / detectors / policies / agent / fixer / cli / llm
games/            样例游戏（含植入缺陷与干净臂）
benchmark/        缺陷植入基准与策略对比
tests/            m0_smoke（15 项断言）/ m1_fallback（零接入）
deploy/dgx-spark/ arm64 SGLang 部署方案与实测
docs/             接入指南、扩展适配器写法、已知限制
```

## 许可

MIT。
