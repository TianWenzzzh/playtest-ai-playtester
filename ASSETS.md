# 素材与依赖授权清单（ASSETS.md）

大赛参赛须知第 5 条原文要求：**使用的图片、音频、字体、模型须有合法使用权或为自制**，违者取消资格。
这份清单的存在理由就是把这句话变成可核对的东西，而不是口头承诺。
每一条都给了"怎么现读验证"的命令 —— 你不需要相信我，跑一遍即可。

## 1. 视觉与音频素材：**零外部素材**

| 类别 | 状态 | 现读验证命令（在仓库根目录跑） |
|---|---|---|
| 图片（png/jpg/gif/svg/webp） | **仓库里一个都没有** | `git ls-files \| grep -iE "\.(png\|jpg\|jpeg\|gif\|svg\|webp)$"` → 空 |
| 音频（mp3/wav/ogg） | 无 | `git ls-files \| grep -iE "\.(mp3\|wav\|ogg\|m4a)$"` → 空 |
| 字体文件（ttf/woff/woff2） | 无 | `git ls-files \| grep -iE "\.(ttf\|otf\|woff2?)$"` → 空 |
| 模型权重（safetensors/gguf/pth…） | 无 | `git ls-files \| grep -iE "\.(safetensors\|gguf\|bin\|pth\|onnx)$"` → 空 |
| CDN / 外链脚本 | 无 | `grep -rn "src=\"http\|@import\|fonts.googleapis" games probe playtest` → 空 |

四个夹具游戏（`games/blocky-jump`、`games/clicker-quest`、`games/maze-exit`、`games/vault-run`）
的**全部画面都由代码画出来**：Canvas 2D 的 `fillRect` / 路径、CSS 几何、以及 Unicode 字符（emoji 与汉字）。
文字用运行环境的**系统默认字体**，不加载任何字体文件。
⇒ 所以"素材侵权"这条红线在本仓库不是"我们小心规避了"，而是**没有可侵权的对象**。

## 2. 运行时依赖（第三方代码，各自许可）

| 依赖 | 许可 | 用途 |
|---|---|---|
| `playwright` | Apache-2.0 | 驱动浏览器、注入垫片、离线镜像路由 |
| Chromium（Playwright 下载或本机 Edge/Chrome） | BSD-3（Playwright 侧）；浏览器本体按各自 EULA **仅调用，不分发** | 真实执行环境 |
| `gradio` | MIT | 创空间 Web 界面 |
| `fastapi` / `uvicorn`（Gradio 依赖链） | MIT / BSD | 同上 |

本仓库自身：**MIT**（见 `LICENSE`）。
`pyproject.toml` / `requirements.txt` 是依赖的唯一出处，本表只写许可结论，不替代清单。

## 3. 🔴 一条必须说清的例外：离线镜像 `.har` 里确实含第三方字节

工具能把一个**别人写的线上应用**取成离线包（`playtest.cli capture`），录出来的 `.har`
里原样嵌着那个应用的 HTML/JS/CSS/图片响应 —— 那是他人的作品字节。

处置（三条都是可核对的事实，不是承诺）：

1. `.har` 一律落在 `out/` 下，而 `out/` 在 `.gitignore` 里 ⇒ **不随仓库分发**。
   现读验证：`git ls-files | grep -i "\.har$"` → 空；`git log --all --name-only | grep -i "\.har$"` → 空。
2. 取包只用于**本地复现**，且默认严格不回源（未命中就 abort），不会把别人的服务打爆；
   实测在真实作品上"全程零联网、不消耗作者一分钱额度"（`docs/offline-capture.md`）。
3. 我们对外引用的第三方作品**连名字与公开 URL 都不给**：仓库里只有不可追溯代号
   （`work-a` / `work-b` / `work-c`，见 `evidence/corpus.json` 的 `redaction` 注记），
   更不复制其源码、图片、音频。把它写进测试是因为它是"别人写的游戏"这个事实本身 ——
   我们的主张正是"不改一行代码也能测"；真实地址由使用者自己在本地供给。

⇒ 如果有人担心这套工具会助长搬运他人素材：工具录包只落在**使用者自己的磁盘**，
且协议文档里明写"复现包不是素材分发渠道"。

## 4. AI 生成声明（参赛须知第 4 条要求如实注明）

| 部分 | 由谁产出 |
|---|---|
| 协议设计、判据、虚拟时钟/种子化/探针三个垫片、离线镜像、修复闭环、界面、文档 | 人（我）设计与编写，Qoder 编码助手参与实现与调试 |
| 四个夹具游戏 | 人写的最小夹具（每个都带植入臂 + 干净臂，缺陷是**故意埋的**，不是 AI 生成的） |
| 演示视频/配图 | 由本仓库代码渲染 + 浏览器截屏，不生成图像素材 |
| 模型 | 本工具**不分发模型**；LLM 臂接的是使用者自备的推理端点（DGX Spark 上的 SGLang 或魔搭 API Inference） |
