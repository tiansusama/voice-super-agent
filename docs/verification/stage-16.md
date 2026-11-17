# 独立开发运行与双击启动交付记录

日期：2026-11-17。分支：`feature/dev-delivery`，起点为 `main` 的 `f38d694`。对应[交付计划](../plan/V1.0.0/16-dev-delivery.md)、[开发运行说明](../development.md)和[验收矩阵](stage-15-acceptance.md)中的 AC-26。

本轮完成独立工作副本准备、双击启动进度、文字交流、托盘隐藏与唤回、重启恢复、设置和历史原文回读验证。按用户安排，真实发言、耳机/扬声器和设备切换继续留待后续验收，AC-26 的完整语音条件仍未完成。项目版本保持 `1.0.0.dev0`。

## 验证环境与代码范围

| 项目 | 实际条件 |
| --- | --- |
| 系统 | 当前 Windows 11 x64 开发机；系统已安装 Git、uv、Node.js/npm、WebView2 |
| 工具 | Git 2.48.1.windows.1、uv 0.11.16、Node.js 24.13.1、npm 11.10.0 |
| Python | uv 创建的独立 CPython 3.12.13 环境；新副本安装 67 个包 |
| Pipecat | Git revision `de8998789d5690073ddd1dafcec7268839af8153`，安装元数据为 1.14.1.dev387 |
| 工作副本 | `.local/verification/stage-16/independent project 小博` 与 `.local/verification/stage-16/double click 小博` |
| 初始状态 | 两个副本均从 `f38d694` 克隆后复制本轮交付改动；最初均无 `.venv`、`frontend/node_modules`、`frontend/dist`、`resources/models`、`.env` |
| 缓存与配置 | 使用本机已有下载缓存；各副本独立创建虚拟环境、前端依赖、模型、配置及数据目录 |
| 模型与音频 | 文字交互使用本地 OpenAI 兼容 SSE 服务；语音凭据留空；本轮没有打开麦克风或音频播放流 |

上述交付改动随后提交为 `e2ec316`。本次审查修复补回该记录，并增加启动错误、传递依赖和交付文档的回归测试。

## 独立准备与启动

第一个副本从其他工作目录运行 `scripts/prepare-dev.ps1`，依次通过 Python、前端依赖、前端构建及 KWS 模型准备，然后执行真实命令行入口 `uv run --locked --project <副本路径> voice-super-agent`。前端类型检查与生产构建通过。

第二个副本从其他工作目录通过 Windows Shell 启动真实 `start.cmd`，进度窗口自动完成四步准备并打开 pywebview 桌面。路径中的空格与中文正常处理；四步进度、当前步骤和用时均已观察。

| 检查 | 实际结果 |
| --- | --- |
| Python 与源码路径 | `sys.executable` 位于新副本 `.venv/Scripts/python.exe`；`voice_super_agent` 位于该副本 `src`；Pipecat 位于该副本 `.venv/Lib/site-packages` |
| STS 独立性 | 准备、启动和文字/历史工具链路使用新副本自己的代码与资源路径 |
| 首次配置 | 缺少 `.env` 时从示例生成；已有配置保留；未配置模型时界面明确显示原因，历史和设置仍可访问 |
| 重复启动 | 命令行第二次启动返回“应用已运行，正在显示已有主窗口”；双击会恢复原窗口，准备日志长度保持不变 |
| 关闭与退出 | 真实主窗口关闭消息使窗口隐藏；原实例继续运行；明确退出后可以重新启动 |
| 进度窗口取消 | 自动化验证关闭准备窗口会终止本次子进程树，后续准备步骤未执行 |

独立性验证依据运行时实际路径。各副本的数据库、日志和 WebView2 数据位于各自 `.local` 中。

## 文字、设置与持久化

桌面检查连接真实 Python 宿主及构建后的前端。测试启动时仅对子进程设置 WebView2 调试端口，用于自动操作界面；普通启动入口继续使用正常桌面配置。本地模型服务负责可控回复和工具请求，后台仍执行真实 Pipecat、桥接和 SQLite 路径。

