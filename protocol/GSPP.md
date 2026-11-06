# GSPP · Game State Probe Protocol (v0.1)

让**任意 HTML5 游戏**在**不被重写**的前提下，变成可被 Agent 读取、可被 Agent 下发动作、
且**可被逐帧确定性重放**的环境。

## 0. 设计立场

赛题 C 的验收原文要求输出：**初始状态、随机种子、完整动作序列**。
这三样要成立，前提是"同一套输入重跑必然得到同一结果"。浏览器里的游戏默认做不到，因为：

| 不确定性来源 | 对策 |
|---|---|
| `requestAnimationFrame` / `setTimeout` 由真实墙钟驱动 | **VirtualClock**：在游戏脚本执行前接管 rAF / `performance.now` / `Date.now` / `setTimeout` / `setInterval`，改由固定步长调度器驱动 |
| `Math.random()` 不可控 | **SeededRandom**：同批接管，替换为 `mulberry32(seed)` |
| 游戏内部状态读不到 | **两级适配器**：作者注册 adapter（高保真）→ 否则 DOM/Canvas 自动推断（零接入） |
| 输入时序抖动 | 动作以**离散 tick 边界**下发，一个动作 = 一次 `apply()` + `advance(ticks_per_action)` |

🔴 **关键卖点**：**作者不需要重构游戏**。VirtualClock + SeededRandom 是注入的垫片，
对未改造的 rAF 游戏同样生效；协议只负责"读状态"和"列动作"。

### 1.1 `key()` 投影的正确性要求（踩出来的，不是理论）

`key()` 声明"哪些字段决定局面本身"，用于可达性探测的状态去重。它有一条硬规矩：

> 🔴 **凡是影响转移的字段，必须出现在 `key()` 里。** 只有"跟着走但绝不反过来影响决策"的
> 记账字段（分数、经过时间、回合计数）才能省略。

违反的后果不是精度下降，是**报出错误的缺陷类型**。实测案例：
`blocky-jump` 的 `cooldown` 门控着"能不能跳"，第一版 `key()` 把它省了 ⇒
在键空间里所有动作都成了自环 ⇒ 探测看不见任何进展 ⇒ 把 `unwinnable`
（能死、赢不了）误报成 `softlock`（既赢不了也死不掉）。

报错类型比不报更糟 —— 别人会照着错的诊断去修，修不好，然后怀疑工具。

所以工具侧加了护栏 `key_is_too_coarse()`：
当"可达终局为空"但"全量哈希其实在变"（`distinct_positions ≤ 1 && distinct_full > 1`）时，
判定为投影声明过粗，**拒绝下 softlock 结论**，宁可不报。

接了协议的人如果看到某个判据一直不触发，第一件该查的就是 `key()` 漏没漏转移相关字段。


命名与语义显式对标 Gymnasium（`reset(seed)` / `step()` / `observation_space` / `action_space`），
不另造一套词汇。

---

## 1. 页面侧 API

垫片在游戏脚本之前注入，游戏可选注册 adapter：

```js
window.__GSPP__.register({
  version: "0.1",
  meta: { id: "blocky-jump", title: "方块跳", ticksPerAction: 4 },

  // 必填：返回当前可读状态（纯数据，可 JSON.stringify）
  read() { return { score: G.score, x: G.player.x, hp: G.hp, level: G.level }; },

  // 必填：返回当前可用动作。可以是静态数组，也可以依状态裁剪
  actions() { return [
    { name: "left",  label: "左移" },
    { name: "right", label: "右移" },
    { name: "jump",  label: "跳跃" },
    { name: "wait",  label: "不动" },
  ]; },

  // 必填：把动作落到游戏上。实现可以是 dispatchEvent、可以是直接改内部变量
  apply(action) { G.input.press(action.name); },

  // 可选：终局判定
  isOver(s)  { return s.hp <= 0 || s.won; },
  isWin(s)   { return !!s.won; },

  // 可选：不变量。每步校验，违反即记为 invariant_violation 缺陷
  invariants(s) {
    const bad = [];
    if (s.hp < 0 || s.hp > 3) bad.push("hp 越界: " + s.hp);
    if (s.score < 0)          bad.push("score 为负");
    return bad;
  },

  // 🔴 可选但强烈建议：状态等价投影 —— 声明哪些字段决定「局面本身」。
  // 不声明的话，带回合数/计时器的游戏会因为状态永不重复而被判成无限状态空间，
  // 可达性探测永远截断，softlock 与 unwinnable 一个都判不出来。
  // 这不是假想：本仓库的 maze-exit 夹具第一次就踩中了这个。
  key(s) { return { level: s.level, hp: s.hp, nextGap: s.nextGap }; },

  // 可选：状态快照的存取（用于"初始状态"精确还原，比只靠 seed 更强）
  snapshot() { return JSON.parse(JSON.stringify(G.serializable())); },
  restore(s) { G.loadFrom(s); },
});
```

