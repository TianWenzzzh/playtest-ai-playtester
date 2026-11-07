# 接入你的游戏：注册块实测 11–48 行（中位数 29）

> 🔴 文件名里的 `20-lines` 是历史遗留：那个数从没被量过。现测口径与逐处读数跑
> `python tools/adapter_linecount.py`（零接入兜底那一路实测就是 0 行）。

目标：让一个已经写完的 HTML5 小游戏变成可被 Agent 读取、可下发动作、**可确定性重放**的环境。

## 最省事的路：什么都不写

PlayTest 有 DOM/Canvas 兜底模式。把你的游戏 URL 给它，它会：

- 从可见文本、按钮的 `disabled` 集合、输入框值、canvas 降采样像素指纹**推断状态**；
- 从可交互元素（`button` / `[role=button]` / `a[href]` / `input` / `canvas` / `[onclick]`）
  加常用键集（方向键、Space、Enter、WASD）**推断动作**；
- 用真实 `pointerdown / mouseup / click` 与 `keydown / keyup` **下发动作**。

```bash
python -m playtest.cli scan https://你的游戏地址 --seeds 20261105
```

这条路能查出：崩溃、UI 死路（按钮没接逻辑）。
查不出：软锁、不可通关 —— 因为工具看不到"赢"是什么定义。

## 高保真的路：注册（实测中位数 29 行）

在你的游戏里加这一段。**三个函数是必需的，其余可选但各换一项能力。**

```js
window.__GSPP__.register({
  meta: { id: "my-game", ticksPerAction: 6 },

  // ① 必需：当前状态。返回纯数据，别返回对象引用或 DOM 节点
  read: () => ({ level: G.level, score: G.score, hp: G.hp, won: G.won, dead: G.hp <= 0 }),

  // ② 必需：可用动作。名字要稳定（回放靠名字）
  actions: () => [{ name: "jump" }, { name: "left" }, { name: "right" }],

  // ③ 必需：把动作打进去。可以直接改内部变量，也可以派发事件
  apply: (a) => {
    if (a.name === "jump") tryJump();
    if (a.name === "left") G.input.left = true;
    if (a.name === "right") G.input.right = true;
  },

  // ④ 换 softlock / unwinnable 两个判据：不写就只能查崩溃和死按钮
  isOver: (s) => s.won || s.dead,
  isWin:  (s) => !!s.won,

  // ⑤ 换 invariant_violation 判据：把你心里"这数不该这样"的规则写出来
  invariants: (s) => {
    const bad = [];
    if (s.hp < 0 || s.hp > 3) bad.push("hp 越界: " + s.hp);
    if (s.score < 0) bad.push("score 为负");
    return bad;
  },

  // ⑥ 状态等价投影：声明哪些字段决定"局面本身"
  // 🔴 硬规矩：**凡是影响转移的字段都必须进来**，只有纯记账字段（分数、计时器、回合数）能省。
  // 省错了不是精度下降，是报出**错误的缺陷类型**。
  // 实测：漏掉门控跳跃的 cooldown ⇒ 键空间里所有动作成自环 ⇒ unwinnable 被误报成 softlock。
  key: (s) => ({ level: s.level, hp: s.hp, won: s.won, dead: s.dead }),

  // ⑦ 换"精确还原初始状态"（比只靠种子强，能测中途局面）
  snapshot: () => JSON.parse(JSON.stringify(G.serialize())),
  restore:  (st) => G.loadFrom(st)
});
```

## 三种常见情况

### A. 游戏是 rAF 主循环，随机用 `Math.random()`

**不用改。** PlayTest 注入的虚拟时钟会接管 `requestAnimationFrame` / `setTimeout` /
`performance.now` / `Date.now`，种子化随机会接管 `Math.random`。

两个例外：
- 你用了 `crypto.getRandomValues()` ⇒ 那一处不受种子控制，工具会明确报
  "此游戏不可确定性复现"，而不是假装复现成功。改成可注入的 RNG 才能进基准。
- 你做手感依赖真实墙钟（节拍判定、连招窗口）⇒ 时间被量化到帧栅格后保真度下降。
  这是确定性的代价，请知情选择。

### B. 游戏在 iframe 里

把 URL 指向**内层 frame** 的地址。跨域 iframe 目前不支持（探针注入不了进去）。

### C. 游戏是 Phaser / Pixi / Unity WebGL

- **Phaser / Pixi**：canvas 里有场景图，注册模式直接读 `scene.sys.settings` 和你的变量即可；
  兜底模式靠像素指纹，能用但判不了软锁。
- **Unity WebGL**：状态在 wasm 里，DOM 兜底基本看不到东西。
  需要走注册模式并在 `apply` 里调你暴露的 `SendMessage` / JS 插件接口。

## 接完之后

```bash
# 扫
python -m playtest.cli scan 你的游戏 --seeds 20261105,20261106,20261107 --policy curiosity

# 把开火的复现包丢进 CI：不可复现时退出码 1
python -m playtest.cli replay out/scan/curiosity-20261105-<id>.json
```

CI 里那一步是这套东西最实用的地方 —— 复现包不再是一份好看的 JSON，
而是一个**会拦下回归的门**。

## 自查清单

- [ ] `read()` 返回的是纯数据（能 `JSON.stringify`，无循环引用）
- [ ] `actions()` 的 `name` 稳定，不随界面文案改动而变
- [ ] `apply()` 是幂等安全的（重复调用不会把游戏弄坏）
- [ ] 有 `isOver` / `isWin`（否则软锁类判据全部失效）
- [ ] 有单调递增字段时**必须**有 `key()`
- [ ] `key()` 里**没有漏掉任何影响转移的字段**（漏了会报出错误的缺陷类型，不是少报）
- [ ] 跑一次 `scan`，确认初始状态哈希在你机器上重跑两次一致
