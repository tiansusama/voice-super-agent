# 开发运行

当前已完成阶段 03 的文字对话：DeepSeek 流式回复、连续追问、文字接管、消息快照及错误反馈。独立工程、单实例、托盘常驻和悬浮宿主沿用阶段 02。真实模型与桌面验收结果见 [阶段 03 验证记录](verification/stage-03.md)。页面启动无需模型凭据，发送文字需要配置 DeepSeek。

在已准备好的项目根目录启动桌面：

```powershell
uv run --locked voice-super-agent
```

`frontend/index.html` 是 Vite 源码入口，其中的 TypeScript/TSX 需要构建或由开发服务转换。浏览器通过 `file://` 直接打开它会白屏。完整应用由上述 Python 命令启动，宿主加载前端页面并提供后台桥接；开发服务模式见第 4 节。

## 1 环境

- Windows 11 x64。
- uv，项目使用 Python 3.12，通过 `.python-version` 与 `requires-python` 指定。
- Node.js 20.19 及以上的 20.x，或 22.12 及以上版本；本机准备时使用 Node.js 24。
- Microsoft Edge WebView2 Runtime 和 .NET Framework 4.6.2 及以上，供 pywebview 的 EdgeChromium 宿主使用。

Python 依赖由 uv 管理，前端使用 npm。以下命令均从项目根目录运行。

## 2 首次准备

```powershell
uv sync --locked
npm --prefix frontend ci
```

Pipecat 依赖声明来自官方 Git 仓库，使用 STS 对应提交 `de8998789d5690073ddd1dafcec7268839af8153`。第一次安装需要 Git 与网络，uv 会把源码放入自己的缓存并安装到本项目 `.venv`。所需版本由 `uv.lock` 记录，运行不使用旁边 STS 的源码或 `.venv`。

前端由 `frontend/package-lock.json` 记录确切依赖。阶段 01 无需额外准备语音模型，依赖包自带资源随依赖安装；后续独立模型资源约定见 [资源说明](../resources/README.md)。

将 `.env.example` 复制为 `.env`，填写 `DEEPSEEK_API_KEY` 即可启用文字请求。已有 `.env` 时直接补充字段。模型默认 `deepseek-flash`，地址默认 `https://api.deepseek.com/v1`，可通过 `DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL` 设置，修改后重启应用生效。文字模式只使用 DeepSeek，不需要语音服务凭据或麦克风。唤醒词、组合键和超时属于后续日常设置。

## 3 加载前端构建产物

```powershell
npm --prefix frontend run build
uv run --locked voice-super-agent
```

构建先进行 TypeScript 检查，再生成 `frontend/dist`。桌面通过 pywebview 的本地静态服务加载页面。没有构建产物时，启动命令会说明需要执行的构建命令。

页面就绪后订阅 `agent-runtime` 线程的状态，显示“文字对话已就绪”或配置缺失原因。输入消息后按 Enter 发送，Shift + Enter 换行；中文输入法选词时的 Enter 保留给输入法。回复期间可以发送新消息接管，已显示的旧回复文字保留，新回复结合原生提交的上下文生成。

页面重载后重新取得消息快照，后续推送按序号衔接。主窗口隐藏或最小化期间，后台继续生成。当前消息保存在本次进程内，明确退出后清空；数据库保存和重启恢复属于阶段 04。

请求失败原因显示在对应助手消息旁，普通服务错误后可以继续发送文字；鉴权等永久错误会显示需要检查配置并重启的提示。应用和 SDK 均关闭自动请求重试。失败消息旁的重新发送及完整能力恢复在阶段 09 接入。

关闭主窗口或点击“隐藏到托盘”后应用继续运行。最小化保留 Windows 的最小化状态。双击托盘图标或选择“显示主窗口”可找回主窗口；再次执行启动命令也会唤出同一个窗口。通过页面或托盘的“退出应用”结束应用和后台循环。

## 4 连接 Vite 开发服务

先在一个终端运行：

```powershell
npm --prefix frontend run dev
```

保持该终端运行，在另一个终端启动桌面：

```powershell
uv run --locked voice-super-agent --dev
```

开发服务固定监听 `http://127.0.0.1:5178`，仅在本机访问；端口被占用时 Vite 直接报错。`--dev` 在打开窗口前检查服务连接，未启动时给出原因和命令。修改 React 页面可通过 Vite 更新桌面内容。

明确退出桌面后，在运行 Vite 的终端按 `Ctrl+C` 结束开发服务。

## 5 桌面联调

加载构建产物并打开联调入口：

```powershell
uv run --locked voice-super-agent --desktop-check
```

也可以与 `--dev` 一起使用。主窗口会增加“桌面联调”区域：

- “开始后台活动”启动真实 asyncio 任务，每秒产生约 20 次计数状态；停止后保留当前计数。
- “预览悬浮宿主”隐藏主窗口，在工作区右下角显示圆角卡片和圆球。卡片支持内部滚动，圆球切换卡片展开与收起。
- 卡片的“×”只隐藏悬浮组件；托盘中的“显示收音窗”可找回已预览过的宿主。“打开主窗口”会收起悬浮组件并显示主窗口。

联调区域用于验证窗口、线程和推送。语音驱动的自动显示、收音音量环和单轮问答在对应阶段接入。普通启动时托盘的“显示收音窗”先保持不可用。

可以在后台活动持续运行时关闭主窗口、重复启动、最小化或重载页面，检查计数接续；完成后通过“退出应用”结束任务和窗口。当前计数属于内存联调状态，明确退出后归零。

## 6 数据与产物位置

| 位置 | 用途 | Git 管理 |
| --- | --- | --- |
| `.venv/` | 本项目 Python 环境 | 忽略 |
| `frontend/node_modules/` | 前端依赖 | 忽略 |
| `frontend/dist/` | 前端构建产物 | 忽略 |
| `.env` | 本地服务凭据 | 忽略；提交配置示例 |
| `.local/data/` | 后续数据库等本地数据 | 忽略 |
| `.local/logs/app.log` | 启动、订阅、窗口操作和退出日志 | 忽略 |
| `.local/webview/` | WebView2 本地存储 | 忽略 |
| `resources/models/` | 后续模型文件 | 忽略；资源说明纳入仓库 |
| `uv.lock`、`frontend/package-lock.json` | 依赖解析结果 | 提交 |

当前开发数据位置位于各自工作副本内，换一个副本会创建自己的 `.local`。

## 7 检查命令与问题定位

```powershell
npm --prefix frontend run typecheck
uv run --locked python -m unittest discover -s tests -v
uv run --locked voice-super-agent --help
uv run --locked python -c "import sys; import voice_super_agent; print(sys.executable); print(voice_super_agent.__file__)"
```

- 缺少前端产物：执行构建命令后重试。
- `--dev` 连接失败：先启动 Vite，检查终端输出与 5178 端口。
- 桌面引擎启动失败：核对 WebView2 Runtime、.NET 和终端异常；已启用的文件日志位于 `.local/logs/app.log`。
- 页面一直连接中：检查是否在 pywebview 桌面内打开；普通浏览器不提供 Python 桥接。查看桌面终端和日志中的页面订阅结果。

本文件描述开发工作副本的运行方式，安装分发与完整资源准备随后续阶段补齐。

环境准备见 [阶段 01 验证记录](verification/stage-01.md)，常驻、桥接和悬浮宿主的实机结果见 [阶段 02 验证记录](verification/stage-02.md)。

文字消息、原生上下文提交、流式服务测试与真实模型验收见 [阶段 03 验证记录](verification/stage-03.md)。
