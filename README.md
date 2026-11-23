# Voice Super Agent

运行在 Windows 11 x64 上的常驻语音 Agent。Python 后端与 React 界面通过 pywebview 连接，语音和文字共用一个 Agent 上下文，支持托盘、语义接管、历史回读和重启恢复。核心实现与自动化已完成，真实耳机/外放及设备操作继续按验收计划安排。

语音与文字已支持同一上下文的双向接管；文字可替代尚未完成的语音，旧输入迟到时仍按原归属排除。开发启动与窗口联调见 [开发运行](docs/development.md)，版本范围见 [V1.0.0 PRD](docs/prd/V1.0.0.md)，进度见 [实现计划](docs/plan/V1.0.0-implementation-plan.md)。

## 在 Windows 上开始使用

Windows 11 x64 安装原型和首次启动配置见[安装说明](docs/windows-installation.md)。

先准备 Git、uv、Node.js/npm 和 WebView2，具体版本及安装入口见[环境说明](docs/development.md#1-环境)。克隆后双击根目录的 **`start.cmd`**。

启动准备窗口会显示四步进度：Python 环境、界面依赖、界面构建和唤醒模型。缺少的项目依赖和模型自动下载；首次生成 `.env`，已有配置继续保留。服务密钥需要自行填写本地 `.env` 并重启；只填写 DeepSeek Key 即可文字对话。应用已运行时，重复双击直接唤出原窗口。

也可以在终端准备和启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare-dev.ps1
uv run --locked voice-super-agent
```

关闭主窗口后应用在托盘继续运行，通过托盘或设置中的“退出应用”明确退出。数据保存在本工作副本的 `.local/`。准备日志和问题处理见[开发运行](docs/development.md)，代码与模型来源见[依赖及迁入代码说明](docs/code-sources.md)，独立副本验证见[交付验证记录](docs/verification/stage-16.md)。

## 项目目标

- 通过语音与 Agent 交互。
- 使用super-agent架构
- 操作电脑上的应用和界面。
- 控制项目中的控件。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