**接入成本目标：中位数 20 行。** `read/apply/actions` 三个函数是全部必需项。

---

## 2. 宿主侧（Python）API

```python
env = BrowserEnv(game_url, init_scripts=[CLOCK_JS, RANDOM_JS, PROBE_JS])
snap = env.reset(seed=20261105)          # -> Snapshot
acts = env.actions()                      # -> [Action]
snap = env.step("jump")                   # apply + advance ticks + read + hash
raw  = env.raw_state()                    # 适配器未注册时为 DOM/Canvas 推断结果
env.close()
```

`Snapshot`:
```python
{"t": 137, "hash": "sha256:...", "data": {...}, "over": false, "win": false, "errors": [...]}
```

`hash` = SHA-256 over **canonical JSON**（键排序、无空白、数字用定点字符串化）。
回放校验就是比 hash 序列，不是比截图。

### 2.1 指纹读在哪一刻（哈希时机是协议的一部分）

`env.step()` 不是"推进 k 帧然后读一次"，而是**推进 k 帧，再读到静息为止**：
反复"推进 `settle_step` 帧 + 重读"，直到连续两次读数相同，或用完 `settle_rounds` 预算。
理由：`Date.now`/`performance.now`/rAF/定时器都在我们手里，但**图片解码、
懒加载分块挂载、React 异步边界的完成时刻落在真实时间上**，
按固定帧数读就会把"新场景正在出图"那一瞬当成局面。
永不安息的动画类落到预算上限——而在被接管的时钟下"第 K 帧"本身就是确定的。

配套两条硬规矩（都是实测教出来的，见工具仓 `docs/offline-capture.md` §5.5）：

1. **观测不许推进录制实例的时钟还不同步记账**。可达性探测、死路确认都会空推若干帧；
   这些帧必须落在复现包的时间线里 —— 包内每步记了"读到第几帧"，
   `replay()` 就按那个帧数补齐再比。**观测的成本也在时间线里。**
2. **持久状态要"变了才装"**。包里那份存档常常等于干净开机会自己写出来的那份，
   此时装载 + 多推一段帧只会让回放偏离录制；真变了才 `reboot()` 重载页面，
   让应用在**开机时**读到它。

`Snapshot.assets` 另带一份非哈希的诊断读数（`undecoded` / `broken` / `resources`），
用来区分"这一刻状态真没变"和"还有图在解码"；它不参与指纹，所以不会改变任何既有哈希域。

---

## 3. 复现包 Schema

```jsonc
{
  "schema": "gspp/repro/1",
  "game": {
    "url": "https://...",
    "name": "方块跳",
    "probe_mode": "registered | dom-fallback",
    "assets": { "index.html": "sha256:..." }          // 证明"测的就是这个包"
  },
  "initial_state": { "seed": 20261105, "snapshot": {...}, "hash": "sha256:..." },
  "actions": [
    { "i": 0, "action": "jump", "hash_before": "sha256:...", "hash_after": "sha256:..." }
  ],
  "defect": {
    "kind": "softlock | crash | invariant_violation | unreachable | ui_dead_end | balance_break",
    "first_seen_step": 137,
    "signature": "stuck@sha256:...|12-actions-exhausted",
    "detail": { }
  },
  "verification": {
    "replayed": true,
    "steps_matched": 137,
    "steps_total": 137,
    "hashes_match": true
  },
  "evidence": { "screenshots": [...], "console": [...], "duration_ms": 84213 }
}
```