| 场景 | 实际结果 |
| --- | --- |
| 连续文字交流 | 两轮问答形成四条展示消息；第二次模型请求包含前一轮上下文，正确接续测试代号“琥珀松鼠7612” |
| 设置保存 | 在 UI 将空闲超时保存为 75 秒，写入新副本数据库 |
| 历史页面 | 可以浏览已保存记录并读取完整原文 |
| 隐藏与唤回 | 主窗口隐藏后后台及桥接继续工作；重复双击恢复同一份消息 |
| 正常退出后重启 | 恢复四条展示消息及 75 秒设置 |
| 重新开始 | 当前聊天区进入空白工作窗口，旧历史保留 |
| 旧原文回读 | 本地模型实际依次请求 `history_search`、`history_read`，后端返回原文，最终回复正确 |
| 启动交接复测 | 准备进程等待应用接过单实例通知后退出；后续重启恢复当前两条消息与 75 秒设置 |

`check-copy.log` 记录 6 项通过；`check-recall.log` 记录 4 项通过。首次历史回读辅助检查在工具阶段过早判断完成并失败；改为等待最终回复后通过，最终结果以 `check-recall.log` 为准。

## 模型资源

两个副本分别通过准备脚本下载并解包官方 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` 包。下载包约 **31.142 MiB**，完整解包约 **35.623 MiB**；上游 README、词表、模型及样例一并保留，来源与许可证见[资源说明](../../resources/README.md)和[依赖来源](../code-sources.md)。

运行所需的四个文件已准备：

- `tokens.txt`
- `encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx`
- `decoder-epoch-12-avg-2-chunk-16-left-64.onnx`
- `joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx`

真实 `KeywordDetector` 使用“你好小博”成功加载，输入一块静音返回未命中。此项覆盖资源可用和模型初始化；真人发音的命中率仍待验证。锁定依赖同时提供 `silero_vad.onnx` 和 `smart-turn-v3.2-cpu.onnx`，分别为 2,327,524 和 8,679,182 字节。

## 启动测量与自动化

| 项目 | 结果 | 测量边界 |
| --- | --- | --- |
| 新副本首次双击 | 45.307 秒 | 从 Shell 启动至 WebView2 调试端点就绪；已有系统工具和下载缓存，包含自动准备 |
| 正常退出后再次双击 | 11.121 秒 | 同一起止点，包含检查、重新构建和 Python 初始化 |
| 原始交付启动测试 | 5 项通过 | 配置保留、按需安装、失败终止后续步骤、取消子进程树 |
| 原始交付完整 Python 测试 | 206 passed，2 warnings，59.18 秒 | 保存于本轮 `pytest.log`；两条为现有依赖弃用提示 |
| 审查修复后完整 Python 测试 | 210 passed，2 warnings，61.74 秒 | 新增 4 项回归；输出保存于 `.local/verification/review-delivery-fixes/pytest.log` |

两次启动测量各为一个样本，调试端点就绪与用户看到首帧的时间含义不同；这些数字供开发观察使用。

本次审查新增的回归覆盖：日志只读及工作目录冲突时显示具体失败原因并退出；直接依赖完整但传递依赖缺失时，由真实 npm 离线重装后完成构建；交付入口文档和本地链接实际存在。原有取消测试等待子进程完成退出后再断言。

执行这些回归及完整 Python 测试：

```powershell
uv run --locked pytest tests/test_delivery.py tests/test_delivery_docs.py -q
uv run --locked pytest -q
```

## 本机证据与后续验收

本机原始输出位于被 Git 忽略的 `.local/verification/stage-16/`：`prepare.log`、`launch-copy.log`、`restart-copy.log`、`model-requests.json`、`check-copy.log`、`check-recall.log`、`manual.stdout.log`、`manual-duplicate.stdout.log`、`pytest.log` 及 `output/` 中的截图。本文件将实际条件、结果和限制纳入版本管理；本地测试副本及其数据继续属于开发产物。

真实关键词/全局按键、连续发言和声学断句、扬声器回声消除与语义接管、实际设备切换、可听首回复/停播延迟及长期语音资源测量，继续按[阶段 15 人工验收清单](stage-15.md)的 M-15-01～M-15-05 安排。16.07、16.08 和 AC-26 继续保留对应未完成条件，完整 V1.0.0 验收尚待这些结果。
