# 依赖与迁入代码来源

本项目采用根目录 [MIT 许可证](../LICENSE)。Python 依赖通过 uv 从自己的锁文件安装，前端依赖通过 npm 从 `frontend/package-lock.json` 安装，各依赖继续保留上游许可证。本轮交付为 Windows 开发工作副本，依赖和模型按说明下载到本地。

| 内容 | 来源与版本 | 本仓库中的使用 |
| --- | --- | --- |
| Pipecat | [官方仓库](https://github.com/pipecat-ai/pipecat/tree/de8998789d5690073ddd1dafcec7268839af8153)，固定 Git revision `de8998789d5690073ddd1dafcec7268839af8153`；安装元数据为 1.14.1.dev387；BSD 2-Clause | `pyproject.toml` 声明 Git 来源，`uv.lock` 锁定；安装到本项目 `.venv`，使用原生轮次、帧、聚合、工具取消与音频输出 |
| STS 参考入口 | 开发时研究的 `bot.py` | 对照流水线顺序、模型参数和共享上下文；装配已在本仓库 `conversation.py`、`voice/pipeline.py` 维护 |
| 火山语音定制适配 | STS 的 `local_volcengine_services.py` | 迁入 `src/voice_super_agent/voice/volcengine.py`，独立维护 ASR 按需连接、输入归属和 TTS 协议 |
| System One 定制适配 | STS 的 `system_one_interruption.py` | 迁入 `src/voice_super_agent/voice/interruption.py`，保持原生附和/接管路径，并接入有限判意上下文 |
| 本地桌面音频 | Pipecat 原生处理器、PyAudio、pywebrtc-audio 0.2.0、pycaw 20251023 | `audio/` 内实现设备回调、实际播放参考、AEC、音量及默认端点生命周期 |
| 本地中文 KWS | Sherpa ONNX 1.13.8；固定 wenetspeech 3.3M 模型发布包；模型 README 标注 Apache License 2.0 | 下载到 `resources/models/`；保留上游 README、词表及模型，路径和文件见[资源说明](../resources/README.md) |
| ONNX Runtime | 1.31.1 | 从本项目 uv 环境显式加载，供本地唤醒及 Pipecat 模型共用 |
| 桌面与前端 | pywebview 6.2.1；React、TypeScript、Vite 的确切版本见 npm 锁文件 | `desktop/` 与 `frontend/` 独立维护；前端构建为本地 `frontend/dist/` |

STS 中的入口和两个定制模块原先属于开发工作区的额外文件；本仓库保留来源注释和实际迁入实现。运行时 import 来自本仓库或自己的 `.venv`，模型来自自己的 `resources/`。文档中的 STS 路径用于阅读对照；换电脑运行时无需准备相邻 STS 仓库。

Pipecat 源码由 uv 的 Git 缓存准备，wheel/源码缓存可以共享；虚拟环境、可编辑项目路径、模型和 `.local` 数据属于各自工作副本。独立性以实际 `sys.executable`、模块路径和新副本运行结果验证，见[交付记录](verification/stage-16.md)。
