# 阶段 01 验证记录

日期：2026-11-07。结果：阶段 01 完成。

对应 [阶段 01 计划](../plan/V1.0.0/01-project-bootstrap.md)，使用方法见 [开发运行](../development.md)。

## 实际环境与依赖

| 项目 | 结果 |
| --- | --- |
| 平台 | Windows x64，EdgeChromium 桌面引擎实际启动成功 |
| uv | 0.11.16 |
| Python | 本项目 `.venv`，CPython 3.12.13 |
| Pipecat | `1.14.1.dev387`，官方 Git 提交 `de8998789d5690073ddd1dafcec7268839af8153` |
| pywebview | 6.2.1 |
| Node.js / npm | 24.13.1 / 11.10.0 |
| React / React DOM | 19.3.1 |
| TypeScript / Vite | 7.0.2 / 8.3.6 |
| 开发服务 | `http://127.0.0.1:5178`，严格使用该端口 |

Pipecat 的实际版本由该 Git 源码的构建元数据产生。此前 STS 安装元数据中的 `1.15.0` 不用作当前依赖版本判断；本项目以提交来源和锁文件记录为准。

## 验证结果

| 验证 | 实际结果 |
| --- | --- |
| 独立 Python 安装 | `uv sync` 成功，生成 `uv.lock`；随后普通 `uv sync --locked` 成功 |
| 安装位置与来源 | Python 和 Pipecat 来自本项目 `.venv`，应用为本项目 `src`；Pipecat 安装元数据记录官方 Git URL 与对应提交 |
| Pipecat 核心导入 | `PipelineWorker`、`LLMContext` 导入成功 |
| 前端准备 | 安装成功，生成 `frontend/package-lock.json` |
| 类型检查和构建 | `tsc --noEmit`、Vite 构建成功，生成本地 `frontend/dist` |
| 后台线程 | 状态请求在 `agent-runtime` 处理；启动和停止后检查线程，停止后只剩主线程 |
| 构建产物启动 | `uv run --locked voice-super-agent` 成功，页面显示“桌面与后台已就绪” |
| 页面操作 | 在真实桌面中触发“检查启动状态”，日志记录新的后台查询 |
| 开发模式启动 | Vite 启动成功，`uv run --locked voice-super-agent --dev` 加载开发服务并实际调用后台状态接口 |
| 正常关闭 | 两种模式均通过窗口关闭退出，退出码 0，日志记录后台循环停止与应用退出 |
| 缺少构建产物 | 启动退出码 2，明确提示前端构建命令；验证后恢复产物 |
| 开发服务未启动 | `--dev` 退出码 2，明确提示服务地址、启动命令及连接原因 |
| 新工作副本 | 在 `.local/stage-01-independent` 创建独立工作副本和 `.venv`，利用已准备的 uv/npm 缓存离线安装、构建及后台启动均成功 |
| 副本路径 | 副本解释器、应用包、项目根路径均指向副本自身，能够定位自己的构建页面 |

## 首次安装的网络情况

首次从 GitHub 获取 Pipecat 的 Git 历史速度较慢，HTTP/1.1 重试后仍未及时完成。本次安装子进程使用临时 Git URL 重写，从本机 STS 仓库读取同一提交对象以填充 uv 缓存。该设置仅作用于这次安装进程。

项目依赖声明、锁文件和安装元数据均记录官方 Git 来源；应用运行不读取相邻 STS 目录。随后未设置重写的 `uv sync --locked` 成功。新工作副本验证使用本机已准备的缓存，未验证完全空缓存下的网络下载速度。

## 本机验证产物与后续范围

- 本机日志：`.local/logs/app.log`。
- 桌面截图：`.local/verification/stage-01-built-window.png`。
- 独立验证副本：`.local/stage-01-independent/`。

上述产物由 Git 忽略。阶段 01 已跑通独立工程与开发启动；单实例、托盘、窗口显示隐藏与完整桥接状态同步由阶段 02 推进。