🔴 `verification` 是这个 schema 的灵魂：它把"我发现了一个 bug"变成
"任何人用这包数据重跑，137 步逐帧哈希一致，缺陷必然再现"。

---

## 4. DOM Fallback（零接入模式）

未注册 adapter 时自动降级：

- **state** = DOM 规范化指纹：可见文本（截断）、`aria-*`、按钮 `disabled` 集合、
  输入框值（`checkbox`/`radio` 额外带 `checked`——只读 `value` 时勾选框恒为 `"on"`，
  勾上与取消在指纹里完全隐形）、canvas 的像素采样哈希（离屏 2D 降采样后取哈希）
- **actions** = 可交互元素集合（`button` / `[role=button]` / `a[href]` / `input` / `canvas`）
  + 常用键集（Arrow/Space/Enter/WASD）
- **apply** = 真实派发 `pointerdown/pointerup` 或 `keydown/keyup`。
  两个例外是必须的：**非可信事件跑不动浏览器自己实现的原生行为**——
  滑杆的轨道拖拽与 `<select>` 的弹层都点不动（实测在真实作品上把音量滑杆误报成死路）。
  动作空间里没有坐标，所以协议给出一个确定的解释：
  `range` 按 `step` 前进一格、到头回绕，用原生 value setter 写入并派发 `input`/`change`；
  `select` 选中下一项并派发 `change`。
- **软锁判定** = 穷尽动作后 state 指纹不变 ⇒ softlock
- **UI 死路判定** = 元素可见可点，且**该控件类别的变化确实在指纹域内可见**，
  而本局遇到的所有局面下派发后都无任何状态/哈希变化且无导航 ⇒ `ui_dead_end`

🔴 `ui_dead_end` 报的是**待确认信号**，不是判决书。
它能证明的只是"这个可见控件在遍历过的所有局面里从未产生任何状态变化"；
而"有意锁着的空控件"与"忘了接逻辑的控件"在指纹上是同一个样子——
判定意图需要作者。报告措辞必须留这个余地（实测案例：
`Boss 战 …，尚未抵达，查看详情` 的无反应很可能是设计如此）。

保真度低于注册模式，但**这是让它能用在别人那 400 多个作品上的唯一办法**，
也是"实用价值 30 分"和"开源与复用 10 分"的实际载体。

---

## 5. 与既有工作的边界（必须说清，否则会被判"又一个 wrapper"）

| 既有物 | 它做了什么 | 它没做什么 |
|---|---|---|
| **Gymnasium** | `reset(seed)/step` 契约与空间描述的标准词汇 | 载体是 Python 进程内环境，**不能直接套浏览器里的 JS 游戏** |
| **Gym Retro / ALE** | ROM savestate 位级回放、RAM 观测 | 载体是二进制 ROM + 内存逆向，**不覆盖 Web** |
| **ML-Agents / Unity-MCP** | 引擎内注册自定义工具给 Agent | 需要 C# 插件与编辑器集成，**仅 Unity** |
| **Ludra**（MIT） | 自有引擎内：JSON 场景、位级确定、`fuzz` 报 softlock/unwinnable 并附复现输入 | 🔴**要求"在我的引擎里做游戏"**；不接既有 HTML5 游戏；无 LLM 语义策略；无"发现→打补丁→回放验证"闭环 |
| **canvas 视觉 bug 研究**（arXiv 2208.02335 / 2501.09236） | 指出 canvas 内容不进 DOM 导致现有 web 测试工具失效 | 只做视觉异常检测，**不产出可重放的动作序列** |

⇒ GSPP 的位置：**把 Gymnasium 的 `reset(seed)/step` 契约搬到浏览器里任意既有 HTML5 游戏上，
用注入垫片而非引擎改造来换取确定性**。这是 Web 侧的空白（Web 游戏目前只有输入标准，没有状态导出标准）。
