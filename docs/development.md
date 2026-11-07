# 开发运行

当前实现到阶段 01：独立工程、最小启动页面、Python 后台事件循环与启动状态查询。模型对话和语音在后续阶段接入，启动本阶段页面无需模型凭据。

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

后续接入模型时，将 `.env.example` 复制为 `.env`，填写相应字段。当前阶段不用创建或填写 `.env`。唤醒词、组合键和超时属于后续日常设置，服务凭据通过 `.env` 配置。

## 3 加载前端构建产物

```powershell
npm --prefix frontend run build
uv run --locked voice-super-agent
```

构建先进行 TypeScript 检查，再生成 `frontend/dist`。桌面通过 pywebview 的本地静态服务加载页面。没有构建产物时，启动命令会说明需要执行的构建命令。

页面启动后会查询实际运行于 `agent-runtime` 线程的 asyncio 循环，显示“桌面与后台已就绪”；点击“检查启动状态”可以再次查询。

当前阶段关闭窗口即结束桌面与后台循环。单实例、关闭隐藏到托盘和悬浮窗口属于阶段 02。

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

关闭桌面后，在运行 Vite 的终端按 `Ctrl+C` 结束开发服务。

## 5 数据与产物位置

| 位置 | 用途 | Git 管理 |
| --- | --- | --- |
| `.venv/` | 本项目 Python 环境 | 忽略 |
| `frontend/node_modules/` | 前端依赖 | 忽略 |
| `frontend/dist/` | 前端构建产物 | 忽略 |
| `.env` | 本地服务凭据 | 忽略；提交配置示例 |
| `.local/data/` | 后续数据库等本地数据 | 忽略 |
| `.local/logs/app.log` | 启动、状态查询和退出日志 | 忽略 |
| `.local/webview/` | WebView2 本地存储 | 忽略 |
| `resources/models/` | 后续模型文件 | 忽略；资源说明纳入仓库 |
| `uv.lock`、`frontend/package-lock.json` | 依赖解析结果 | 提交 |

当前开发数据位置位于各自工作副本内，换一个副本会创建自己的 `.local`。

## 6 检查命令与问题定位

```powershell
npm --prefix frontend run typecheck
uv run --locked voice-super-agent --help
uv run --locked python -c "import sys; import voice_super_agent; print(sys.executable); print(voice_super_agent.__file__)"
```

- 缺少前端产物：执行构建命令后重试。
- `--dev` 连接失败：先启动 Vite，检查终端输出与 5178 端口。
- 桌面引擎启动失败：核对 WebView2 Runtime、.NET 和终端异常；已启用的文件日志位于 `.local/logs/app.log`。
- 页面一直连接中：检查是否在 pywebview 桌面内打开；普通浏览器不提供 Python 桥接。查看桌面终端和日志中的后台状态查询结果。

本文件描述开发工作副本的运行方式，安装分发与完整资源准备随后续阶段补齐。

本次准备与实机结果见 [阶段 01 验证记录](verification/stage-01.md)。
