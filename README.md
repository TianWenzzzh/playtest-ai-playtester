# PlayTest · AI 试玩员

> 给它一个游戏，它自己玩、自己找出卡关，打包成**可以原样重放一遍**的复现包，
> 然后自己把代码改对，改完重放确认不红。
>
> 开源 · MIT · 注册适配器**实测 11–48 行**（中位数 29；现读 `python tools/adapter_linecount.py`）；
> 一行都不写的降级路径已在 3 件真实第三方作品上实测（作者零改动）
>
> 当前开发状态只看工作区 `09-作战控制台.md`；当前认证是否有效，须比较本树源码摘要与
> 重新运行 `tools/certify.py` 生成的 `evidence/latest.json`。本 README 不是实时状态表。

---

## 30 秒上手

```bash
pip install playwright && playwright install chromium
git clone <this-repo> && cd playtest

# 扫一个游戏，产出复现包
python -m playtest.cli scan games/clicker-quest --seeds 20261105 --policy curiosity

# 校验某个复现包是否真的可复现（退出码 0=可复现，1=不可）—— 可以直接进 CI
python -m playtest.cli replay out/scan/curiosity-20261105-<id>.json

# 线上的游戏：先取包，之后所有读数都从磁盘回答（零联网，异步时序这个非确定源被摘掉）
python -m playtest.cli capture https://someone.ms.show/ --har someone.har --warm-clicks
python -m playtest.cli capture https://someone.ms.show/ --har someone.har --top-up  # 补包：撑开深层覆盖面
python -m playtest.cli scan https://someone.ms.show/ --har someone.har
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

复现的等级不由这句话本身保证，由**回放时喂进去的字节**决定，所以分两档说：

- **本仓库自带的样例**：拿这三个字段在新实例里重跑，逐帧状态哈希全等 —— 这是认证每一步与
  `tests/release_quickstart.py`（干净临时目录从零产出再回放）实际在量的东西。
- **真实第三方作品**：只有回放吃的还是**同一份离线快照**时，重放的才是同一枚包。换一份快照就换一枚
  `pack_id`，那是输入变了，不是机器不稳。⇒ 因此任何对外结论都同时给 `pack_id` 和输入快照哈希，
  逐枚读 `evidence/corpus.json`，逐字节校验跑 `PYTHONUTF8=1 python tools/corpus.py`。

"复现"两个字的含义是**可以被别人原样重跑并同样红/绿**，不是"我们保证任意环境下逐帧一致"。

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

### 模式一 · 注册（高保真，实测中位数 29 行）

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
| `semantic` | 45 行启发式（实测类跨度）：追目标相关变量的变化 | 🔴**如果 LLM 打不过它，"AI 能力"那部分就是虚的** |
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
   🔴 这条现在有本机读数了：探针自证渲染后端，实测这台开发机一直是软件渲染
   （`ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)`）。
   我们全部位级证据都是**录制与回放同环境**取的，Canvas 2D 在软件渲染下仍逐像素确定，
   所以确定性结论不受影响；但**不能拿本机的绿去替别人的 GPU 背书**。
   报警不藏在注释里：`scan` 的 stderr 与界面 stream 面板都会把这句话贴在读数旁边。
4. 可达性探测受 `horizon` 限制，状态空间大的游戏会截断。
5. **iframe 里的游戏可以测，但复现包要带对的那份持久状态**。
   实测过"平台壳 + 跨源 iframe"的形态：init script 逐 frame 生效，跨源也能读；
   真正难的是壳会把应用入口 302 回自己的页面，且 iframe URL 每次带一个新时间戳 ——
   这两条由 `env._resolve_frame()` 与离线镜像的三档命中处理（见
   [`docs/offline-capture.md`](docs/offline-capture.md)）。
6. **服务端依赖是复现等级的硬上限**。带推理接口的应用（POST 一发就花钱那种）
   离线兜不住，工具把它显式记成未命中并降格，而不是假装跑通。
7. **离线摘掉网络，但解码曾经仍然漏进指纹** —— 这条 11-06 下午修掉了，过程留着。
   机制不是"解码慢"，而是：**被接管的虚拟时钟只在我们叫它走的时候走，所以"推进 16 帧"
   消耗的墙钟时间≈0**，而图片解码是落在真实时间上的浏览器任务。
   于是"连续两次哈希相同"这个静息条件几乎必然在**解码还没落地的那一瞬间**成立 ——
   它量的是"虚拟帧之间没变化"，量不到"真实世界里还有一张图正在解"。
   当时的实测形状（🔴 11-06 下午那一批，**已被下面绑输入快照的三遍读数取代**）：
   一枚 23 步包 `23/23 / 23/23 / 23/23` 全等，
   另一枚 26 步包 `20/26 → 20/26 → 21/26` —— **分歧点自己会移动**，那枚如实记 `async_dependent`。
   现在的静息条件是三条同时成立：**两次哈希相同 · 没有在途图片解码 · 资源条目数不再增长**，
   并且只在真有在途工作时才睡一小段真实时间（夹具零成本）。
   改后读数**按枚分开、且绑输入快照**（`tools/replay_repeat.py` 在同一份 HAR 上把 6 枚包各重放三遍，
   逐遍读数与 `offline_har_sha256` 一起存进 `evidence/ledger/*_repeat3.json`，`tools/corpus.py` 现校验）：
   **5 枚三遍逐值一致**（牛马 20 步 / 优宠 3、5 步 / 千门 3、2 步）；
   🔴 **牛马地下城 23 步那枚（`ef6b22c199b40365`）三遍里第一遍在第 18 步
   `click:button:开始冒险` 分叉（18/23），后两遍 23/23** ⇒ 如实降格 `async_dependent`，
   不写成"能复现"。分叉动作是场景切换，但**归因未闭合**：三遍的 `wall_ms`
   （3790 / 3865 / 3604，见 `evidence/ledger/niuma_repeat3.json`）与是否分叉不相关，
   所以"解码逃得出静息判据"只是假设之一；另一条更便宜的假设是 `replay()` 的存档注入分支
   （`storage_injected` 至今没被记进 ledger，而历史上同位置分叉的真因正是回放多推了一段帧）。
   先做字段级诊断，再谈改判据。
   同环境"同一 seed 连录两遍动作序列逐值相同"（以前这里会漂：26 / 25 / 26 步）
   量的是 11-05 那份快照（当时一次性核验脚本判 18/18；那份脚本与日志留在开发工作区、**不随仓库分发**，
   仓内等价判据现跑 `PYTHONUTF8=1 python tests/m3_offline.py`）；**跨快照的包身份不同，两句不许互借**。
   🔴 用完预算仍不安息时不假装安静：`Snapshot.assets.settle_unsettled` 会如实带上。
8. **复现失败先怀疑仪器，再宣称原理**。真实作品上曾被归给"异步"的分歧，
   真因是回放路径与录制路径不一致（多推一段帧 / 没按包里记录的帧时间线补齐）。
   这两处修好后曾取得**单次**回放 23/23；🔴 但"三次重放都全等"这句已被第 7 条的三遍读数否证，
   不许再写。26 步那枚属 11-05 旧快照，跨快照包身份不同，只作历史。
   判据要求的是"≥1 枚可复现"，当前由另外 5 枚三遍逐值一致成立；
   "真实作品的位级复现已全部解决"这句更大的话不成立。见 `docs/offline-capture.md` §5.5。

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
| 接既有 HTML5 游戏 | ❌ 要求用它的引擎 | 部分 | ❌ 闭源商业游戏 | ❌ 需接入 | ✅ 注册实测 11–48 行 / 0 行降级 |
| 确定性回放 | ✅ | ❌ | ❌ | （无 seed） | ✅ 逐帧哈希校验 |
| 复现包可进 CI | 附复现输入 | ❌ | ❌ | ❌ | ✅ `replay` 命令带退出码 |
| LLM 语义策略 | ❌ | ✅ 仅发现 | ✅ 但闭源 | 知识图谱 | ✅ 五策略同表对比 |
| **发现→打补丁→回放验证** | ❌ | ❌ | ❌ | ❌ | ✅ |
| 本地单卡部署 | — | — | — | 云 SaaS | ✅ |

定位一句话：**把 Gymnasium 的 `reset(seed)/step` 契约搬到浏览器里的 HTML5 游戏上（凡实现本契约者），
靠注入垫片而不是引擎改造来换确定性，把复现包当一等交付物，再接上修复腿。**

---

## 目录

```
probe/            三个注入垫片（浏览器侧，纯 JS，无依赖）
protocol/GSPP.md  协议规范与复现包 schema
playtest/         Python 主体：env / trace / detectors / policies / agent / fixer / offline / cli / llm
games/            样例游戏（含植入缺陷与干净臂）
benchmark/        缺陷植入基准与策略对比
tests/            m0_smoke（确定性内核）/ m1_fallback（零接入）/ m2_fix（修复闭环）
                  m3_offline（离线取包端到端）/ m3_mirror_semantics（镜像匹配语义，不开浏览器）
                  m4_certify（认证器红绿负控）/ m5_corpus（外部语料台账负控）
                  m6_benchmark_note（基准表零命中的归因只认降级读数，不认列名）
                  m7_outward_wording（门面不许无界量词；成本类行数必须等于 adapter_linecount 的现测值）
                  m8_llm_client（模型客户端 HTTP 行为：默认不发魔搭不支持的 response_format；失败时错误体真因进 last_error）
                  real_targets（真实第三方作品）—— 各项断言数以运行输出为准，文档里不抄数
tools/            certify.py（一键认证）/ corpus.py（外部语料校验）/ replay_repeat.py（同快照连重放 N 遍）
                  adapter_linecount.py（现测注册块行数分布；m7 判据 5 与门面文档共用这一把尺）
evidence/         corpus.json（外部作品台账）+ ledger/（随仓库走的脱敏证据读数，不含 HAR 与响应字节）
                  latest.json 与 runs/ 为本机生成证据，已 gitignore
deploy/dgx-spark/ arm64 SGLang 部署方案与实测
docs/             接入指南、离线取包、扩展适配器写法、已知限制
```

## 认证与外部语料

```bash
PYTHONUTF8=1 python tools/certify.py fast
PYTHONUTF8=1 python tools/certify.py full
PYTHONUTF8=1 python tools/certify.py release
PYTHONUTF8=1 python tools/corpus.py
```

- `fast` 运行核心套件并校验外部语料台账。
- `full` 在 `fast` 基础上追加真实作品离线端到端与基准跑批。
- `release` 会额外要求 `tests/release_ui.py`、`tests/release_public.py`、`tests/release_quickstart.py` 三条发布验收腿。任一脚本缺失或前置未满足时按设计返回 `RC=2`，**不代表发布认证已通过**。
- `tools/corpus.py` 校验外部作品的证据路径与 SHA-256；作者确认没有证据就无法升级状态。
  🔴 证据文件本体（脱敏后的读数与逐遍回放结果，不含 HAR 与第三方响应字节）随仓库走，
  路径解析逃出仓库根就判红 —— 这条是 11-06 晚从"匿名 clone 里第一件事就吃 RC=1"那次实测补的，
  所以任何人 clone 下来跑这一条，看到的绿与作者机器上的绿是同一份字节。
- `tools/replay_repeat.py` 把已有的复现包在**同一份快照**上连重放 N 遍，逐遍留 `replayed /
  steps_matched / hashes_match / mismatch` 与 `offline_har_sha256`，台账等级由它写。

`evidence/latest.json` 只能由认证器生成。它绑定当次源码摘要、真实退出码与逐条日志哈希；某一步失败时，它会覆盖旧绿牌，而不是保留旧绿牌冒充当前状态。

## 许可

MIT。

素材与依赖授权清单见 [`ASSETS.md`](ASSETS.md)：四个夹具的画面全部由代码画（Canvas 2D / CSS 几何 / Unicode），
仓库里**没有任何图片、音频、字体、模型文件**，也没有 CDN 外链 —— 那几条都配了可现读的验证命令。
离线取包录出的 `.har` 里确实含第三方应用的响应字节，因此它落在 `out/`（已 gitignore）且从不随仓库分发。
