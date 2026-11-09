# Voice Super Agent

运行在 Windows 环境中的语音 Agent 项目，已接入独立开发启动、单实例、托盘常驻、DeepSeek 流式对话、SQLite 保存与恢复，以及参考 STS 的连续语音、本地唤醒、主动结束和空闲超时。本地音频已加入 AEC、播放参考、音量与默认设备管理；语音流程已完成自动化验证，真实耳机和外放联调待验收。

语音与文字已支持同一上下文的双向接管；文字可替代尚未完成的语音，旧输入迟到时仍按原归属排除。开发启动与窗口联调见 [开发运行](docs/development.md)，版本范围见 [V1.0.0 PRD](docs/prd/V1.0.0.md)，进度见 [实现计划](docs/plan/V1.0.0-implementation-plan.md)。

## 项目目标

- 通过语音与 Agent 交互。
- 使用super-agent架构
- 操作电脑上的应用和界面。
- 控制项目中的控件。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
